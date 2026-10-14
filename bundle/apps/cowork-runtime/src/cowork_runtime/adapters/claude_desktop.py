"""Incremental metadata reader for Claude Desktop IndexedDB tool records.

Claude Desktop keeps conversation messages in Chromium IndexedDB (LevelDB).
This adapter reads only tool-call metadata from those records: the tool-use
id, the tool name, the error flag, timestamps and -- for the ``Skill`` tool --
the Skill name.  Conversation text, tool inputs and tool results are never
decoded.

Recognised evidence:

* MCP tool calls: ``mcp__{server}__{tool}``, ``+server:tool`` /
  ``server:tool`` and the local bridge form ``/lcl-{server}-{tool}``.
* Skill calls: the agent ``Skill`` tool (its ``skill`` input names the Skill).
* Deferred Skill loading: a ``tool_search`` result that says
  ``Loaded N <skill> tool`` for a Skill in the local inventory.
"""

from __future__ import annotations

import bisect
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from cowork_contracts import ComponentEventV1

from .degraded import degraded_event
from .registry import CodexCursor, PluginRegistry


class ClaudeDesktopParseError(ValueError):
    def __init__(self, message: str, *, path: str, offset: int) -> None:
        super().__init__(f"{message} (path={path}, offset={offset})")
        self.path = path
        self.offset = offset


@dataclass(frozen=True)
class ClaudeDesktopReadResult:
    events: tuple[ComponentEventV1, ...]
    next_cursor: CodexCursor
    diagnostics: tuple[str, ...]


_TS = rb"20\d\d-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z"
# The V8 length byte varies with the fractional-second precision.
_START_TIMESTAMP = re.compile(rb'start_timestamp"[\x14-\x20](?P<occurred>' + _TS + rb')"')
_NATIVE_CREATED_AT = re.compile(rb'created_at"[\x14-\x20](?P<occurred>' + _TS + rb')"')
_LOADED_SKILL = re.compile(rb"Loaded\s+\d+\s+(?P<skill>[A-Za-z0-9_-]+)\s+tool")

_USE_MARKER = b'type"\x08tool_use"'
_RESULT_MARKER = b'type"\x0btool_result"'
_MARKERS = re.compile(re.escape(_USE_MARKER) + b"|" + re.escape(_RESULT_MARKER))
_ID_KEY = b'\x02id"'
_TOOL_USE_ID_KEY = b'\x0btool_use_id"'
_NAME_KEY = b'\x04name"'
_SKILL_KEY = b'\x05skill"'
_INPUT_KEY = b'"\x05input'
_PREFERRED_SKILL_KEYS = ("skill", "skill_name", "skillName", "skillId", "skill_id", "name", "command")
_IS_ERROR = re.compile(rb"\x08?is_error(?P<error>[FT])")
# Older fixtures and builds did not always carry a well-formed V8 length
# prefix in front of short strings; fall back to "optional control byte, then
# printable bytes up to the next string tag".
_LOOSE_STRING = re.compile(rb'[\x00-\x1f]?(?P<value>[^"\x00-\x1f]{1,256})"')
_PRINTABLE = re.compile(rb"[\x20\x21\x23-\x7e]{1,256}")
_SKILL_NAME = re.compile(r"[A-Za-z0-9_:.\-]{1,128}")

# Key windows, in bytes, measured from the tool marker.
_ID_TO_NAME_WINDOW = 1024
_RESULT_NAME_WINDOW = 256
_BLOCK_WINDOW = 8192
_LEGACY_TIMESTAMP_WINDOW = 160
_TOOL_SEARCH_WINDOW = 65536

