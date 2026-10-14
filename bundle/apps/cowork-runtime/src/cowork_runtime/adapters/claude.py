"""Incremental, metadata-only reader for Claude Code project transcript JSONL."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cowork_contracts import ComponentEventV1

from .registry import CodexCursor, PluginRegistry


class ClaudeParseError(ValueError):
    """A Claude transcript cannot be safely parsed or resumed."""

    def __init__(self, message: str, *, path: str, offset: int) -> None:
        super().__init__(f"{message} (path={path}, offset={offset})")
        self.path = path
        self.offset = offset


@dataclass(frozen=True)
class ClaudeDiagnostic:
    code: str
    message: str
    path: str
    offset: int


@dataclass(frozen=True)
class ClaudeReadResult:
    events: tuple[ComponentEventV1, ...]
    next_cursor: CodexCursor
    diagnostics: tuple[ClaudeDiagnostic, ...]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return value.astimezone(timezone.utc)


def _parse_datetime(value: object, *, default: datetime) -> datetime:
    if value is None:
        return default
    if isinstance(value, datetime):
        return _as_utc(value)
    if not isinstance(value, str):
        raise ValueError("timestamp must be an RFC3339 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _as_utc(parsed)


def _file_identity(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_dev}:{stat.st_ino}"


def _stable_event_id(installation_id: str, session_id: str, tool_use_id: str, stage: str) -> str:
    key = f"{installation_id}:{session_id}:{tool_use_id}:{stage}"
    return f"claude:{hashlib.sha256(key.encode('utf-8')).hexdigest()}"


def _session_id(record: dict[str, Any]) -> str | None:
    value = record.get("sessionId") or record.get("session_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _tool_details(name: str) -> tuple[str, str]:
    if name.startswith("mcp__"):
        return "mcp_tool", "mcp_call"
    if name.casefold() == "skill":
        return "skill", "skill_call"
    return "tool", "tool_call"


def _usage(message: dict[str, Any]) -> tuple[int | None, int | None, int | None]:
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None, None, None
    values: list[int | None] = []
    for field in ("input_tokens", "output_tokens", "total_tokens"):
        value = usage.get(field)
        if value is None:
            values.append(None)
        elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"message.usage.{field} must be a non-negative integer when present")
        else:
            values.append(value)
    return values[0], values[1], values[2]


def _optional_bool(payload: dict[str, Any], field: str) -> bool:
    value = payload.get(field)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean when present")
    return value


def _text_content(content: object) -> str | None:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts = [block.get("text") for block in content if isinstance(block, dict) and isinstance(block.get("text"), str)]
    return "\n".join(parts) if parts else None


def _serialized_content(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class ClaudeIncrementalAdapter:
    """Read explicit Claude Code tool blocks without persisting transcript bodies."""

    def __init__(self, registry: PluginRegistry) -> None:
        self.registry = registry

    def read(
        self,
        path: str | Path,
        *,
        installation_id: str,
        session_id: str,
        run_id: str | None = None,
        now: datetime | None = None,
    ) -> ClaudeReadResult:
        source_path = Path(path).expanduser().resolve()
        if not source_path.is_file():
            raise ClaudeParseError("transcript path is not a regular file", path=str(source_path), offset=0)
        if not session_id.strip():
            raise ValueError("session_id must be non-empty")
        current_time = _as_utc(now or _utc_now())
        self.registry._require_active(installation_id)
        binding = self.registry.binding(installation_id)
        cursor = self.registry.cursor(installation_id, str(source_path))
        identity = _file_identity(source_path)
        size = source_path.stat().st_size
        if cursor is not None:
            if cursor.file_identity != identity:
                # Claude can replace an in-progress transcript atomically.  Replay
                # the replacement from byte zero; stable ComponentEvent IDs make
                # downstream delivery idempotent and retain newly added records.
                offset = 0
            elif size < cursor.offset:
                raise ClaudeParseError("transcript file was truncated", path=str(source_path), offset=cursor.offset)
            else:
                offset = cursor.offset
        else:
            offset = 0

        names: dict[str, str] = {}
        starts: dict[str, datetime] = {}
        usage_by_id: dict[str, tuple[int | None, int | None, int | None]] = {}
        if offset:
            self._rebuild_context(
                source_path,
                limit=offset,
                session_id=session_id,
                current_time=current_time,
                names=names,
                starts=starts,
                usage_by_id=usage_by_id,
            )

        events: list[ComponentEventV1] = []
        seen_ids: set[str] = set()
        latest_user_input: str | None = None
        pending_terminal_event_indexes: list[int] = []
        with source_path.open("rb") as handle:
            handle.seek(offset)
            while True:
                line_offset = handle.tell()
                raw_line = handle.readline()
                if not raw_line:
                    break
                next_offset = handle.tell()
                if not raw_line.endswith(b"\n"):
                    raise ClaudeParseError(
                        "transcript contains a truncated final line",
                        path=str(source_path),
                        offset=line_offset,
                    )
                record = self._decode_record(raw_line, path=source_path, offset=line_offset)
                try:
                    fresh_events = self._events_from_record(
                            record,
                            installation_id=installation_id,
                            session_id=session_id,
                            run_id=run_id or session_id,
                            current_time=current_time,
                            binding=binding,
                            names=names,
                            starts=starts,
                            usage_by_id=usage_by_id,
                            seen_ids=seen_ids,
                            user_input=latest_user_input,
                        )
                except ValueError as exc:
                    raise ClaudeParseError(str(exc), path=str(source_path), offset=line_offset) from exc
                start_index = len(events)
                events.extend(fresh_events)
                pending_terminal_event_indexes.extend(
                    index for index in range(start_index, len(events)) if events[index].stage != "started"
                )
                message = record.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    has_tool_result = isinstance(content, list) and any(
                        isinstance(block, dict) and block.get("type") == "tool_result" for block in content
                    )
                    text = _text_content(content)
                    if message.get("role") == "user" and not has_tool_result and text is not None:
                        latest_user_input = text
                        pending_terminal_event_indexes.clear()
                    elif message.get("role") == "assistant" and text is not None:
                        for index in pending_terminal_event_indexes:
                            events[index] = events[index].model_copy(update={"model_output": text})
                        pending_terminal_event_indexes.clear()
                offset = next_offset

        next_cursor = CodexCursor(
            installation_id=installation_id,
            source_path=str(source_path),
            file_identity=identity,
            offset=offset,
            updated_at=current_time,
        )
        self.registry.save_cursor(next_cursor)
        return ClaudeReadResult(events=tuple(events), next_cursor=next_cursor, diagnostics=())

    @staticmethod
    def _decode_record(raw_line: bytes, *, path: Path, offset: int) -> dict[str, Any]:
        try:
            record = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ClaudeParseError(f"invalid JSONL record: {exc}", path=str(path), offset=offset) from exc
        if not isinstance(record, dict):
            return {}
        return record

    def _rebuild_context(
        self,
        path: Path,
        *,
        limit: int,
        session_id: str,
        current_time: datetime,
        names: dict[str, str],
        starts: dict[str, datetime],
        usage_by_id: dict[str, tuple[int | None, int | None, int | None]],
    ) -> None:
        with path.open("rb") as handle:
            while handle.tell() < limit:
                line_offset = handle.tell()
                raw_line = handle.readline()
                if not raw_line or not raw_line.endswith(b"\n"):
                    raise ClaudeParseError("transcript context is truncated", path=str(path), offset=line_offset)
                record = self._decode_record(raw_line, path=path, offset=line_offset)
                try:
                    self._context_from_record(
                        record,
                        session_id=session_id,
                        current_time=current_time,
                        names=names,
                        starts=starts,
                        usage_by_id=usage_by_id,
                    )
                except ValueError as exc:
                    raise ClaudeParseError(str(exc), path=str(path), offset=line_offset) from exc

    @staticmethod
    def _context_from_record(
        record: dict[str, Any],
        *,
        session_id: str,
        current_time: datetime,
        names: dict[str, str],
        starts: dict[str, datetime],
        usage_by_id: dict[str, tuple[int | None, int | None, int | None]],
    ) -> None:
        if _session_id(record) not in {None, session_id}:
            return
        message = record.get("message")
        if not isinstance(message, dict):
            return
        content = message.get("content")
        if not isinstance(content, list):
            return
        occurred_at = _parse_datetime(record.get("timestamp"), default=current_time)
        usage = _usage(message)
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use":
                continue
            tool_id = block.get("id")
            name = block.get("name")
            if isinstance(tool_id, str) and tool_id.strip() and isinstance(name, str) and name.strip():
                canonical_tool_id = tool_id.strip()
                canonical_name = name.strip()
                existing_name = names.get(canonical_tool_id)
                if existing_name is not None and existing_name != canonical_name:
                    raise ValueError("tool_use id was reused for a different tool name")
                if existing_name is None:
                    names[canonical_tool_id] = canonical_name
                    starts[canonical_tool_id] = occurred_at
                    usage_by_id[canonical_tool_id] = usage

    def _events_from_record(
        self,
        record: dict[str, Any],
        *,
        installation_id: str,
        session_id: str,
        run_id: str,
        current_time: datetime,
        binding: Any,
        names: dict[str, str],
        starts: dict[str, datetime],
        usage_by_id: dict[str, tuple[int | None, int | None, int | None]],
        seen_ids: set[str],
        user_input: str | None,
    ) -> list[ComponentEventV1]:
        if _session_id(record) not in {None, session_id}:
            return []
        message = record.get("message")
        if not isinstance(message, dict):
            return []
        content = message.get("content")
        if not isinstance(content, list):
            return []
        occurred_at = _parse_datetime(record.get("timestamp"), default=current_time)
        usage = _usage(message)
        result: list[ComponentEventV1] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "tool_use":
                tool_id = block.get("id")
                name = block.get("name")
                if not isinstance(tool_id, str) or not tool_id.strip():
                    raise ValueError("tool_use.id must be a non-empty string")
                if not isinstance(name, str) or not name.strip():
                    raise ValueError("tool_use.name must be a non-empty string")
                tool_id = tool_id.strip()
                name = name.strip()
                existing_name = names.get(tool_id)
                if existing_name is not None and existing_name != name:
                    raise ValueError("tool_use id was reused for a different tool name")
                if existing_name is None:
                    names[tool_id] = name
                    starts[tool_id] = occurred_at
                    usage_by_id[tool_id] = usage
                result.append(
                    self._build_event(
                        installation_id,
                        session_id,
                        run_id,
                        binding,
                        tool_id,
                        name,
                        "started",
                        occurred_at,
                        starts[tool_id],
                        None,
                        None,
                        user_input,
                        None,
                    )
                )
            elif block_type == "tool_result":
                tool_id = block.get("tool_use_id")
                if not isinstance(tool_id, str) or not tool_id.strip():
                    raise ValueError("tool_result.tool_use_id must be a non-empty string")
                tool_id = tool_id.strip()
                name = names.get(tool_id)
                if name is None:
                    raise ValueError("tool_result has no matching tool_use")
                tool_use_result = record.get("toolUseResult")
                interrupted = (
                    _optional_bool(tool_use_result, "interrupted")
                    if isinstance(tool_use_result, dict)
                    else False
                )
                is_error = _optional_bool(block, "is_error")
                stage = "cancelled" if interrupted else ("failed" if is_error else "completed")
                input_tokens, output_tokens, total_tokens = usage_by_id.get(tool_id, (None, None, None))
                result.append(
                    self._build_event(
                        installation_id,
                        session_id,
                        run_id,
                        binding,
                        tool_id,
                        name,
                        stage,
                        occurred_at,
                        starts.get(tool_id),
                        "tool_error" if is_error else None,
                        (input_tokens, output_tokens, total_tokens),
                        user_input,
                        _serialized_content(block.get("content")),
                    )
                )
        fresh: list[ComponentEventV1] = []
        for event in result:
            if event.event_id in seen_ids:
                continue
            seen_ids.add(event.event_id)
            fresh.append(event)
        return fresh

    @staticmethod
    def _build_event(
        installation_id: str,
        session_id: str,
        run_id: str,
        binding: Any,
        tool_id: str,
        name: str,
        stage: str,
        occurred_at: datetime,
        started_at: datetime | None,
        error_code: str | None,
        usage: tuple[int | None, int | None, int | None] | None,
        user_input: str | None,
        tool_result: str | None,
    ) -> ComponentEventV1:
        component_kind, observation_kind = _tool_details(name)
        input_tokens, output_tokens, total_tokens = usage or (None, None, None)
        return ComponentEventV1(
            schema_version="component_event.v1",
            event_id=_stable_event_id(installation_id, session_id, tool_id, stage),
            invocation_id=tool_id,
            run_id=run_id,
            runtime="claude",
            plugin_id="claude-adapter",
            component_kind=component_kind,
            component_id=name,
            component_version=None,
            stage=stage,
            occurred_at=occurred_at,
            started_at=started_at,
            ended_at=occurred_at if stage != "started" else None,
            retry_count=None,
            error_code=error_code,
            observation_kind=observation_kind,
            evidence_source="claude_transcript",
            evidence_level="verified",
            parent_invocation_id=None,
            installation_id=installation_id,
            tenant_id=binding.tenant_id if binding is not None else None,
            user_id=binding.user_id if binding is not None else None,
            source_type="claude_project_jsonl",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            user_input=user_input,
            tool_result=tool_result,
        )


__all__ = ["ClaudeDiagnostic", "ClaudeIncrementalAdapter", "ClaudeParseError", "ClaudeReadResult"]
