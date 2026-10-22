"""Claude Desktop turns that used a Skill / MCP: the user's message and the final reply.

Claude Desktop caches each conversation in IndexedDB as one value: an object whose
"tree" holds {kind: "hub_transcript", conversationUuid, messages: [...]} (V8
serialization; large values live in the ".blob" folder next to the LevelDB folder,
Snappy-compressed behind a Blink header). Each message has uuid, sender
("human" / "assistant"), created_at, stop_reason and content blocks (text,
tool_use, tool_result). A turn is a human message with text and the assistant
messages after it; it is finished once the last assistant message has a stop reason.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..adapters import claude_desktop as native

_TREE = b'"\x04treeo'
_MAX_DEPTH = 64


class _Unsupported(ValueError):
    pass


class _Reader:
    """Minimal V8 ValueDeserializer for the plain JSON-like values Claude stores."""

    def __init__(self, data: bytes, position: int, limit: int) -> None:
        self.data, self.position, self.limit = data, position, limit
        self.objects: list[Any] = []

    def varint(self) -> int:
        value = shift = 0
        while True:
            if self.position >= self.limit or shift > 63:
                raise _Unsupported("varint")
            byte = self.data[self.position]
            self.position += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return value

    def value(self, depth: int = 0) -> Any:
        if depth > _MAX_DEPTH:
            raise _Unsupported("depth")
        while self.position < self.limit and self.data[self.position] == 0x00:  # padding
            self.position += 1
        if self.position >= self.limit:
            raise _Unsupported("end")
        tag = self.data[self.position]
        self.position += 1
        if tag == 0xFF:  # version
            self.varint()
            return self.value(depth)
        if tag == 0x22:  # one-byte string
            size = self.varint()
            text = self.data[self.position:self.position + size].decode("latin-1")
            self.position += size
            return text
        if tag == 0x63:  # two-byte string
            size = self.varint()
            text = self.data[self.position:self.position + size].decode("utf-16-le", "replace")
            self.position += size
            return text
        if tag == 0x53:  # UTF-8 string
            size = self.varint()
            text = self.data[self.position:self.position + size].decode("utf-8", "replace")
            self.position += size
            return text
        if tag == 0x49:  # int32 (zigzag)
            raw = self.varint()
            return (raw >> 1) ^ -(raw & 1)
        if tag == 0x55:  # uint32
            return self.varint()
        if tag in (0x4E, 0x44):  # double, date
            self.position += 8
            return None
        if tag == 0x54:
            return True
        if tag == 0x46:
            return False
        if tag in (0x5F, 0x30):  # undefined, null
            return None
        if tag == 0x6F:  # object
            result: dict[str, Any] = {}
            self.objects.append(result)
            while True:
                while self.position < self.limit and self.data[self.position] == 0x00:
                    self.position += 1
                if self.position < self.limit and self.data[self.position] == 0x7B:
                    self.position += 1
                    self.varint()
                    return result
                key = self.value(depth + 1)
                result[str(key)] = self.value(depth + 1)
        if tag in (0x61, 0x41):  # sparse / dense array
            length = self.varint()
            items: dict[int, Any] = {}
            self.objects.append(items)
            if tag == 0x41:
                for index in range(length):
                    if self.position < self.limit and self.data[self.position] == 0x2D:  # hole
                        self.position += 1
                        continue
                    items[index] = self.value(depth + 1)
            end = 0x40 if tag == 0x61 else 0x24
            while True:
                while self.position < self.limit and self.data[self.position] == 0x00:
                    self.position += 1
                if self.position < self.limit and self.data[self.position] == end:
                    self.position += 1
                    self.varint()
                    self.varint()
                    return [items[key] for key in sorted(k for k in items if isinstance(k, int))]
                key = self.value(depth + 1)
                item = self.value(depth + 1)
                if isinstance(key, int):
                    items[key] = item
        if tag == 0x5E:  # reference to an earlier object
            index = self.varint()
            return self.objects[index] if index < len(self.objects) else None
        raise _Unsupported(f"tag {tag:#x}")


def transcripts(data: bytes) -> list[dict[str, Any]]:
    """Every readable hub_transcript tree in a byte stream (oldest first)."""
    found = []
    for match in re.finditer(re.escape(_TREE), data):
        reader = _Reader(data, match.end() - 1, len(data))
        try:
            tree = reader.value()
        except (_Unsupported, IndexError):
            continue
        if isinstance(tree, dict) and isinstance(tree.get("conversationUuid"), str) and isinstance(tree.get("messages"), list):
            found.append(tree)
    return found


def _blob_data(raw: bytes) -> bytes:
    if raw[:3] == b"\xff\x11\x02":  # Blink header + Snappy-compressed V8 value
        try:
            return native._snappy_decompress(raw[3:])
        except Exception:
            return raw
    return raw


def read_sources(leveldb_root: Path) -> list[dict[str, Any]]:
    """Transcripts in the LevelDB files and the sibling .blob folder."""
    trees: list[dict[str, Any]] = []
    paths: list[Path] = []
    if leveldb_root.is_dir():
        paths += sorted(p for p in leveldb_root.iterdir() if p.suffix in (".log", ".ldb"))
        blob = leveldb_root.with_name(leveldb_root.name.replace(".leveldb", ".blob"))
        if blob.is_dir():
            for directory, _, names in os.walk(blob):
                paths += sorted(Path(directory) / name for name in names)
    for path in paths:
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        data = native._source_stream(path, raw).data if path.suffix in (".log", ".ldb") else _blob_data(raw)
        trees.extend(transcripts(data))
    return trees


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _texts(blocks: object) -> list[str]:
    return [block["text"] for block in blocks or [] if isinstance(block, dict) and block.get("type") == "text"
            and isinstance(block.get("text"), str) and block["text"].strip()] if isinstance(blocks, list) else []


def _component(block: dict[str, Any]) -> tuple[str, str] | None:
    name = block.get("name")
    if not isinstance(name, str) or not name:
        return None
    if name.casefold() == "skill":
        payload = block.get("input") if isinstance(block.get("input"), dict) else {}
        for key in ("skill", "skill_name", "skillName", "name", "command"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return "skill", value.strip()
        return None
    kind, identity, _ = native._identity(name)
    return (kind, identity) if kind == "mcp_tool" else None


@dataclass
class ClaudeTurn:
    conversation: str
    turn_id: str
    started_at: datetime
    ended_at: datetime
    user_input: str | None
    final_output: str | None
    skills: list[str] = field(default_factory=list)
    mcp_tools: list[str] = field(default_factory=list)


def finished_turns(tree: dict[str, Any]) -> list[ClaudeTurn]:
    messages = [m for m in tree.get("messages", []) if isinstance(m, dict)]
    messages.sort(key=lambda m: m.get("index") if isinstance(m.get("index"), int) else 0)
    turns: list[ClaudeTurn] = []
    current: dict[str, Any] | None = None

    def close() -> None:
        if current is None or not current["assistant"]:
            return
        last = current["assistant"][-1]
        if not last.get("stop_reason"):
            return  # still being answered
        skills: list[str] = []
        mcp: list[str] = []
        for message in current["assistant"]:
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    component = _component(block)
                    if component and component[0] == "skill" and component[1] not in skills:
                        skills.append(component[1])
                    elif component and component[0] == "mcp_tool" and component[1] not in mcp:
                        mcp.append(component[1])
        if not skills and not mcp:
            return
        # The final reply is the text after the turn's last tool call (across all of
        # its assistant messages); without such text, the last text the assistant wrote.
        blocks = [b for message in current["assistant"] for b in message.get("content") or [] if isinstance(b, dict)]
        tools = [i for i, b in enumerate(blocks) if b.get("type") in ("tool_use", "tool_result")]
        tail = _texts(blocks[tools[-1] + 1:]) if tools else _texts(blocks)
        if not tail:
            texts = _texts(blocks)
            tail = texts[-1:]
        final = "\n\n".join(tail) or None
        human = current["human"]
        started = _time(human.get("created_at"))
        ended = _time(last.get("updated_at")) or _time(last.get("created_at")) or started
        if started is None or ended is None:
            return
        turns.append(ClaudeTurn(tree["conversationUuid"], str(human.get("uuid")), started, max(started, ended),
                                "\n\n".join(_texts(human.get("content"))) or None, final, skills, mcp))

    for message in messages:
        sender = message.get("sender")
        if sender == "human" and _texts(message.get("content")):
            close()
            current = {"human": message, "assistant": []}
        elif sender == "assistant" and current is not None:
            current["assistant"].append(message)
    close()
    return turns


def collect(leveldb_root: Path) -> list[ClaudeTurn]:
    """Finished turns of every cached conversation; the most complete copy of each wins."""
    best: dict[str, dict[str, Any]] = {}
    for tree in read_sources(leveldb_root):
        key = tree["conversationUuid"]
        if key not in best or len(tree["messages"]) >= len(best[key]["messages"]):
            best[key] = tree
    turns: list[ClaudeTurn] = []
    for tree in best.values():
        turns.extend(finished_turns(tree))
    return turns


__all__ = ["ClaudeTurn", "collect", "finished_turns", "read_sources", "transcripts"]
