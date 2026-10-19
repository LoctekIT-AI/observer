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
