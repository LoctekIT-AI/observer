#!/usr/bin/env python3
"""Persist metadata-only WorkBuddy Code hook records from standard input."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_STAGE_TO_HOOK = {
    "user_input": "UserPromptSubmit",
    "started": "PreToolUse",
    "completed": "PostToolUse",
    "failed": "PostToolUseFailure",
    "final_output": "Stop",
}
_MAX_SESSION_ID = 256
_MAX_TOOL_NAME = 512
_MAX_INVOCATION_ID = 256
_MAX_SKILL_ID = 512
# WorkBuddy AI (the international build) is the same client with its own data
# folder; its hooks pass --client so its calls are reported under their own name.
CLIENTS = ("workbuddy", "workbuddy-ai")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _required_identifier(payload: dict[str, Any], field: str, *, max_length: int) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_length:
        raise ValueError(f"hook input {field} must be a non-empty string up to {max_length} characters")
    return value.strip()


def _optional_identifier(payload: dict[str, Any], field: str, *, max_length: int) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_length:
        raise ValueError(f"hook input {field} must be a non-empty string up to {max_length} characters when present")
    return value.strip()


def _invocation_id(payload: dict[str, Any], *, stage: str, session_id: str, tool_name: str) -> str:
    explicit = _optional_identifier(payload, "tool_use_id", max_length=_MAX_INVOCATION_ID)
    if explicit is not None:
        return explicit
    if stage != "started":
        raise ValueError("terminal hook input has no tool_use_id; refusing to infer lifecycle correlation")
    # A start record can stand alone when the host omits the correlation id.
    # This identifier is explicitly marked as local evidence by the Runtime adapter.
    material = f"{session_id}:{tool_name}:{payload.get('transcript_path', '')}:{_utc_now()}"
    return f"workbuddy-start:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _skill_id_from_value(value: object) -> str | None:
    """Extract only a normalized Skill identity; never persist the response body."""
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            match = re.search(r"[/\\]skills[/\\]([^/\\\s]+)", current)
            if match and len(match.group(1)) <= _MAX_SKILL_ID:
                return match.group(1)
        elif isinstance(current, dict):
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return None


def _event(payload: dict[str, Any], *, stage: str) -> dict[str, Any]:
    expected_hook = _STAGE_TO_HOOK[stage]
    hook_name = _required_identifier(payload, "hook_event_name", max_length=64)
    if hook_name != expected_hook:
        raise ValueError(f"stage {stage} requires hook_event_name {expected_hook}")
    session_id = _required_identifier(payload, "session_id", max_length=_MAX_SESSION_ID)
    if stage in {"user_input", "final_output"}:
        return {
            "schema_version": "workbuddy_interaction_event.v1",
            "occurred_at": _utc_now(),
            "stage": stage,
            "session_id": session_id,
            "hook_event_name": hook_name,
        }
    tool_name = _required_identifier(payload, "tool_name", max_length=_MAX_TOOL_NAME)
    event = {
        "schema_version": "workbuddy_hook_event.v1",
        "occurred_at": _utc_now(),
        "stage": stage,
        "session_id": session_id,
        "invocation_id": _invocation_id(
            payload,
            stage=stage,
            session_id=session_id,
            tool_name=tool_name,
        ),
        "tool_name": tool_name,
        "hook_event_name": hook_name,
    }
    if stage != "started" and tool_name.casefold() == "skill" and "tool_response" in payload:
        skill_id = _skill_id_from_value(payload["tool_response"])
        if skill_id is not None:
            event["skill_id"] = skill_id
    return event


def _event_path(explicit: Path | None = None) -> Path:
    if explicit is not None:
        if not explicit.is_absolute():
            raise ValueError("--event-file must be absolute")
        from install_workbuddy_hooks import _path
        path = _path(explicit)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path
    root = os.environ.get("AIHUB_WORKBUDDY_EVENT_DIR")
    if not root:
        # WorkBuddy's plugin hooks do not inherit the installer's environment.
        # Use the same per-user native directory as the worker/collector so a
        # normal desktop plugin install remains connected without shell setup.
        root = str(Path.home() / ".aihub" / "observer")
    directory = Path(root).expanduser().resolve()
    if directory.exists() and not directory.is_dir():
        raise ValueError("AIHUB_WORKBUDDY_EVENT_DIR is not a directory")
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "workbuddy-hook-events.jsonl"


def _unrecognized_event(payload: dict[str, Any], *, stage: str) -> dict[str, str]:
    session_id = payload.get("session_id")
    tool_name = payload.get("tool_name")
    event = {
        "schema_version": "workbuddy_unrecognized_event.v1",
        "occurred_at": _utc_now(),
        "stage": stage,
        "hook_event_name": _STAGE_TO_HOOK[stage],
        "hint": tool_name.strip()[:96] if isinstance(tool_name, str) and tool_name.strip() else "hook",
    }
    if isinstance(session_id, str) and session_id.strip() and len(session_id.strip()) <= _MAX_SESSION_ID:
        event["session_id"] = session_id.strip()
    return event


def _append_line(path: Path, line: bytes) -> None:
    """Append one complete JSONL line.

    WorkBuddy starts a separate hook process per tool stage, often at the same
    moment. Windows has no atomic O_APPEND across processes, so concurrent
    appends could overwrite each other and leave a broken half line. An
    exclusive lock on a sidecar file serializes the writers; the line is then
    written with a single append and flushed.
    """
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "a+b") as lock:
        if os.name == "nt":
            import msvcrt
            import time
            deadline = time.monotonic() + 4
            while True:
                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
            try:
                os.lseek(descriptor, 0, os.SEEK_END)
                view = memoryview(line)
                while view:
                    view = view[os.write(descriptor, view):]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.chmod(path, 0o600)
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=sorted(_STAGE_TO_HOOK))
    parser.add_argument("--event-file", type=Path, help="explicit absolute native hook output path")
    parser.add_argument("--client", choices=CLIENTS, default="workbuddy")
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("hook input must be a JSON object")
        try:
            event = _event(payload, stage=args.stage)
        except ValueError:
            # WorkBuddy sent a hook this bridge cannot read (usually a newer
            # WorkBuddy format). Keep one metadata-only marker for a finished
            # call so it is still counted as an unrecognized call.
            if args.stage not in {"completed", "failed"}:
                raise
            event = _unrecognized_event(payload, stage=args.stage)
        if args.client != "workbuddy":
            event["client"] = args.client
        path = _event_path(args.event_file)
        _append_line(path, (json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, OSError, ValueError) as exc:
        print(f"skill-mcp-observer hook bridge: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
