"""Turn markers in a Codex rollout (see turn_tracker): turn start / end, user messages, final answer."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .turn_tracker import Marker


def _text(value: object) -> str | None:
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, list):
        parts = [item["text"] for item in value if isinstance(item, dict) and isinstance(item.get("text"), str)]
        text = "\n".join(parts)
        return text if text.strip() else None
    return None


def _at(record: dict[str, Any]) -> datetime | None:
    value = record.get("timestamp")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def markers(path: Path, start: int, end: int) -> list[Marker]:
    """Markers of the records between two byte offsets (complete lines only)."""
    found: list[Marker] = []
    with path.open("rb") as handle:
        handle.seek(start)
        while handle.tell() < end:
            raw = handle.readline()
            if not raw or not raw.endswith(b"\n"):
                break
            try:
                record = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict) or record.get("type") != "event_msg":
                continue
            payload = record.get("payload")
            at = _at(record)
            if not isinstance(payload, dict) or at is None:
                continue
            kind = payload.get("type")
            if kind == "task_started":
                found.append(("start", at, {"turn_id": str(payload.get("turn_id") or "") or None}))
            elif kind == "task_complete":
                found.append(("end", at, {"turn_id": payload.get("turn_id"), "text": _text(payload.get("last_agent_message"))}))
            elif kind == "turn_aborted":
                found.append(("end", at, {"turn_id": payload.get("turn_id"), "text": None, "aborted": True}))
            elif kind == "user_message":  # Codex CLI
                text = _text(payload.get("message"))
                if text:
                    found.append(("user", at, {"text": text}))
            elif kind == "item_completed" and isinstance(payload.get("item"), dict):
                item = payload["item"]
                if item.get("type") == "UserMessage":
                    text = _text(item.get("content"))
                    if text:
                        found.append(("user", at, {"text": text}))
                elif item.get("type") == "AgentMessage" and item.get("phase") == "final_answer":
                    text = _text(item.get("content"))
                    if text:
                        found.append(("final", at, {"text": text}))
    return found


__all__ = ["markers"]
