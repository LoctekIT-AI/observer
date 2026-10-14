#!/usr/bin/env python3
"""Persist metadata-only Codex plugin hook lifecycle records."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_STAGE_TO_HOOK = {
    "session_started": "SessionStart",
    "started": "PreToolUse",
    "completed": "PostToolUse",
    "turn_completed": "Stop",
    "cancelled": "Interrupt",
}
_TOOL_STAGES = {"started", "completed"}
_TURN_STAGES = {"started", "completed", "turn_completed", "cancelled"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _required_identifier(payload: dict[str, Any], field: str, *, max_length: int = 512) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_length:
        raise ValueError(f"hook input {field} must be a non-empty string up to {max_length} characters")
    return value.strip()


def _event(payload: dict[str, Any], *, stage: str) -> dict[str, str]:
    hook_name = _required_identifier(payload, "hook_event_name", max_length=64)
    expected_hook = _STAGE_TO_HOOK[stage]
    if hook_name != expected_hook:
        raise ValueError(f"stage {stage} requires hook_event_name {expected_hook}")
    event = {
        "schema_version": "codex_hook_event.v1",
        "occurred_at": _utc_now(),
        "stage": stage,
        "session_id": _required_identifier(payload, "session_id", max_length=256),
        "hook_event_name": hook_name,
    }
    if stage in _TURN_STAGES:
        event["turn_id"] = _required_identifier(payload, "turn_id", max_length=256)
    if stage in _TOOL_STAGES:
        event["invocation_id"] = _required_identifier(payload, "tool_use_id", max_length=256)
        event["tool_name"] = _required_identifier(payload, "tool_name")
    return event


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=sorted(_STAGE_TO_HOOK))
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("hook input must be a JSON object")
        event = _event(payload, stage=args.stage)
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, separators=(",", ":")) + "\n")
    except (UnicodeDecodeError, json.JSONDecodeError, OSError, ValueError) as exc:
        print(f"skill-mcp-observer Codex hook bridge: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
