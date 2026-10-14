from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2] / "bundle"
BRIDGE = REPO_ROOT / "scripts" / "workbuddy_hook_bridge.py"


def _run_bridge(tmp_path: Path, *, stage: str, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    event_dir = tmp_path / "workbuddy-events"
    environment = os.environ | {"AIHUB_WORKBUDDY_EVENT_DIR": str(event_dir)}
    return subprocess.run(
        [sys.executable, str(BRIDGE), "--stage", stage],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )


def test_pre_tool_hook_persists_only_documented_metadata(tmp_path: Path) -> None:
    result = _run_bridge(
        tmp_path,
        stage="started",
        payload={
            "session_id": "session-1",
            "hook_event_name": "PreToolUse",
            "tool_name": "mcp__demo__search",
            "tool_input": {"secret": "must-not-persist"},
            "transcript_path": "/private/transcript.jsonl",
        },
    )

    assert result.returncode == 0, result.stderr
    event_path = tmp_path / "workbuddy-events" / "workbuddy-hook-events.jsonl"
    records = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["schema_version"] == "workbuddy_hook_event.v1"
    assert records[0]["stage"] == "started"
    assert records[0]["session_id"] == "session-1"
    assert records[0]["tool_name"] == "mcp__demo__search"
    assert set(records[0]) == {
        "schema_version",
        "occurred_at",
        "stage",
        "session_id",
        "invocation_id",
        "tool_name",
        "hook_event_name",
    }


def test_terminal_hook_without_official_tool_use_id_is_kept_as_unrecognized_marker(tmp_path: Path) -> None:
    result = _run_bridge(
        tmp_path,
        stage="completed",
        payload={
            "session_id": "session-1",
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "must-not-persist"},
            "tool_response": {"output": "must-not-persist"},
        },
    )

    assert result.returncode == 0
    written = (tmp_path / "workbuddy-events" / "workbuddy-hook-events.jsonl").read_text()
    [record] = [json.loads(line) for line in written.splitlines()]
    assert record["schema_version"] == "workbuddy_unrecognized_event.v1"
    assert record["hint"] == "Bash" and record["session_id"] == "session-1"
    assert "must-not-persist" not in written


def test_plugin_hook_uses_worker_default_directory_without_shell_environment(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment.pop("AIHUB_WORKBUDDY_EVENT_DIR", None)
    environment["HOME"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, str(BRIDGE), "--stage", "started"],
        input=json.dumps({
            "session_id": "session-1",
            "hook_event_name": "PreToolUse",
            "tool_name": "mcp__demo__search",
            "tool_use_id": "call-1",
        }),
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".aihub/observer/workbuddy-hook-events.jsonl").is_file()
