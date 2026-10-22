"""WorkBuddy turn markers (see turn_tracker).

Turn boundaries come from Observer's own UserPromptSubmit / Stop hook records (no text).
The text of a finished turn is read locally from WorkBuddy's own conversation file
<home>/.workbuddy[-ai]/projects/<project>/<session id>.jsonl: the user's last message
before the turn ended and the assistant's last reply after it.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .turn_tracker import Marker

DATA_FOLDERS = {"workbuddy": ".workbuddy", "workbuddy-ai": ".workbuddy-ai"}
_SLACK = timedelta(seconds=30)
_REMINDER = re.compile(r"<system-reminder[^>]*>.*?</system-reminder>", re.S)
_QUERY = re.compile(r"<user_query>(.*?)</user_query>", re.S)


def _at(value: object) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def markers(path: Path, start: int, end: int, session_id: str) -> list[Marker]:
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
            if not isinstance(record, dict) or record.get("schema_version") != "workbuddy_interaction_event.v1":
                continue
            if record.get("session_id") != session_id:
                continue
            at = _at(record.get("occurred_at"))
            if at is None:
                continue
            if record.get("stage") == "user_input":
                found.append(("start", at, {}))
            elif record.get("stage") == "final_output":
                found.append(("end", at, {}))
    return found


def user_text(content: object) -> str | None:
    parts = [item.get("text") for item in content if isinstance(item, dict) and isinstance(item.get("text"), str)] \
        if isinstance(content, list) else [content] if isinstance(content, str) else []
    text = "\n".join(part for part in parts if part)
    queries = _QUERY.findall(text)
    text = "\n".join(queries) if queries else _REMINDER.sub("", text)
    return text.strip() or None


def assistant_text(content: object) -> str | None:
    if isinstance(content, str):
        return content.strip() or None
    if not isinstance(content, list):
        return None
    parts = [item.get("text") for item in content
             if isinstance(item, dict) and item.get("type") in {"output_text", "text"} and isinstance(item.get("text"), str)]
    return "\n".join(parts).strip() or None


def transcript(home: Path, runtime: str, session_id: str) -> Path | None:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", session_id):
        return None
    folder = home / DATA_FOLDERS.get(runtime, ".workbuddy") / "projects"
    if not folder.is_dir():
        return None
    for candidate in folder.glob(f"*/{session_id}.jsonl"):
        if candidate.is_file():
            return candidate
    return None


def turn_text(path: Path, ended_at: datetime) -> tuple[str | None, str | None]:
    """(user input, final reply) of the turn that ended at ended_at."""
    last_user: str | None = None
    final: str | None = None
    limit = ended_at + _SLACK
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("type") != "message":
                continue
            at = _at(record.get("timestamp"))
            if at is not None and at > limit:
                break
            if record.get("role") == "user":
                text = user_text(record.get("content"))
                if text:
                    last_user, final = text, None
            elif record.get("role") == "assistant":
                text = assistant_text(record.get("content"))
                if text:
                    final = text
    return last_user, final


def fill(found: list[Marker], *, home: Path, runtime: str, session_id: str) -> list[Marker]:
    """Add the turn's text to every end marker (read once per poll)."""
    if not any(kind == "end" for kind, _, _ in found):
        return found
    path = transcript(home, runtime, session_id)
    if path is None:
        return found
    filled: list[Marker] = []
    for kind, at, data in found:
        if kind == "end":
            try:
                user, final = turn_text(path, at)
            except OSError:
                user = final = None
            data = {**data, "input": user, "text": final}
        filled.append((kind, at, data))
    return filled


__all__ = ["DATA_FOLDERS", "fill", "markers", "transcript", "turn_text", "user_text"]
