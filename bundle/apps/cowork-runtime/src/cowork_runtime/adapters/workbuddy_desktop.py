"""Incremental reader for official WorkBuddy Code command-hook JSONL evidence."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cowork_contracts import ComponentEventV1

from .registry import CodexCursor, PluginRegistry


class WorkBuddyParseError(ValueError):
    """A WorkBuddy hook evidence file cannot be safely parsed or resumed."""

    def __init__(self, message: str, *, path: str, offset: int) -> None:
        super().__init__(f"{message} (path={path}, offset={offset})")
        self.path = path
        self.offset = offset


@dataclass(frozen=True)
class WorkBuddyReadResult:
    events: tuple[ComponentEventV1, ...]
    next_cursor: CodexCursor
    diagnostics: tuple[str, ...]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return value.astimezone(timezone.utc)


def _parse_datetime(value: object, *, default: datetime) -> datetime:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError("occurred_at must be an RFC3339 string")
    return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _file_identity(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_dev}:{stat.st_ino}"


def _required_string(record: dict[str, Any], field: str, *, max_length: int) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_length:
        raise ValueError(f"{field} must be a non-empty string up to {max_length} characters")
    return value.strip()


def _optional_identifier(record: dict[str, Any], field: str, *, max_length: int) -> str | None:
    value = record.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_length:
        raise ValueError(f"{field} must be a non-empty string up to {max_length} characters when present")
    return value.strip()


def _component_details(tool_name: str) -> tuple[str, str]:
    if tool_name.startswith("mcp__"):
        return "mcp_tool", "mcp_call"
    if tool_name.casefold() == "skill":
        return "skill", "skill_call"
    return "tool", "tool_call"


def _skill_id_from_result(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.search(r"[/\\]skills[/\\]([^/\\\s]+)", value)
    return match.group(1) if match else None


def _stable_event_id(installation_id: str, session_id: str, invocation_id: str, stage: str) -> str:
    key = f"{installation_id}:{session_id}:{invocation_id}:{stage}"
    return f"workbuddy:{hashlib.sha256(key.encode('utf-8')).hexdigest()}"


class WorkBuddyDesktopAdapter:
    """Read official WorkBuddy Code hook records without tool bodies."""

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
    ) -> WorkBuddyReadResult:
        source_path = Path(path).expanduser().resolve()
        if not source_path.is_file():
            raise WorkBuddyParseError("hook evidence path is not a regular file", path=str(source_path), offset=0)
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
                # The official hook ledger can be atomically replaced.  Replay it
                # from byte zero; its stable ComponentEvent IDs keep downstream
                # terminal delivery idempotent while preserving new hook events.
                offset = 0
            elif size < cursor.offset:
                raise WorkBuddyParseError("hook evidence file was truncated", path=str(source_path), offset=cursor.offset)
            else:
                offset = cursor.offset
        else:
            offset = 0

        events: list[ComponentEventV1] = []
        seen: set[str] = set()
        diagnostics: list[str] = []
        starts: dict[str, datetime] = {}
        latest_user_input: str | None = None
        pending_terminal_event_indexes: list[int] = []
        if offset:
            self._rebuild_context(
                source_path,
                limit=offset,
                session_id=session_id,
                current_time=current_time,
                starts=starts,
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
                    raise WorkBuddyParseError("hook evidence contains a truncated final line", path=str(source_path), offset=line_offset)
                try:
                    record = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    # A complete line that is not JSON (e.g. a write broken by a
                    # killed hook process) cannot be repaired. Skip it and report
                    # it, instead of blocking every later call in this file.
                    diagnostics.append(f"skipped malformed record at offset {line_offset}")
                    offset = next_offset
                    continue
                if not isinstance(record, dict):
                    offset = next_offset
                    continue
                schema_version = record.get("schema_version")
                if (
                    isinstance(schema_version, str)
                    and schema_version.startswith("workbuddy_")
                    and schema_version
                    not in {"workbuddy_hook_event.v1", "workbuddy_interaction_event.v1"}
                ):
                    # Hook schemas are versioned by WorkBuddy.  Do not skip a
                    # newer schema: that would make the worker look healthy
                    # while silently losing calls until its adapter is updated.
                    raise WorkBuddyParseError(
                        "unsupported WorkBuddy hook schema",
                        path=str(source_path),
                        offset=line_offset,
                    )
                if record.get("schema_version") == "workbuddy_interaction_event.v1":
                    if _required_string(record, "session_id", max_length=256) != session_id:
                        offset = next_offset
                        continue
                    interaction_stage = _required_string(record, "stage", max_length=32)
                    if interaction_stage == "user_input":
                        latest_user_input = None
                        pending_terminal_event_indexes.clear()
                    elif interaction_stage == "final_output":
                        for index in pending_terminal_event_indexes:
                            events[index] = events[index].model_copy(update={"model_output": None})
                        pending_terminal_event_indexes.clear()
                    else:
                        raise WorkBuddyParseError(f"unsupported interaction stage: {interaction_stage}", path=str(source_path), offset=line_offset)
                    offset = next_offset
                    continue
                try:
                    event = self._event_from_record(
                        record,
                        installation_id=installation_id,
                        session_id=session_id,
                        run_id=run_id or session_id,
                        current_time=current_time,
                        binding=binding,
                        starts=starts,
                        user_input=latest_user_input,
                    )
                except ValueError as exc:
                    raise WorkBuddyParseError(str(exc), path=str(source_path), offset=line_offset) from exc
                if event is not None and event.event_id not in seen:
                    skill_id = (
                        _optional_identifier(record, "skill_id", max_length=512)
                        or (_skill_id_from_result(record.get("tool_result")) if event.component_kind == "skill" else None)
                    )
                    if skill_id:
                        event = event.model_copy(update={"component_id": skill_id})
                        for index, previous in enumerate(events):
                            if previous.invocation_id == event.invocation_id and previous.component_kind == "skill":
                                events[index] = previous.model_copy(update={"component_id": skill_id})
                    seen.add(event.event_id)
                    events.append(event)
                    if event.stage != "started":
                        pending_terminal_event_indexes.append(len(events) - 1)
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
        return WorkBuddyReadResult(events=tuple(events), next_cursor=next_cursor, diagnostics=tuple(diagnostics))

    @classmethod
    def _rebuild_context(
        cls,
        path: Path,
        *,
        limit: int,
        session_id: str,
        current_time: datetime,
        starts: dict[str, datetime],
    ) -> None:
        with path.open("rb") as handle:
            while handle.tell() < limit:
                line_offset = handle.tell()
                raw_line = handle.readline()
                if not raw_line or not raw_line.endswith(b"\n"):
                    raise WorkBuddyParseError("hook evidence context is truncated", path=str(path), offset=line_offset)
                try:
                    record = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue  # reported once by read() when the line was first consumed
                if not isinstance(record, dict) or record.get("schema_version") != "workbuddy_hook_event.v1":
                    schema_version = record.get("schema_version") if isinstance(record, dict) else None
                    if (
                        isinstance(schema_version, str)
                        and schema_version.startswith("workbuddy_")
                        and schema_version
                        not in {"workbuddy_hook_event.v1", "workbuddy_interaction_event.v1"}
                    ):
                        # 增量游标重建也必须拒绝未知 Hook 版本，不能因它位于旧前缀而漏报。
                        raise WorkBuddyParseError(
                            "unsupported WorkBuddy hook schema",
                            path=str(path),
                            offset=line_offset,
                        )
                    continue
                if _required_string(record, "session_id", max_length=256) != session_id:
                    continue
                stage = _required_string(record, "stage", max_length=32)
                if stage != "started":
                    continue
                invocation_id = _required_string(record, "invocation_id", max_length=256)
                starts[invocation_id] = _parse_datetime(record.get("occurred_at"), default=current_time)

    @staticmethod
    def _event_from_record(
        record: dict[str, Any],
        *,
        installation_id: str,
        session_id: str,
        run_id: str,
        current_time: datetime,
        binding: Any,
        starts: dict[str, datetime],
        user_input: str | None,
    ) -> ComponentEventV1 | None:
        if record.get("schema_version") != "workbuddy_hook_event.v1":
            return None
        if _required_string(record, "session_id", max_length=256) != session_id:
            return None
        stage = _required_string(record, "stage", max_length=32)
        if stage not in {"started", "completed", "failed"}:
            raise ValueError(f"unsupported hook stage: {stage}")
        invocation_id = _required_string(record, "invocation_id", max_length=256)
        tool_name = _required_string(record, "tool_name", max_length=512)
        hook_name = _required_string(record, "hook_event_name", max_length=64)
        expected = {"started": "PreToolUse", "completed": "PostToolUse", "failed": "PostToolUseFailure"}[stage]
        if hook_name != expected:
            raise ValueError(f"stage {stage} requires hook_event_name {expected}")
        occurred_at = _parse_datetime(record.get("occurred_at"), default=current_time)
        started_at = starts.get(invocation_id)
        if stage == "started":
            starts[invocation_id] = occurred_at
            started_at = occurred_at
        component_kind, observation_kind = _component_details(tool_name)
        return ComponentEventV1(
            schema_version="component_event.v1",
            event_id=_stable_event_id(installation_id, session_id, invocation_id, stage),
            invocation_id=invocation_id,
            run_id=run_id,
            runtime="workbuddy",
            plugin_id="workbuddy-desktop-adapter",
            component_kind=component_kind,
            component_id=tool_name,
            component_version=None,
            stage=stage,
            occurred_at=occurred_at,
            started_at=started_at,
            ended_at=occurred_at if stage != "started" else None,
            retry_count=None,
            error_code="tool_failed" if stage == "failed" else None,
            observation_kind=observation_kind,
            evidence_source="workbuddy_command_hook",
            evidence_level="partial" if invocation_id.startswith("workbuddy-start:") else "verified",
            parent_invocation_id=None,
            installation_id=installation_id,
            tenant_id=binding.tenant_id if binding is not None else None,
            user_id=binding.user_id if binding is not None else None,
            source_type="hook_jsonl",
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            tool_result=None,
            user_input=user_input,
        )


__all__ = ["WorkBuddyDesktopAdapter", "WorkBuddyParseError", "WorkBuddyReadResult"]