_LOG_BLOCK_SIZE = 32768
_LOG_HEADER_SIZE = 7
_LOG_FULL, _LOG_FIRST, _LOG_MIDDLE, _LOG_LAST = 1, 2, 3, 4


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _identity(name: str) -> tuple[str, str, str]:
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3 and parts[1] and parts[2]:
            return "mcp_tool", name, "mcp_call"
    if name.startswith("+") or ":" in name:
        value = name.lstrip("+")
        if ":" in value:
            server, tool = value.split(":", 1)
            if server and tool:
                return "mcp_tool", f"mcp__{server}__{tool}", "mcp_call"
    # Claude Desktop's local MCP bridge serializes a server/tool pair as
    # `/lcl-{server}-{tool}` (the leading slash is not always present in the
    # stored string).  Bridge tool names use underscores, so split the final
    # hyphen to preserve server names that contain hyphens.
    bridged = name.removeprefix("/")
    if bridged.startswith("lcl-"):
        value = bridged.removeprefix("lcl-")
        if "-" in value:
            server, tool = value.rsplit("-", 1)
            if server and tool:
                return "mcp_tool", f"mcp__{server}__{tool}", "mcp_call"
    return "tool", name, "tool_call"


def _event_id(installation: str, session: str, call: str, stage: str) -> str:
    digest = hashlib.sha256(f"{installation}:{session}:{call}:{stage}".encode()).hexdigest()
    return f"claude-desktop:{digest}"


def _file_identity(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_dev}:{stat.st_ino}"


@dataclass(frozen=True)
class _Stream:
    """Logical bytes of a source file plus the mapping back to file offsets."""

    data: bytes
    # (logical_start, file_start, length) for every copied fragment.
    segments: tuple[tuple[int, int, int], ...]
    # File offset up to which every logical record is complete.
    complete_end: int
    framed: bool = False

    def logical_offset(self, file_offset: int) -> int:
        if not self.segments:
            return min(file_offset, len(self.data))
        starts = [segment[1] for segment in self.segments]
        index = bisect.bisect_right(starts, file_offset) - 1
        if index < 0:
            return 0
        logical, start, length = self.segments[index]
        if file_offset < start + length:
            return logical + (file_offset - start)
        return logical + length


def _raw_stream(raw: bytes) -> _Stream:
    return _Stream(raw, ((0, 0, len(raw)),) if raw else (), len(raw))


def _leveldb_log_stream(raw: bytes) -> _Stream | None:
    """Strip LevelDB write-ahead-log framing.

    A ``.log`` file is a sequence of 32 KiB blocks; every physical record has
    a 7-byte header and values larger than the space left in a block are
    split into FIRST/MIDDLE/LAST fragments.  Without removing the headers a
    tool record that crosses a block boundary cannot be matched.  Only
    complete logical records are returned so a record still being written is
    re-read on the next poll.  Returns ``None`` when the bytes are not a
    well-formed log (callers then scan the raw bytes as before).
    """
    data = bytearray()
    segments: list[tuple[int, int, int]] = []
    pending: list[tuple[int, int]] = []
    complete_end = 0
    seen = False
    position = 0
    while position < len(raw):
        left = _LOG_BLOCK_SIZE - position % _LOG_BLOCK_SIZE
        if left < _LOG_HEADER_SIZE:
            position += left
            continue
        if position + _LOG_HEADER_SIZE > len(raw):
            break
        header = raw[position:position + _LOG_HEADER_SIZE]
        length = header[4] | header[5] << 8
        kind = header[6]
        if kind == 0 and length == 0:
            # Zero-filled tail of a block or of a preallocated file.
            position += left
            continue
        if kind not in (_LOG_FULL, _LOG_FIRST, _LOG_MIDDLE, _LOG_LAST) or length > left - _LOG_HEADER_SIZE:
            return None
        start = position + _LOG_HEADER_SIZE
        end = start + length
        if end > len(raw):
            break  # the writer has not finished this physical record yet
        if kind in (_LOG_FULL, _LOG_FIRST) and pending:
            return None
        if kind in (_LOG_MIDDLE, _LOG_LAST) and not pending:
            return None
        seen = True
        pending.append((start, length))
        if kind in (_LOG_FULL, _LOG_LAST):
            for fragment_start, fragment_length in pending:
                segments.append((len(data), fragment_start, fragment_length))
                data += raw[fragment_start:fragment_start + fragment_length]
            pending.clear()
            complete_end = end
        position = end
    if not seen:
        return None
    return _Stream(bytes(data), tuple(segments), complete_end, framed=True)


