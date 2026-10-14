"""Layer 2: every collector sends small health records (versions, states, counters; no content)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from cowork_runtime.telemetry.health import HealthReporter, bundle_version, health_payload

T0 = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)


def _attrs(body):
    span = body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    return span, {a["key"]: next(iter(a["value"].values())) for a in span["attributes"]}


def test_sends_on_first_poll_on_state_change_and_hourly(tmp_path):
    sent = []
    reporter = HealthReporter(tmp_path, "codex", send=lambda body, trace, span: sent.append(body))
    ok = {"status": "ok", "events": 2, "outbox": {"delivered": 5}, "errors": []}
    assert reporter.observe(ok, now=T0)
    assert not reporter.observe(ok, now=T0 + timedelta(minutes=10))
    failed = {"status": "failed", "errors": [{"code": "source_invalid", "path": "C:/Users/someone/secret", "error_type": "JSONDecodeError"}]}
    assert reporter.observe(failed, now=T0 + timedelta(minutes=11))
    assert reporter.observe(ok, now=T0 + timedelta(minutes=12), unrecognized_calls=3)
    assert reporter.observe(ok, now=T0 + timedelta(minutes=80))
    assert len(sent) == 4
    span, attrs = _attrs(sent[1])
    assert span["name"] == "observer.health" and span["status"]["code"] == 2
    assert attrs["observer.health.error_codes"] == "source_invalid:JSONDecodeError"
    assert "secret" not in json.dumps(sent)  # paths are never sent
    _, latest = _attrs(sent[-1])
    assert latest["observer.health.total_unrecognized_calls"] == 3
    assert latest["observer.health.total_events"] == 8  # per-poll counts are summed
    assert latest["observer.health.runtime"] == "codex"


def test_send_failure_never_breaks_collection_and_is_retried(tmp_path):
    calls = []

    def flaky(body, trace, span):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("network down")

    reporter = HealthReporter(tmp_path, "dsh", send=flaky)
    assert not reporter.observe({"status": "ok"}, now=T0)
    assert json.loads((tmp_path / "health.json").read_text())["last_send_error"] == "OSError"
    assert reporter.observe({"status": "ok"}, now=T0 + timedelta(minutes=1))  # not sent yet, so due again


def test_disabled_reporter_keeps_totals_without_sending(tmp_path):
    reporter = HealthReporter(tmp_path, "claude", enabled=False, send=lambda *a: (_ for _ in ()).throw(AssertionError))
    assert not reporter.observe({"status": "ok", "events": 1}, now=T0)
    assert json.loads((tmp_path / "health.json").read_text())["totals"]["events"] == 1


def test_payload_identifies_the_build(tmp_path):
    body, trace_id, span_id = health_payload("workbuddy", {"status": "ok"}, now=T0, totals={})
    _, attrs = _attrs(body)
    assert attrs["observer.health.bundle_version"] == bundle_version() != "unknown"
    assert attrs["langfuse.trace.name"] == "observer.health"
    assert len(trace_id) == 32 and len(span_id) == 16


def test_worker_records_health_state(tmp_path):
    from cowork_runtime.observer.claude_worker import ClaudeDesktopObserverWorker

    source = tmp_path / "leveldb"
    source.mkdir()
    (source / "000003.log").write_bytes(b"")
    worker = ClaudeDesktopObserverWorker(source_root=source, state_dir=tmp_path / "state", flush=False)
    worker.run_once()
    worker.close()
    assert "totals" in json.loads((tmp_path / "state" / "health.json").read_text())
