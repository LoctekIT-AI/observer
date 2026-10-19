"""Incremental, metadata-only reader for structured Codex rollout JSONL."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cowork_contracts import ComponentEventV1

from .degraded import degraded_event
from .registry import CodexCursor, PluginRegistry


# 不代表任何 Skill/MCP 调用的工具记录：
# - tool_search_*：Codex 工具检索，只是在查找可用工具；
# - additional_tools：新会话开头声明的可用工具清单（只有工具名和说明，不是调用）。
_CODEX_NON_INVOCATION_TOOL_RECORDS = frozenset({"tool_search_call", "tool_search_output", "additional_tools"})


class CodexParseError(ValueError):
    """A rollout file cannot be safely parsed or resumed."""

    def __init__(self, message: str, *, path: str, offset: int) -> None:
        super().__init__(f"{message} (path={path}, offset={offset})")
        self.path = path
        self.offset = offset


@dataclass(frozen=True)
class CodexDiagnostic:
    code: str
    message: str
    path: str
    offset: int


@dataclass(frozen=True)
class CodexReadResult:
    events: tuple[ComponentEventV1, ...]
    next_cursor: CodexCursor
    diagnostics: tuple[CodexDiagnostic, ...]
    # Coverage self-check: raw records that look like a Skill read / an MCP call,
    # counted independently of the parsers above (see _coverage_signal).
    # Each entry is (kind, record time) with kind "skill" or "mcp".
    signals: tuple[tuple[str, datetime], ...] = ()


def _coverage_signal(record: dict[str, Any]) -> tuple[str, ...]:
    """Kinds ("skill", "mcp") a raw record looks like, judged without the event parsers.

    Deliberately simple and format-agnostic, so a client format change that
    makes the real parsers miss calls shows up as signals without events.
    Only call records are looked at (never outputs or tool lists).
    """
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else None
    if record.get("type") != "response_item" or payload is None:
        return ()
    kind = payload.get("type")
    if kind not in {"custom_tool_call", "function_call", "mcp_tool_call"}:
        return ()
    if kind == "mcp_tool_call" or str(payload.get("namespace") or "").startswith("mcp__") or str(payload.get("name") or "").startswith("mcp__"):
        return ("mcp",)
    body = payload.get("input") if kind == "custom_tool_call" else payload.get("arguments")
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, default=str) if body is not None else ""
    kinds: list[str] = []
    if "SKILL.md" in text and "skills" in text:
        kinds.append("skill")
    if "tools.mcp__" in text:
        kinds.append("mcp")
    return tuple(kinds)


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


def _stable_event_id(installation_id: str, session_id: str, call_id: str, stage: str) -> str:
    stable_key = f"{installation_id}:{session_id}:{call_id}:{stage}"
    return f"codex:{hashlib.sha256(stable_key.encode('utf-8')).hexdigest()}"


def _skill_selection_id(session_id: str, name: str, occurred_at: datetime, index: int) -> str:
    stable_key = f"{session_id}:skill-selection:{name}:{occurred_at.isoformat()}:{index}"
    return f"skill-selection:{hashlib.sha256(stable_key.encode('utf-8')).hexdigest()}"


def _skill_names_read_by_command(command: str) -> tuple[str, ...]:
    names: list[str] = []
    for segment in re.split(r"&&|\|\||[;|]", command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            continue
        if not tokens:
            continue
        executable_index = 1 if Path(tokens[0]).name == "command" and len(tokens) > 1 else 0
        if Path(tokens[executable_index]).name not in {"cat", "head", "tail", "less", "more", "sed"}:
            continue
        for token in tokens[executable_index + 1 :]:
            candidate = Path(token)
            if candidate.name != "SKILL.md" or "skills" not in candidate.parts:
                continue
            name = candidate.parent.name
            if name and name not in names:
                names.append(name)
    return tuple(names)


def _native_selected_skill_names(record: dict[str, Any], content: object) -> tuple[str, ...]:
    """Read the structured Skill selector marker emitted by current Desktop.

    Newer Desktop builds serialize the selected Skill as an ``input_text``
    block and mark the message with ``skills.selected_skill_instructions``.
    The marker is the native evidence; the instruction body is only parsed to
    recover the Skill id and its path is deliberately not retained.
    """
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else None
    metadata = (payload or {}).get("internal_chat_message_metadata_passthrough")
    kinds = metadata.get("content_item_kinds") if isinstance(metadata, dict) else None
    if not isinstance(kinds, list) or "skills.selected_skill_instructions" not in kinds:
        return ()
    if not isinstance(content, list):
        return ()
    names: list[str] = []
    pattern = re.compile(r"<skill>\s*<name>\s*([^<]+?)\s*</name>\s*<path>\s*([^<]+?)\s*</path>", re.DOTALL)
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "input_text":
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        match = pattern.search(text)
        if not match:
            continue
        name, path = match.group(1).strip(), match.group(2).strip()
        if not name or not path.startswith("/") or not path.endswith("/SKILL.md"):
            continue
        if name not in names:
            names.append(name)
    return tuple(names)


def _required_string(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"payload.{field} must be a non-empty string")
    return value.strip()


def _optional_string(payload: dict[str, Any], field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"payload.{field} must be a non-empty string when present")
    return value.strip()


def _optional_nonnegative_int(payload: dict[str, Any], field: str) -> int | None:
    value = payload.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"payload.{field} must be a non-negative integer when present")
    return value


def _usage(payload: dict[str, Any]) -> tuple[int | None, int | None, int | None]:
    usage = payload.get("usage")
    if usage is None:
        return (
            _optional_nonnegative_int(payload, "input_tokens"),
            _optional_nonnegative_int(payload, "output_tokens"),
            _optional_nonnegative_int(payload, "total_tokens"),
        )
    if not isinstance(usage, dict):
        raise ValueError("payload.usage must be an object")
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    total_tokens = usage.get("total_tokens")
    values: list[int | None] = []
    for name, value in (
        ("input_tokens", input_tokens),
        ("output_tokens", output_tokens),
        ("total_tokens", total_tokens),
    ):
        if value is None:
            values.append(None)
        elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"payload.usage.{name} must be a non-negative integer when present")
        else:
            values.append(value)
    return values[0], values[1], values[2]


def _record_timestamp(record: dict[str, Any], *, fallback: datetime) -> datetime:
    return _parse_datetime(record.get("timestamp"), default=fallback)


def _content_text(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts) if parts else None
    return None


def _safe_content(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value if len(value) <= 1_000_000 else None
    try:
        serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        return serialized if len(serialized) <= 1_000_000 else None
    except (TypeError, ValueError):
        return None


def _timestamp_from_milliseconds(payload: dict[str, Any], field: str) -> datetime:
    value = payload.get(field)
    if value is None:
        paired_field = {
            "started_at_ms": "completed_at_ms",
            "completed_at_ms": "started_at_ms",
            "startedAtMs": "completedAtMs",
            "completedAtMs": "startedAtMs",
        }.get(field)
        if paired_field is not None:
            value = payload.get(paired_field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"payload.{field} must be a non-negative integer")
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def _timestamp_from_seconds(payload: dict[str, Any], field: str) -> datetime:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"payload.{field} must be a non-negative number")
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _component_details(name: str) -> tuple[str, str]:
    if name.startswith("mcp__"):
        return "mcp_tool", "mcp_call"
    if name.casefold() == "skill":
        return "skill", "skill_call"
    return "tool", "tool_call"


def _terminal_stage(status: str) -> str:
    stage = {
        "success": "completed",
        "succeeded": "completed",
        "completed": "completed",
        "error": "failed",
        "failed": "failed",
        "timeout": "timeout",
        "timed_out": "timeout",
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "interrupted": "cancelled",
    }.get(status.lower())
    if stage is None:
        raise ValueError(f"unsupported custom tool call status: {status}")
    return stage


def _degraded_call(record: dict[str, Any]) -> tuple[str, str | None] | None:
    """(format hint, native call id) when an unreadable record looks like a call."""
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    candidates = [payload]
    if isinstance(payload.get("item"), dict):
        candidates.append(payload["item"])
    for candidate in candidates:
        kind = candidate.get("type")
        if not isinstance(kind, str):
            continue
        folded = kind.casefold()
        if folded.endswith("output") or folded.endswith("_end") or "result" in folded:
            return None  # the matching call record is what gets counted
        if "tool" in folded or "call" in folded:
            call = candidate.get("call_id") or candidate.get("id")
            return kind, call.strip() if isinstance(call, str) and call.strip() else None
    return None


def _reject_unknown_tool_record(record: dict[str, Any], *, path: str, offset: int) -> None:
    """Reject a new tool payload until its event semantics are implemented."""

    if record.get("type") != "response_item":
        return
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return
    payload_type = payload.get("type")
    if payload_type in _CODEX_NON_INVOCATION_TOOL_RECORDS:
        # Codex 的工具检索（tool_search）只是在查找可用工具，不是一次 Skill/MCP 调用，
        # 语义已确认，明确跳过；其他未知的 *tool* 记录仍然报错。
        return
    if (
        isinstance(payload_type, str)
        and "tool" in payload_type.casefold()
        and payload_type
        not in {
            "custom_tool_call",
            "custom_tool_call_output",
            "mcp_tool_call",
            "mcp_tool_call_output",
        }
    ):
        # 新工具格式必须先补齐事件语义；跳过它会让兼容性检查虚假成功。
        raise CodexParseError(
            "unsupported Codex tool record format", path=path, offset=offset
        )


class CodexIncrementalAdapter:
    """Read only new, explicitly structured tool records from a rollout file."""

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
        persist_cursor: bool = True,
    ) -> CodexReadResult:
        source_path = Path(path).expanduser().resolve()
        if not source_path.is_file():
            raise CodexParseError("rollout path is not a regular file", path=str(source_path), offset=0)
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
                # Codex can replace an in-progress rollout atomically.  Replay the
                # replacement from byte zero; ComponentEvent IDs are stable across
                # the old and replacement records, so downstream delivery remains
                # idempotent while any newly written records are observed.
                offset = 0
            elif size < cursor.offset:
                raise CodexParseError("rollout file was truncated", path=str(source_path), offset=cursor.offset)
            else:
                offset = cursor.offset
        else:
            offset = 0

        events: list[ComponentEventV1] = []
        diagnostics: list[CodexDiagnostic] = []
        signals: list[tuple[str, datetime]] = []
        seen_keys: set[str] = set()
        started_at_by_call: dict[str, datetime] = {}
        name_by_call: dict[str, str] = {}
        terminal_status_by_call: dict[str, str] = {}
        native_terminal_calls: set[str] = set()
        latest_user_input: str | None = None
        latest_model_output: str | None = None
        pending_terminal_event_indexes: list[int] = []
        native_thread_id: str | None = None
        app_server_skills_by_turn: dict[str, list[tuple[str, str, datetime]]] = {}
        if offset:
            native_thread_id, latest_user_input, latest_model_output = self._rebuild_context(
                source_path,
                limit=offset,
                session_id=session_id,
                current_time=current_time,
                started_at_by_call=started_at_by_call,
                name_by_call=name_by_call,
                terminal_status_by_call=terminal_status_by_call,
                native_terminal_calls=native_terminal_calls,
                app_server_skills_by_turn=app_server_skills_by_turn,
            )
        with source_path.open("rb") as handle:
            handle.seek(offset)
            while True:
                line_offset = handle.tell()
                raw_line = handle.readline()
                if not raw_line:
                    break
                next_offset = handle.tell()
                if not raw_line.endswith(b"\n"):
                    raise CodexParseError("rollout contains a truncated final line", path=str(source_path), offset=line_offset)
                try:
                    line = raw_line.decode("utf-8")
                    if line.strip() == "Reading additional input from stdin...":
                        offset = next_offset
                        continue
                    record = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    # A complete line that is not JSON cannot be repaired; skip it
                    # (reported in diagnostics) instead of blocking the session.
                    diagnostics.append(CodexDiagnostic("malformed_record", "skipped a line that is not JSON", str(source_path), line_offset))
                    offset = next_offset
                    continue
                if not isinstance(record, dict):
                    continue
                signal_kinds = _coverage_signal(record)
                if signal_kinds:
                    try:
                        signal_at = _record_timestamp(record, fallback=current_time)
                    except ValueError:
                        signal_at = current_time
                    signals.extend((kind, signal_at) for kind in signal_kinds)
                try:
                    _reject_unknown_tool_record(record, path=str(source_path), offset=line_offset)
                    if record.get("type") == "thread.started":
                        candidate_thread_id = record.get("thread_id")
                        if isinstance(candidate_thread_id, str) and candidate_thread_id.strip():
                            native_thread_id = candidate_thread_id.strip()
                    native_exec_events = self._native_exec_events_from_record(
                        record,
                        installation_id=installation_id,
                        session_id=session_id,
                        run_id=run_id or session_id,
                        current_time=current_time,
                        binding=binding,
                        started_at_by_call=started_at_by_call,
                        name_by_call=name_by_call,
                        seen_keys=seen_keys,
                        stream_matches=(native_thread_id is None or native_thread_id == session_id),
                    )
                    item_events = self._item_completed_events_from_record(
                        record,
                        installation_id=installation_id,
                        session_id=session_id,
                        run_id=run_id or session_id,
                        binding=binding,
                        seen_keys=seen_keys,
                        latest_user_input=latest_user_input,
                        latest_model_output=latest_model_output,
                        native_terminal_calls=native_terminal_calls,
                    )
                    skill_events = self._skill_events_from_record(
                        record,
                        installation_id=installation_id,
                        session_id=session_id,
                        run_id=run_id or session_id,
                        current_time=current_time,
                        binding=binding,
                        seen_keys=seen_keys,
                    )
                    app_server_skill_events = self._app_server_skill_events_from_record(
                        record,
                        installation_id=installation_id,
                        session_id=session_id,
                        run_id=run_id or session_id,
                        current_time=current_time,
                        binding=binding,
                        seen_keys=seen_keys,
                        skills_by_turn=app_server_skills_by_turn,
                    )
                    app_server_mcp_events = self._app_server_mcp_events_from_record(
                        record,
                        installation_id=installation_id,
                        session_id=session_id,
                        run_id=run_id or session_id,
                        binding=binding,
                        started_at_by_call=started_at_by_call,
                        name_by_call=name_by_call,
                        seen_keys=seen_keys,
                    )
                    event = self._event_from_record(
                        record,
                        installation_id=installation_id,
                        session_id=session_id,
                        run_id=run_id or session_id,
                        current_time=current_time,
                        binding=binding,
                        started_at_by_call=started_at_by_call,
                        name_by_call=name_by_call,
                        terminal_status_by_call=terminal_status_by_call,
                        native_terminal_calls=native_terminal_calls,
                        seen_keys=seen_keys,
                    )
                except ValueError as exc:
                    # A record this adapter version cannot read (usually a new
                    # Codex format). Count it as an unrecognized call when it
                    # looks like an invocation, report it, and keep going.
                    diagnostics.append(CodexDiagnostic("unrecognized_record", type(exc).__name__, str(source_path), line_offset))
                    degraded = _degraded_call(record)
                    if degraded is not None:
                        hint, native_call = degraded
                        events.append(degraded_event(
                            runtime="codex", plugin_id="codex-adapter", installation_id=installation_id,
                            session_id=session_id, run_id=run_id or session_id,
                            native_call=native_call or f"{source_path.name}:{line_offset}", hint=hint,
                            occurred_at=_parse_datetime(record.get("timestamp"), default=current_time),
                            source_type="rollout_jsonl", evidence_source="codex_rollout_unrecognized", binding=binding,
                        ))
                    offset = next_offset
                    continue
                item_event_start = len(events)
                events.extend(native_exec_events)
                events.extend(item_events)
                pending_terminal_event_indexes.extend(
                    index
                    for index in range(item_event_start, len(events))
                    if events[index].stage != "started" and events[index].model_output is None
                )
                events.extend(skill_events)
                events.extend(app_server_skill_events)
                events.extend(app_server_mcp_events)
                if event is not None:
                    events.append(event)
                if record.get("type") == "event_msg" and isinstance(record.get("payload"), dict):
                    item = record["payload"].get("item")
                    if isinstance(item, dict):
                        text = _content_text(item.get("content"))
                        if item.get("type") == "UserMessage" and text is not None:
                            latest_user_input = text
                            latest_model_output = None
                            pending_terminal_event_indexes.clear()
                        elif item.get("type") == "AgentMessage" and text is not None:
                            latest_model_output = text
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
        if persist_cursor:
            self.registry.save_cursor(next_cursor)
        return CodexReadResult(events=tuple(events), next_cursor=next_cursor, diagnostics=tuple(diagnostics),
                               signals=tuple(signals))

    @staticmethod
    def _rebuild_context(
        path: Path,
        *,
        limit: int,
        session_id: str,
        current_time: datetime,
        started_at_by_call: dict[str, datetime],
        name_by_call: dict[str, str],
        terminal_status_by_call: dict[str, str],
        native_terminal_calls: set[str],
        app_server_skills_by_turn: dict[str, list[tuple[str, str, datetime]]],
    ) -> tuple[str | None, str | None, str | None]:
        """Restore correlation state that may precede an incremental cursor."""

        native_thread_id: str | None = None
        latest_user_input: str | None = None
        latest_model_output: str | None = None
        with path.open("rb") as handle:
            while handle.tell() < limit:
                line_offset = handle.tell()
                raw_line = handle.readline()
                if not raw_line or not raw_line.endswith(b"\n"):
                    raise CodexParseError("rollout context is truncated", path=str(path), offset=line_offset)
                try:
                    record = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue  # reported by read() when the line was first consumed
                if not isinstance(record, dict):
                    continue

                if record.get("type") == "thread.started":
                    candidate = record.get("thread_id")
                    if isinstance(candidate, str) and candidate.strip():
                        native_thread_id = candidate.strip()

                if record.get("type") == "response_item":
                    payload = record.get("payload")
                    if isinstance(payload, dict):
                        source_session = payload.get("session_id")
                        if source_session is None or source_session == session_id:
                            payload_type = payload.get("type")
                            try:
                                _reject_unknown_tool_record(record, path=str(path), offset=line_offset)
                            except CodexParseError:
                                continue  # counted as unrecognized by read()
                            call_id = payload.get("call_id")
                            if isinstance(call_id, str) and call_id.strip():
                                call_id = call_id.strip()
                                if payload_type in {"custom_tool_call", "function_call"}:
                                    name = payload.get("name")
                                    if payload_type != "function_call" or (
                                        isinstance(payload.get("namespace"), str)
                                        and payload["namespace"].startswith("mcp__")
                                    ):
                                        if isinstance(name, str) and name.strip():
                                            if payload_type == "function_call" and payload.get("namespace"):
                                                name = f"{payload['namespace']}__{name.strip()}"
                                            name_by_call[call_id] = name.strip()
                                            occurred_at = _parse_datetime(
                                                payload.get("occurred_at"),
                                                default=_record_timestamp(record, fallback=current_time),
                                            )
                                            started_at_by_call[call_id] = _parse_datetime(
                                                payload.get("started_at"), default=occurred_at
                                            )
                                            status = _optional_string(payload, "status")
                                            if status is not None:
                                                terminal_status_by_call[call_id] = status
                                elif payload_type == "custom_tool_call_output":
                                    status = _optional_string(payload, "status")
                                    if status is not None and call_id in name_by_call:
                                        terminal_status_by_call[call_id] = status
                        if payload.get("type") == "message":
                            text = _content_text(payload.get("content"))
                            if text is not None:
                                if payload.get("role") == "user":
                                    latest_user_input = text
                                    latest_model_output = None
                                elif payload.get("role") == "assistant":
                                    latest_model_output = text

                if record.get("type") == "event_msg" and isinstance(record.get("payload"), dict):
                    item = record["payload"].get("item")
                    if isinstance(item, dict):
                        if record["payload"].get("type") == "item_completed" and item.get("type") == "McpToolCall":
                            _terminal_stage(_required_string(item, "status"))
                            native_terminal_calls.add(_required_string(item, "id"))
                        text = _content_text(item.get("content"))
                        if item.get("type") == "UserMessage" and text is not None:
                            latest_user_input = text
                            latest_model_output = None
                        elif item.get("type") == "AgentMessage" and text is not None:
                            latest_model_output = text

                if record.get("type") in {"item.started", "item.completed"} and native_thread_id == session_id:
                    item = record.get("item")
                    if isinstance(item, dict) and item.get("type") == "mcp_tool_call":
                        call_id = item.get("id")
                        server = item.get("server")
                        tool = item.get("tool")
                        if all(isinstance(value, str) and value.strip() for value in (call_id, server, tool)):
                            component_id = f"mcp__{server.strip()}__{tool.strip()}"
                            if record["type"] == "item.started":
                                name_by_call[call_id.strip()] = component_id
                                started_at_by_call[call_id.strip()] = current_time

                method = record.get("method")
                params = record.get("params")
                if isinstance(params, dict) and params.get("threadId") == session_id:
                    item = params.get("item")
                    if method == "item/started" and isinstance(item, dict) and item.get("type") == "mcpToolCall":
                        call_id = _required_string(item, "id")
                        server = _required_string(item, "server")
                        tool = _required_string(item, "tool")
                        name_by_call[call_id] = f"mcp__{server}__{tool}"
                        started_at_by_call[call_id] = _timestamp_from_milliseconds(params, "startedAtMs")
                    if method == "item/started" and isinstance(item, dict) and item.get("type") == "userMessage":
                        turn_id = _required_string(params, "turnId")
                        message_id = _required_string(item, "id")
                        content = item.get("content")
                        if isinstance(content, list):
                            occurred_at = _timestamp_from_milliseconds(params, "startedAtMs")
                            pending = app_server_skills_by_turn.setdefault(turn_id, [])
                            for index, content_item in enumerate(content):
                                if not isinstance(content_item, dict) or content_item.get("type") != "skill":
                                    continue
                                name = _required_string(content_item, "name")
                                _required_string(content_item, "path")
                                pending.append((f"{message_id}:skill:{index}", name, occurred_at))
                    if method == "turn/completed":
                        turn = params.get("turn")
                        if isinstance(turn, dict):
                            app_server_skills_by_turn.pop(_required_string(turn, "id"), None)
        return native_thread_id, latest_user_input, latest_model_output

    def _native_exec_events_from_record(
        self,
        record: dict[str, Any],
        *,
        installation_id: str,
        session_id: str,
        run_id: str,
        current_time: datetime,
        binding: Any,
        started_at_by_call: dict[str, datetime],
        name_by_call: dict[str, str],
        seen_keys: set[str],
        stream_matches: bool,
    ) -> tuple[ComponentEventV1, ...]:
        """Map native ``codex exec --json`` item lifecycle records.

        The non-interactive CLI emits ``item.started``/``item.completed`` at
        the top level rather than the persisted rollout ``response_item``
        shape.  These records are accepted only from the requested native
        thread, so a task-bound source cannot leak another session's calls.
        """

        if not stream_matches or record.get("type") not in {"item.started", "item.completed"}:
            return ()
        item = record.get("item")
        if not isinstance(item, dict) or item.get("type") != "mcp_tool_call":
            return ()
        call_id = item.get("id")
        server = item.get("server")
        tool = item.get("tool")
        if not all(isinstance(value, str) and value.strip() for value in (call_id, server, tool)):
            return ()
        call_id, server, tool = call_id.strip(), server.strip(), tool.strip()
        component_id = f"mcp__{server}__{tool}"
        occurred_at = current_time
        if record.get("type") == "item.started":
            name_by_call[call_id] = component_id
            started_at_by_call[call_id] = occurred_at
            stage = "started"
            ended_at = None
        else:
            if name_by_call.get(call_id) != component_id:
                return ()
            raw_status = item.get("status")
            stage = _terminal_stage(raw_status) if isinstance(raw_status, str) and raw_status.strip() else "completed"
            ended_at = occurred_at
        event_id = _stable_event_id(installation_id, session_id, call_id, stage)
        if event_id in seen_keys:
            return ()
        seen_keys.add(event_id)
        started_at = started_at_by_call.get(call_id, occurred_at)
        return (ComponentEventV1(
            schema_version="component_event.v1",
            event_id=event_id,
            invocation_id=call_id,
            run_id=run_id,
            runtime="codex",
            plugin_id="codex-adapter",
            component_kind="mcp_tool",
            component_id=component_id,
            stage=stage,
            occurred_at=occurred_at,
            started_at=started_at,
            ended_at=ended_at,
            error_code=("tool_error" if stage == "failed" else None),
            observation_kind="mcp_call",
            evidence_source="codex_exec_json",
            evidence_level="verified",
            installation_id=installation_id,
            tenant_id=binding.tenant_id if binding is not None else None,
            user_id=binding.user_id if binding is not None else None,
            source_type="rollout_jsonl",
            tool_result=_safe_content(item.get("result")) if stage not in {"started"} else None,
        ),)

    def _item_completed_events_from_record(
        self,
        record: dict[str, Any],
        *,
        installation_id: str,
        session_id: str,
        run_id: str,
        binding: Any,
        seen_keys: set[str],
        latest_user_input: str | None,
        latest_model_output: str | None,
        native_terminal_calls: set[str],
    ) -> tuple[ComponentEventV1, ...]:
        if record.get("type") != "event_msg":
            return ()
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "item_completed":
            return ()
        item = payload.get("item")
        if not isinstance(item, dict):
            return ()
        if item.get("type") == "CommandExecution":
            call_id = _required_string(item, "id")
            if _terminal_stage(_required_string(item, "status")) != "completed":
                return ()
            command = item.get("command")
            if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
                raise ValueError("payload.item.command must be a non-empty string array")
            occurred_at = _timestamp_from_milliseconds(payload, "started_at_ms")
            events: list[ComponentEventV1] = []
            for index, name in enumerate(_skill_names_read_by_command(command[-1])):
                invocation_id = f"{call_id}:skill-observation:{index}"
                event_id = _stable_event_id(installation_id, session_id, invocation_id, "completed")
                if event_id in seen_keys:
                    continue
                seen_keys.add(event_id)
                events.append(
                    ComponentEventV1(
                        schema_version="component_event.v1",
                        event_id=event_id,
                        invocation_id=invocation_id,
                        run_id=run_id,
                        runtime="codex",
                        plugin_id="codex-adapter",
                        component_kind="skill",
                        component_id=name,
                        stage="completed",
                        occurred_at=occurred_at,
                        started_at=occurred_at,
                        ended_at=occurred_at,
                        observation_kind="skill_file_read",
                        evidence_source="codex_command_execution",
                        evidence_level="inferred",
                        installation_id=installation_id,
                        tenant_id=binding.tenant_id if binding is not None else None,
                        user_id=binding.user_id if binding is not None else None,
                        source_type="rollout_jsonl",
                        user_input=latest_user_input,
                        model_output=latest_model_output,
                        tool_result=_safe_content(item.get("aggregated_output")),
                    )
                )
            return tuple(events)
        if item.get("type") != "McpToolCall":
            return ()

        call_id = _required_string(item, "id")
        server = _required_string(item, "server")
        tool = _required_string(item, "tool")
        terminal_stage = _terminal_stage(_required_string(item, "status"))
        started_at = _timestamp_from_milliseconds(payload, "started_at_ms")
        completed_at = _timestamp_from_milliseconds(payload, "completed_at_ms")
        if completed_at < started_at:
            raise ValueError("payload.completed_at_ms must not precede payload.started_at_ms")

        # 原生 McpToolCall 已给出终态；后续 output 只是同次调用的另一种表示，
        # 不能因其缺少 status 又将 failed 等终态覆盖为 completed。
        native_terminal_calls.add(call_id)

        component_id = f"mcp__{server}__{tool}"
        events: list[ComponentEventV1] = []
        for stage, occurred_at, ended_at in (
            ("started", started_at, None),
            (terminal_stage, completed_at, completed_at),
        ):
            event_id = _stable_event_id(installation_id, session_id, call_id, stage)
            if event_id in seen_keys:
                continue
            seen_keys.add(event_id)
            events.append(
                ComponentEventV1(
                    schema_version="component_event.v1",
                    event_id=event_id,
                    invocation_id=call_id,
                    run_id=run_id,
                    runtime="codex",
                    plugin_id="codex-adapter",
                    component_kind="mcp_tool",
                    component_id=component_id,
                    stage=stage,
                    occurred_at=occurred_at,
                    started_at=started_at,
                    ended_at=ended_at,
                    observation_kind="mcp_call",
                    evidence_source="codex_rollout",
                    evidence_level="verified",
                    installation_id=installation_id,
                    tenant_id=binding.tenant_id if binding is not None else None,
                    user_id=binding.user_id if binding is not None else None,
                    source_type="rollout_jsonl",
                    user_input=latest_user_input,
                    model_output=latest_model_output,
                    tool_result=_safe_content(item.get("result")) if stage == terminal_stage else None,
                )
            )
        return tuple(events)

    def _app_server_skill_events_from_record(
        self,
        record: dict[str, Any],
        *,
        installation_id: str,
        session_id: str,
        run_id: str,
        current_time: datetime,
        binding: Any,
        seen_keys: set[str],
        skills_by_turn: dict[str, list[tuple[str, str, datetime]]],
    ) -> tuple[ComponentEventV1, ...]:
        method = record.get("method")
        if method not in {"item/started", "turn/completed"}:
            return ()
        params = record.get("params")
        if not isinstance(params, dict) or params.get("threadId") != session_id:
            return ()

        if method == "item/started":
            turn_id = _required_string(params, "turnId")
            item = params.get("item")
            if not isinstance(item, dict) or item.get("type") != "userMessage":
                return ()
            message_id = _required_string(item, "id")
            content = item.get("content")
            if not isinstance(content, list):
                return ()
            occurred_at = _timestamp_from_milliseconds(params, "startedAtMs")
            pending = skills_by_turn.setdefault(turn_id, [])
            events: list[ComponentEventV1] = []
            for index, content_item in enumerate(content):
                if not isinstance(content_item, dict) or content_item.get("type") != "skill":
                    continue
                name = _required_string(content_item, "name")
                _required_string(content_item, "path")
                invocation_id = f"{message_id}:skill:{index}"
                pending.append((invocation_id, name, occurred_at))
                event = self._app_server_skill_event(
                    installation_id=installation_id,
                    session_id=session_id,
                    run_id=run_id,
                    binding=binding,
                    invocation_id=invocation_id,
                    name=name,
                    stage="started",
                    occurred_at=occurred_at,
                    started_at=occurred_at,
                    seen_keys=seen_keys,
                )
                if event is not None:
                    events.append(event)
            return tuple(events)

        turn = params.get("turn")
        if not isinstance(turn, dict):
            return ()
        turn_id = _required_string(turn, "id")
        pending = skills_by_turn.pop(turn_id, None)
        if not pending:
            return ()
        stage = _terminal_stage(_required_string(turn, "status"))
        occurred_at = (
            _timestamp_from_seconds(turn, "completedAt")
            if turn.get("completedAt") is not None
            else current_time
        )
        events = []
        for invocation_id, name, started_at in pending:
            event = self._app_server_skill_event(
                installation_id=installation_id,
                session_id=session_id,
                run_id=run_id,
                binding=binding,
                invocation_id=invocation_id,
                name=name,
                stage=stage,
                occurred_at=occurred_at,
                started_at=started_at,
                seen_keys=seen_keys,
            )
            if event is not None:
                events.append(event)
        return tuple(events)

    def _app_server_mcp_events_from_record(
        self,
        record: dict[str, Any],
        *,
        installation_id: str,
        session_id: str,
        run_id: str,
        binding: Any,
        started_at_by_call: dict[str, datetime],
        name_by_call: dict[str, str],
        seen_keys: set[str],
    ) -> tuple[ComponentEventV1, ...]:
        method = record.get("method")
        if method not in {"item/started", "item/completed"}:
            return ()
        params = record.get("params")
        if not isinstance(params, dict) or params.get("threadId") != session_id:
            return ()
        item = params.get("item")
        if not isinstance(item, dict) or item.get("type") != "mcpToolCall":
            return ()

        call_id = _required_string(item, "id")
        server = _required_string(item, "server")
        tool = _required_string(item, "tool")
        component_id = f"mcp__{server}__{tool}"
        if method == "item/started":
            occurred_at = _timestamp_from_milliseconds(params, "startedAtMs")
            started_at_by_call[call_id] = occurred_at
            name_by_call[call_id] = component_id
            stage = "started"
        else:
            if name_by_call.get(call_id) != component_id:
                return ()
            occurred_at = _timestamp_from_milliseconds(params, "completedAtMs")
            stage = _terminal_stage(_required_string(item, "status"))

        event_id = _stable_event_id(installation_id, session_id, call_id, stage)
        if event_id in seen_keys:
            return ()
        seen_keys.add(event_id)
        started_at = started_at_by_call.get(call_id, occurred_at)
        return (ComponentEventV1(
            schema_version="component_event.v1",
            event_id=event_id,
            invocation_id=call_id,
            run_id=run_id,
            runtime="codex",
            plugin_id="codex-adapter",
            component_kind="mcp_tool",
            component_id=component_id,
            stage=stage,
            occurred_at=occurred_at,
            started_at=started_at,
            ended_at=occurred_at if stage != "started" else None,
            error_code="tool_error" if stage == "failed" else None,
            observation_kind="mcp_call",
            evidence_source="codex_app_server",
            evidence_level="verified",
            installation_id=installation_id,
            tenant_id=binding.tenant_id if binding is not None else None,
            user_id=binding.user_id if binding is not None else None,
            source_type="rollout_jsonl",
        ),)

    def _app_server_skill_event(
        self,
        *,
        installation_id: str,
        session_id: str,
        run_id: str,
        binding: Any,
        invocation_id: str,
        name: str,
        stage: str,
        occurred_at: datetime,
        started_at: datetime,
        seen_keys: set[str],
    ) -> ComponentEventV1 | None:
        event_id = _stable_event_id(installation_id, session_id, invocation_id, stage)
        if event_id in seen_keys:
            return None
        seen_keys.add(event_id)
        return ComponentEventV1(
            schema_version="component_event.v1",
            event_id=event_id,
            invocation_id=invocation_id,
            run_id=run_id,
            runtime="codex",
            plugin_id="codex-adapter",
            component_kind="skill",
            component_id=name,
            stage=stage,
            occurred_at=occurred_at,
            started_at=started_at,
            ended_at=occurred_at if stage != "started" else None,
            error_code="turn_failed" if stage == "failed" else None,
            observation_kind="skill_call",
            evidence_source="codex_app_server",
            evidence_level="verified",
            installation_id=installation_id,
            tenant_id=binding.tenant_id if binding is not None else None,
            user_id=binding.user_id if binding is not None else None,
            source_type="rollout_jsonl",
        )

    def _skill_events_from_record(
        self,
        record: dict[str, Any],
        *,
        installation_id: str,
        session_id: str,
        run_id: str,
        current_time: datetime,
        binding: Any,
        seen_keys: set[str],
    ) -> tuple[ComponentEventV1, ...]:
        record_type = record.get("type")
        if record_type not in {"response_item", "event_msg"}:
            return ()
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return ()
        occurred_at = _record_timestamp(record, fallback=current_time)
        observations: list[tuple[str, str, str, str]] = []
        source_item_id: str | None = None
        if payload.get("type") == "message" and payload.get("role") == "user":
            content = payload.get("content")
        elif record_type == "event_msg" and payload.get("type") == "item_completed":
            item = payload.get("item")
            if not isinstance(item, dict) or item.get("type") != "UserMessage":
                return ()
            content = item.get("content")
            source_item_id = item.get("id") if isinstance(item.get("id"), str) else None
            occurred_at = _timestamp_from_milliseconds(payload, "started_at_ms")
        else:
            content = None
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "skill":
                    continue
                name = _required_string(item, "name")
                _required_string(item, "path")
                observations.append((name, "skill_selected", "codex_skill_user_input", "partial"))
            for name in _native_selected_skill_names(record, content):
                observations.append((name, "skill_selected", "codex_skill_user_input", "partial"))
        if record_type == "response_item" and payload.get("type") == "function_call" and payload.get("name") in {"exec_command", "shell_command"}:
            arguments = payload.get("arguments")
            try:
                parsed_arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
            except json.JSONDecodeError:
                parsed_arguments = None
            if isinstance(parsed_arguments, dict):
                command = parsed_arguments.get("cmd") or parsed_arguments.get("command")
                if isinstance(command, str):
                    for name in _skill_names_read_by_command(command):
                        observations.append((name, "skill_file_read", "codex_tool_arguments", "inferred"))
        elif record_type == "response_item" and payload.get("type") == "message" and payload.get("role") != "user":
            return ()

        events: list[ComponentEventV1] = []
        for index, (name, observation_kind, evidence_source, evidence_level) in enumerate(observations):
            source_call_id = payload.get("call_id") or source_item_id
            if isinstance(source_call_id, str) and source_call_id.strip():
                invocation_id = f"{source_call_id}:skill-observation:{index}"
            else:
                invocation_id = _skill_selection_id(session_id, name, occurred_at, index)
            event_id = _stable_event_id(installation_id, session_id, invocation_id, "completed")
            if event_id in seen_keys:
                continue
            seen_keys.add(event_id)
            events.append(
                ComponentEventV1(
                    schema_version="component_event.v1",
                    event_id=event_id,
                    invocation_id=invocation_id,
                    run_id=run_id,
                    runtime="codex",
                    plugin_id="codex-adapter",
                    component_kind="skill",
                    component_id=name,
                    stage="completed",
                    occurred_at=occurred_at,
                    started_at=occurred_at,
                    ended_at=occurred_at,
                    observation_kind=observation_kind,
                    evidence_source=evidence_source,
                    evidence_level=evidence_level,
                    installation_id=installation_id,
                    tenant_id=binding.tenant_id if binding is not None else None,
                    user_id=binding.user_id if binding is not None else None,
                    source_type="rollout_jsonl",
                )
            )
        return tuple(events)

    def _event_from_record(
        self,
        record: dict[str, Any],
        *,
        installation_id: str,
        session_id: str,
        run_id: str,
        current_time: datetime,
        binding: Any,
        started_at_by_call: dict[str, datetime],
        name_by_call: dict[str, str],
        terminal_status_by_call: dict[str, str],
        native_terminal_calls: set[str],
        seen_keys: set[str],
    ) -> ComponentEventV1 | None:
        if record.get("type") != "response_item":
            return None
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return None
        payload_type = payload.get("type")
        if payload_type not in {
            "custom_tool_call",
            "custom_tool_call_output",
            "function_call",
            "function_call_output",
        }:
            return None
        if payload_type == "function_call":
            namespace = payload.get("namespace")
            if not isinstance(namespace, str) or not namespace.startswith("mcp__"):
                return None
        if payload_type == "function_call_output":
            output_call_id = payload.get("call_id")
            if not isinstance(output_call_id, str) or output_call_id not in name_by_call:
                return None
        source_session = payload.get("session_id")
        if source_session is not None and source_session != session_id:
            return None
        call_id = _required_string(payload, "call_id")
        if payload_type in {"custom_tool_call_output", "function_call_output"} and call_id in native_terminal_calls:
            return None
        recorded_at = _record_timestamp(record, fallback=current_time)
        if payload_type in {"custom_tool_call", "function_call"}:
            name = _required_string(payload, "name")
            if payload_type == "function_call":
                namespace = _optional_string(payload, "namespace")
                if namespace:
                    name = f"{namespace}__{name}"
            name_by_call[call_id] = name
            stage = "started"
            occurred_at = _parse_datetime(payload.get("occurred_at"), default=recorded_at)
            started_at = _parse_datetime(payload.get("started_at"), default=occurred_at)
            ended_at = None
            started_at_by_call[call_id] = started_at
            status = _optional_string(payload, "status")
            if status is not None:
                terminal_status_by_call[call_id] = status
            error_code = None
        else:
            name = _optional_string(payload, "name") or name_by_call.get(call_id)
            if name is None:
                raise ValueError("custom_tool_call_output has no matching tool start")
            status = _optional_string(payload, "status") or terminal_status_by_call.get(call_id)
            if payload_type == "function_call_output":
                stage = _terminal_stage(status) if status is not None else "completed"
            else:
                if status is None:
                    raise ValueError("custom_tool_call_output has no status on itself or matching tool start")
                stage = _terminal_stage(status)
            occurred_at = _parse_datetime(payload.get("occurred_at"), default=recorded_at)
            started_at = started_at_by_call.get(call_id)
            ended_at = occurred_at
            error_code = _optional_string(payload, "error_code")
        component_kind, observation_kind = _component_details(name)
        event_id = _stable_event_id(installation_id, session_id, call_id, stage)
        if event_id in seen_keys:
            return None
        seen_keys.add(event_id)
        input_tokens, output_tokens, total_tokens = _usage(payload)
        return ComponentEventV1(
            schema_version="component_event.v1",
            event_id=event_id,
            invocation_id=call_id,
            run_id=run_id,
            runtime="codex",
            plugin_id="codex-adapter",
            component_kind=component_kind,
            component_id=name,
            component_version=_optional_string(payload, "component_version") or _optional_string(payload, "version"),
            stage=stage,
            occurred_at=occurred_at,
            started_at=started_at,
            ended_at=ended_at,
            retry_count=_optional_nonnegative_int(payload, "retry_count"),
            error_code=error_code,
            observation_kind=observation_kind,
            evidence_source="codex_rollout",
            evidence_level="verified",
            parent_invocation_id=_optional_string(payload, "parent_invocation_id"),
            installation_id=installation_id,
            tenant_id=binding.tenant_id if binding is not None else None,
            user_id=binding.user_id if binding is not None else None,
            source_type="rollout_jsonl",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )


__all__ = [
    "CodexDiagnostic",
    "CodexIncrementalAdapter",
    "CodexParseError",
    "CodexReadResult",
]