_TABLE_MAGIC = 0xDB4775248B80FB57
_TABLE_FOOTER_SIZE = 48
_BLOCK_TRAILER_SIZE = 5


class _TableFormatError(ValueError):
    pass


def _varint(data: bytes, position: int, end: int | None = None) -> tuple[int, int]:
    limit = len(data) if end is None else end
    value = shift = 0
    while position < limit and shift <= 63:
        byte = data[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, position
        shift += 7
    raise _TableFormatError("truncated varint")


def _snappy_decompress(data: bytes) -> bytes:
    """Decode one raw Snappy block (the format LevelDB uses for table blocks)."""
    expected, position = _varint(data, 0)
    output = bytearray()
    while position < len(data):
        tag = data[position]
        position += 1
        kind = tag & 0x03
        if kind == 0:  # literal
            length = tag >> 2
            if length >= 60:
                extra = length - 59
                if position + extra > len(data):
                    raise _TableFormatError("truncated snappy literal length")
                length = int.from_bytes(data[position:position + extra], "little")
                position += extra
            length += 1
            if position + length > len(data):
                raise _TableFormatError("truncated snappy literal")
            output += data[position:position + length]
            position += length
            continue
        if kind == 1:
            if position >= len(data):
                raise _TableFormatError("truncated snappy copy")
            length = ((tag >> 2) & 0x07) + 4
            offset = ((tag >> 5) << 8) | data[position]
            position += 1
        else:
            size = 2 if kind == 2 else 4
            if position + size > len(data):
                raise _TableFormatError("truncated snappy copy")
            length = (tag >> 2) + 1
            offset = int.from_bytes(data[position:position + size], "little")
            position += size
        if offset == 0 or offset > len(output):
            raise _TableFormatError("invalid snappy copy offset")
        start = len(output) - offset
        for index in range(length):  # copies may overlap their own output
            output.append(output[start + index])
    if len(output) != expected:
        raise _TableFormatError("snappy length mismatch")
    return bytes(output)


def _table_block(raw: bytes, offset: int, size: int) -> bytes:
    end = offset + size
    if offset < 0 or end + _BLOCK_TRAILER_SIZE > len(raw):
        raise _TableFormatError("block handle outside table")
    contents = raw[offset:end]
    compression = raw[end]
    if compression == 0:
        return contents
    if compression == 1:
        return _snappy_decompress(contents)
    raise _TableFormatError(f"unsupported block compression {compression}")


def _block_entries(block: bytes) -> list[tuple[bytes, bytes]]:
    if len(block) < 4:
        raise _TableFormatError("block too small")
    restarts = int.from_bytes(block[-4:], "little")
    limit = len(block) - 4 - 4 * restarts
    if limit < 0:
        raise _TableFormatError("invalid restart array")
    entries: list[tuple[bytes, bytes]] = []
    key = b""
    position = 0
    while position < limit:
        shared, position = _varint(block, position, limit)
        unshared, position = _varint(block, position, limit)
        value_length, position = _varint(block, position, limit)
        if shared > len(key) or position + unshared + value_length > limit:
            raise _TableFormatError("corrupt block entry")
        key = key[:shared] + block[position:position + unshared]
        position += unshared
        entries.append((key, block[position:position + value_length]))
        position += value_length
    return entries


def _leveldb_table_values(raw: bytes) -> list[bytes]:
    """Values of every data block of a LevelDB table (``.ldb`` / ``.sst``).

    LevelDB compacts its write-ahead log into sorted tables whose blocks are
    usually Snappy-compressed, so tool records that were compacted before the
    worker read them are only readable after decoding the blocks.
    """
    if len(raw) < _TABLE_FOOTER_SIZE:
        raise _TableFormatError("file too small for a table")
    footer = raw[-_TABLE_FOOTER_SIZE:]
    if int.from_bytes(footer[-8:], "little") != _TABLE_MAGIC:
        raise _TableFormatError("not a LevelDB table")
    _, position = _varint(footer, 0)  # metaindex offset
    _, position = _varint(footer, position)  # metaindex size
    index_offset, position = _varint(footer, position)
    index_size, _ = _varint(footer, position)
    values: list[bytes] = []
    for _, handle in _block_entries(_table_block(raw, index_offset, index_size)):
        block_offset, cursor = _varint(handle, 0)
        block_size, _ = _varint(handle, cursor)
        values.extend(value for _, value in _block_entries(_table_block(raw, block_offset, block_size)))
    return values


def _leveldb_table_stream(raw: bytes) -> _Stream | None:
    try:
        values = _leveldb_table_values(raw)
    except _TableFormatError:
        return None
    # Tables are immutable: once decoded, the whole file is complete.
    return _Stream(b"".join(values), (), len(raw), framed=False)


def _source_stream(source: Path, raw: bytes) -> _Stream:
    if source.suffix == ".log":
        stream = _leveldb_log_stream(raw)
        if stream is not None:
            return stream
    if source.suffix in (".ldb", ".sst"):
        stream = _leveldb_table_stream(raw)
        if stream is not None:
            return stream
    return _raw_stream(raw)


def _string_at(data: bytes, position: int, *, followers: bytes = b'"') -> str | None:
    """Read the V8 one-byte string whose length prefix starts at ``position``."""
    length = shift = 0
    cursor = position
    while cursor < len(data) and shift <= 28:
        byte = data[cursor]
        cursor += 1
        length |= (byte & 0x7F) << shift
        shift += 7
        if byte & 0x80:
            continue
        end = cursor + length
        if 0 < length <= 256 and end < len(data) and data[end] in followers:
            value = data[cursor:end]
            if _PRINTABLE.fullmatch(value):
                return value.decode("ascii")
        break
    loose = _LOOSE_STRING.match(data, position)
    return loose.group("value").decode("ascii", "replace") if loose else None


def _timestamp(data: bytes, marker: int, block_end: int, fallback: datetime) -> datetime:
    """Best available time for a tool record, without reading message text."""
    previous = None
    for candidate in _START_TIMESTAMP.finditer(data, max(0, marker - _LEGACY_TIMESTAMP_WINDOW), marker):
        previous = candidate
    if previous is not None:
        return _utc(previous.group("occurred").decode("ascii"))
    following = _START_TIMESTAMP.search(data, marker, min(block_end, marker + _BLOCK_WINDOW))
    if following is not None:
        return _utc(following.group("occurred").decode("ascii"))
    created = _NATIVE_CREATED_AT.search(data, marker)
    if created is not None:
        return _utc(created.group("occurred").decode("ascii"))
    last = None
    for candidate in _NATIVE_CREATED_AT.finditer(data, 0, marker):
        last = candidate
    return _utc(last.group("occurred").decode("ascii")) if last is not None else fallback


def _read_string(data: bytes, position: int, end: int) -> tuple[str | None, int]:
    """Strictly read one V8 one-byte string whose tag is at ``position``."""
    if position >= end or data[position] != 0x22:
        return None, position
    length = shift = 0
    cursor = position + 1
    while cursor < end and shift <= 28:
        byte = data[cursor]
        cursor += 1
        length |= (byte & 0x7F) << shift
        shift += 7
        if byte & 0x80:
            continue
        stop = cursor + length
        if 0 < length <= 256 and stop <= end and _PRINTABLE.fullmatch(data[cursor:stop]):
            return data[cursor:stop].decode("ascii"), stop
        break
    return None, position


def _skill_from_input(data: bytes, start: int, end: int) -> str | None:
    """Skill name from a ``Skill`` tool's flat ``input`` object.

    Only top-level string keys/values of the input object are read; a value
    is accepted only when it looks like a Skill identifier.
    """
    key = data.find(_INPUT_KEY, start, end)
    if key < 0:
        return None
    position = key + len(_INPUT_KEY)
    if position >= end or data[position] != 0x6F:  # 'o' begins a V8 object
        return None
    position += 1
    values: dict[str, str] = {}
    for _ in range(8):
        name, after_name = _read_string(data, position, end)
        if name is None:
            break
        value, after_value = _read_string(data, after_name, end)
        if value is None:
            break
        values[name] = value
        position = after_value
    for candidate in _PREFERRED_SKILL_KEYS:
        value = values.get(candidate)
        if value and _SKILL_NAME.fullmatch(value):
            return value
    if len(values) == 1:
        value = next(iter(values.values()))
        if _SKILL_NAME.fullmatch(value):
            return value
    return None


@dataclass(frozen=True)
class _ToolRecord:
    position: int
    block_end: int
    kind: str  # "use" | "result"
    call: str
    name: str | None
    skill: str | None
    failed: bool
    occurred: datetime


def _parse_records(data: bytes, fallback: datetime) -> tuple[list[_ToolRecord], list[tuple[int, str]]]:
    markers = [(match.start(), match.group()) for match in _MARKERS.finditer(data)]
    records: list[_ToolRecord] = []
    unparsed: list[tuple[int, str]] = []
    for index, (position, marker) in enumerate(markers):
        block_end = markers[index + 1][0] if index + 1 < len(markers) else len(data)
        body = position + len(marker)
        if marker == _USE_MARKER:
            if not data.startswith(_ID_KEY, body):
                unparsed.append((position, "use"))
                continue
            call = _string_at(data, body + len(_ID_KEY))
            name_key = data.find(_NAME_KEY, body, min(block_end, body + _ID_TO_NAME_WINDOW))
            name = _string_at(data, name_key + len(_NAME_KEY)) if name_key >= 0 else None
            if not call or not name:
                unparsed.append((position, "use"))
                continue
            skill = None
            if name.casefold() == "skill":
                skill_key = data.find(_SKILL_KEY, name_key, min(block_end, name_key + _BLOCK_WINDOW))
                if skill_key >= 0:
                    value = _string_at(data, skill_key + len(_SKILL_KEY), followers=b'"{')
                    if value and _SKILL_NAME.fullmatch(value):
                        skill = value
                if skill is None:
                    skill = _skill_from_input(data, name_key, min(block_end, name_key + _BLOCK_WINDOW))
            records.append(_ToolRecord(
                position, block_end, "use", call, name, skill, False,
                _timestamp(data, position, block_end, fallback),
            ))
            continue
        if not data.startswith(_TOOL_USE_ID_KEY, body):
            unparsed.append((position, "result"))
            continue
        call = _string_at(data, body + len(_TOOL_USE_ID_KEY))
        name_key = data.find(_NAME_KEY, body, min(block_end, body + _RESULT_NAME_WINDOW))
        name = _string_at(data, name_key + len(_NAME_KEY)) if name_key >= 0 else None
        error = _IS_ERROR.search(data, body, min(block_end, body + _ID_TO_NAME_WINDOW))
        if not call or error is None:
            unparsed.append((position, "result"))
            continue
        records.append(_ToolRecord(
            position, block_end, "result", call, name, None, error.group("error") == b"T",
            _timestamp(data, position, block_end, fallback),
        ))
    return records, unparsed


def _evidence(record: "_ToolRecord") -> str:
    # A Skill call whose Skill name could not be read is still a Skill call,
    # but only partially identified.
    if record.name is not None and record.name.casefold() == "skill" and record.skill is None:
        return "partial"
    return "verified"


def _collect_records(
    raw: bytes, stream: _Stream, fallback: datetime,
) -> tuple[list[_ToolRecord], list[tuple[int, str]], list[str]]:
    """Tool records of a source, in logical-stream coordinates."""
    diagnostics: list[str] = ["leveldb_log_framing_removed"] if stream.framed else []
    records, unparsed = _parse_records(stream.data, fallback)
    if stream.framed:
        # Also read the raw file bytes.  A few current records are only
        # readable there; records readable in both views are merged by
        # (kind, call) and positioned in logical coordinates.
        known = {(record.kind, record.call) for record in records}
        raw_records, _ = _parse_records(raw, fallback)
        extra = [
            _ToolRecord(
                stream.logical_offset(record.position), stream.logical_offset(record.block_end),
                record.kind, record.call, record.name, record.skill, record.failed, record.occurred,
            )
            for record in raw_records
            if (record.kind, record.call) not in known
        ]
        if extra:
            diagnostics.append(f"raw_view_records={len(extra)}")
            records = sorted(records + extra, key=lambda item: (item.position, item.kind == "result"))
    return records, unparsed, diagnostics


class ClaudeDesktopAdapter:
    """Read Claude Desktop's native LevelDB records without transcript bodies."""

    def __init__(self, registry: PluginRegistry) -> None:
        self.registry = registry

    def read(
        self,
        path: str | Path,
        *,
        installation_id: str,
        session_id: str,
        run_id: str | None = None,
        skill_ids: Iterable[str] = (),
        now: datetime | None = None,
        persist_cursor: bool = True,
    ) -> ClaudeDesktopReadResult:
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise ClaudeDesktopParseError("IndexedDB source is not a regular file", path=str(source), offset=0)
        if not session_id.strip():
            raise ValueError("session_id must be non-empty")
        self.registry._require_active(installation_id)
        binding = self.registry.binding(installation_id)
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        identity = _file_identity(source)
        cursor = self.registry.cursor(installation_id, str(source))
        offset = 0 if cursor is None or cursor.file_identity != identity else cursor.offset
        size = source.stat().st_size
        if cursor is not None and cursor.file_identity == identity and size < cursor.offset:
            raise ClaudeDesktopParseError("IndexedDB source was truncated", path=str(source), offset=cursor.offset)
        raw = source.read_bytes()
        # LevelDB records can be rewritten as a new file.  Parse the whole file
        # every time (start context may precede the cursor); only records at or
        # after the cursor emit events, and stable IDs keep delivery idempotent.
        stream = _source_stream(source, raw)
        data = stream.data
        start = stream.logical_offset(offset)
        allowed_skills = {item.strip() for item in skill_ids if isinstance(item, str) and item.strip()}
        records, unparsed, diagnostics = _collect_records(raw, stream, current)
        new_uses = [record for record in records if record.kind == "use" and record.position >= start]
        new_unparsed_uses = [position for position, kind in unparsed if kind == "use" and position >= start]
        # Claude's storage format is private and changes with client releases.
        # A tool call this version cannot read is counted as one unrecognized
        # (inferred) call and reported, instead of stopping the whole source.
        if new_unparsed_uses:
            diagnostics.append(f"unparsed_tool_use_records={len(new_unparsed_uses)}")

        run = run_id or session_id
        events: list[ComponentEventV1] = []
        seen: set[str] = set()
        starts: dict[str, _ToolRecord] = {}

        def emit(event: ComponentEventV1) -> None:
            if event.event_id not in seen:
                seen.add(event.event_id)
                events.append(event)

        for position in new_unparsed_uses:
            emit(degraded_event(
                runtime="claude", plugin_id="claude-desktop-adapter", installation_id=installation_id,
                session_id=session_id, run_id=run_id or session_id, native_call=f"{source.name}:{position}",
                hint="claude_tool_use", occurred_at=_timestamp(data, position, len(data), current),
                source_type="indexeddb_leveldb", evidence_source="claude_desktop_unrecognized", binding=binding,
            ))

        def component(record: _ToolRecord) -> tuple[str, str, str]:
            assert record.name is not None
            if record.name.casefold() == "skill":
                return "skill", record.skill or record.name, "skill_call"
            return _identity(record.name)

        mismatched = 0
        for record in records:
            if record.kind == "use":
                known = starts.get(record.call)
                if known is None or (known.skill is None and record.skill is not None):
                    # Streaming rewrites store the same call several times;
                    # keep the first sighting unless a later one names the Skill.
                    starts[record.call] = record if known is None else _ToolRecord(
                        known.position, known.block_end, "use", known.call, known.name,
                        record.skill, False, known.occurred,
                    )
                if record.position < start:
                    continue
                kind, component_id, observation = component(starts[record.call])
                emit(self._event(
                    installation_id, session_id, run, record.call, kind, component_id,
                    "started", record.occurred, starts[record.call].occurred, observation,
                    _evidence(starts[record.call]), binding,
                ))
                continue
            started = starts.get(record.call)
            if started is None or record.position < start:
                continue
            if record.name is not None and record.name != started.name:
                # A mismatched pair is malformed input; do not guess which
                # component the result belongs to.
                mismatched += 1
                continue
            kind, component_id, observation = component(started)
            stage = "failed" if record.failed else "completed"
            emit(self._event(
                installation_id, session_id, run, record.call, kind, component_id,
                stage, record.occurred, started.occurred, observation, _evidence(started), binding,
            ))
            # Native deferred-tool search is the Claude Desktop signal that a
            # Skill was selected.  Require the Skill to exist in the local
            # inventory so a generic tool search cannot become a fabricated
            # Skill event.
            if started.name == "tool_search" and allowed_skills:
                snippet = data[record.position:min(record.block_end, record.position + _TOOL_SEARCH_WINDOW)]
                for skill_match in _LOADED_SKILL.finditer(snippet):
                    skill = skill_match.group("skill").decode("ascii", "replace")
                    if skill not in allowed_skills:
                        continue
                    emit(self._event(
                        installation_id, session_id, run, f"{record.call}:skill:{skill}", "skill", skill,
                        "completed", record.occurred, started.occurred, "skill_selected", "partial", binding,
                        evidence_source="claude_desktop_tool_search",
                    ))
        if mismatched:
            diagnostics.append(f"mismatched_tool_results={mismatched}")
        events.sort(key=lambda item: (item.occurred_at, item.event_id))
        next_cursor = CodexCursor(
            installation_id=installation_id, source_path=str(source), file_identity=identity,
            offset=stream.complete_end, updated_at=current,
        )
        if persist_cursor:
            self.registry.save_cursor(next_cursor)
        return ClaudeDesktopReadResult(events=tuple(events), next_cursor=next_cursor, diagnostics=tuple(diagnostics))

    @staticmethod
    def _event(
        installation_id: str, session_id: str, run_id: str, invocation: str, kind: str, component: str,
        stage: str, occurred: datetime, started: datetime, observation: str, evidence: str, binding: Any,
        *, evidence_source: str = "claude_desktop_indexeddb",
    ) -> ComponentEventV1:
        return ComponentEventV1(
            schema_version="component_event.v1", event_id=_event_id(installation_id, session_id, invocation, stage),
            invocation_id=invocation, run_id=run_id, runtime="claude", plugin_id="claude-desktop-adapter",
            component_kind=kind, component_id=component, component_version=None, stage=stage,
            occurred_at=occurred, started_at=started, ended_at=occurred if stage != "started" else None,
            error_code="tool_error" if stage == "failed" else None, observation_kind=observation,
            evidence_source=evidence_source,
            evidence_level=evidence, installation_id=installation_id,
            tenant_id=binding.tenant_id if binding is not None else None,
            user_id=binding.user_id if binding is not None else None, source_type="indexeddb_leveldb",
        )


__all__ = ["ClaudeDesktopAdapter", "ClaudeDesktopParseError", "ClaudeDesktopReadResult"]
