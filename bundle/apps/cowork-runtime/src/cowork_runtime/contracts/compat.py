"""Compatibility mappers for existing Cowork UI events."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .enums import EventVisibility, RuntimeEventType
from .events import RuntimeEvent

LEGACY_UI_SCHEMA_VERSION = "cowork_run_ui_event.v1"

LEGACY_UI_EVENT_TYPE_MAP: Dict[str, RuntimeEventType] = {
    "task_created": RuntimeEventType.RUN_CREATED,
    "thinking": RuntimeEventType.AGENT_REASONING,
    "turn.assistant_text": RuntimeEventType.TURN_ASSISTANT_TEXT,
    "turn.assistant_draft": RuntimeEventType.TURN_ASSISTANT_DRAFT,
    "artifact": RuntimeEventType.ARTIFACT_CREATED,
    "run.completed": RuntimeEventType.RUN_COMPLETED,
    "run.failed": RuntimeEventType.RUN_FAILED,
    "run.cancelled": RuntimeEventType.RUN_CANCELLED,
    "run.blocked": RuntimeEventType.RUN_BLOCKED,
    "confirmation.decided": RuntimeEventType.APPROVAL_RESOLVED,
    "client_action.completed": RuntimeEventType.TOOL_RESULT_CREATED,
    "state_sync": RuntimeEventType.STATE_SYNC,
}


def runtime_event_from_legacy_ui_event(raw: Dict[str, Any]) -> RuntimeEvent:
    """Map a `cowork_run_ui_event.v1` record into `cowork.runtime.event.v1`.

    The mapper preserves the original data under `payload.legacy` so timeline
    rendering can migrate without losing user-visible content.
    """

    run_id = _required_str(raw, "run_id")
    seq = int(raw.get("seq") or 0)
    legacy_type = _required_str(raw, "type")
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    occurred_at = _parse_datetime(raw.get("created_at"))
    event_type = LEGACY_UI_EVENT_TYPE_MAP.get(legacy_type, RuntimeEventType.STATE_SYNC)
    event_id = str(raw.get("event_id") or f"legacy:{run_id}:{seq}:{legacy_type}")
    return RuntimeEvent(
        run_id=run_id,
        seq=seq,
        event_id=event_id,
        type=event_type,
        occurred_at=occurred_at,
        visibility=_visibility_for_legacy_type(legacy_type),
        payload={
            "legacy": {
                "schema_version": raw.get("schema_version") or LEGACY_UI_SCHEMA_VERSION,
                "type": legacy_type,
                "session_id": raw.get("session_id"),
                "data": data,
            },
            "content": data.get("content"),
            "status": data.get("status"),
        },
    )


def _required_str(raw: Dict[str, Any], field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"legacy UI event missing {field}")
    return value


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _visibility_for_legacy_type(legacy_type: str) -> EventVisibility:
    if legacy_type in {"thinking", "state_sync"}:
        return EventVisibility.DEBUG
    return EventVisibility.USER
