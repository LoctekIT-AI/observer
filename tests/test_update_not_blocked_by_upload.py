"""v0.2.38: an upload-only failure of a running worker must not block (and roll back) an update."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

import install_observer_launch_agent as agent


def _status(tmp_path, **changes):
    status = {"status": "ok", "pid": 42, "launch_pid": 42, "errors": [],
              "checked_at": datetime.now(timezone.utc).isoformat()}
    status.update(changes)
    (tmp_path / "status.json").write_text(json.dumps(status), encoding="utf-8")
    return argparse.Namespace(state_dir=tmp_path, interval_seconds=5.0)


def test_delivery_failed_only_is_accepted(tmp_path):
    args = _status(tmp_path, status="failed", errors=[{"code": "delivery_failed", "error_type": "RuntimeError"}])
    _, error = agent._worker_status(args, {"pid": 42})
    assert error is None


def test_source_errors_still_block(tmp_path):
    args = _status(tmp_path, status="failed", errors=[
        {"code": "delivery_failed", "error_type": "RuntimeError"},
        {"code": "source_invalid", "error_type": "ValueError"},
    ])
    assert agent._worker_status(args, {"pid": 42})[1] == "worker status is not ok"
    args = _status(tmp_path, status="failed", errors=[])
    assert agent._worker_status(args, {"pid": 42})[1] == "worker status is not ok"


def test_result_stamped_before_call_is_uploaded_not_rejected_forever():
    """The stuck Claude record on the dev Mac (10-19): result time 1 s before the call time."""
    from datetime import timedelta

    from cowork_contracts import ComponentEventV1
    from cowork_runtime.telemetry.langfuse import _payload

    ended = datetime(2026, 10, 19, 5, 37, 36, tzinfo=timezone.utc)
    event = ComponentEventV1(
        schema_version="component_event.v1", event_id="claude-desktop:x", invocation_id="cblk_x", run_id="run-1",
        runtime="claude", plugin_id="claude-desktop-adapter", component_kind="mcp_tool",
        component_id="mcp__remote-devices__device_request_folder_access", stage="completed",
        occurred_at=ended, started_at=ended + timedelta(seconds=1), ended_at=ended,
        observation_kind="mcp_call", evidence_source="claude_desktop_indexeddb", evidence_level="verified",
        installation_id="install-claude-desktop-1", source_type="indexeddb_leveldb",
    )
    body, _, _ = _payload(event)
    span = body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span["startTimeUnixNano"] == span["endTimeUnixNano"]
