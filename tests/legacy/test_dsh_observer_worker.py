"""Integration tests consume genuine metadata emitted by installed DSH services."""
from __future__ import annotations

import os
import pytest
import importlib.util
import json
import os
import shutil
import subprocess

pytestmark = pytest.mark.skipif("DSH_NATIVE_ROOT" not in os.environ, reason="needs a real DSH installation (DSH_NATIVE_ROOT)")
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]

@pytest.fixture
def native_events(tmp_path: Path) -> Path:
    env = {**os.environ, 'DSH_NATIVE_ROOT': os.environ['DSH_NATIVE_ROOT'], 'DSH_TEST_OUTPUT': str(tmp_path / 'native')}
    subprocess.run(['node', str(ROOT / 'packages/dsh-observer/tests/native.integration.mjs')], cwd=ROOT, env=env, check=True, capture_output=True, text=True)
    return tmp_path / 'native/component-events.jsonl'


def test_genuine_dsh_records_persist_queue_and_replay_without_duplicates(native_events: Path, tmp_path: Path) -> None:
    assert importlib.util.find_spec('cowork_runtime.observer.dsh_worker') is not None, 'durable DSH worker must exist'
    from cowork_runtime.observer.dsh_worker import DshObserverWorker
    rows = [json.loads(line) for line in native_events.read_text().splitlines()]
    state = tmp_path / 'state'
    worker = DshObserverWorker(source_root=native_events, state_dir=state, flush=False)
    try:
        first = worker.run_once()
        assert first.events == first.terminal_enqueued == 5
        assert len(worker.store.list_events(rows[0]['run_id'])) == 5
        assert worker.outbox.count_by_status() == {'pending': 5}
        assert worker.run_once().events == 0
        with native_events.open('a') as handle:
            handle.write(native_events.read_text())
        worker.run_once()
        assert len(worker.store.list_events(rows[0]['run_id'])) == 5
        assert worker.outbox.count_by_status() == {'pending': 5}
    finally:
        worker.close()
    reopened = DshObserverWorker(source_root=native_events, state_dir=state, flush=False)
    try:
        assert reopened.run_once().events == 0
        assert reopened.outbox.count_by_status() == {'pending': 5}
        assert reopened.registry.manifest('dsh-observer', rows[0]['installation_id']).runtime == 'dsh'
    finally:
        reopened.close()

    # 真实队列保留、账本需恢复时，重读不能把已有队列条目报告成新增。
    recovery = tmp_path / 'recovery'
    recovery.mkdir()
    shutil.copyfile(state / 'langfuse-content-outbox.sqlite3', recovery / 'langfuse-content-outbox.sqlite3')
    recovering = DshObserverWorker(source_root=native_events, state_dir=recovery, flush=False)
    try:
        result = recovering.run_once()
        assert result.events == 5
        assert result.terminal_enqueued == 0
        assert recovering.outbox.count_by_status() == {'pending': 5}
    finally:
        recovering.close()


def test_malformed_and_content_bearing_records_fail_visibly(native_events: Path, tmp_path: Path) -> None:
    assert importlib.util.find_spec('cowork_runtime.observer.dsh_worker') is not None, 'durable DSH worker must exist'
    from cowork_runtime.observer.dsh_worker import DshObserverWorker
    worker = DshObserverWorker(source_root=native_events, state_dir=tmp_path / 'state', flush=False)
    try:
        worker.run_once()
        with native_events.open('a') as handle:
            handle.write('{broken json}\n')
        with pytest.raises(ValueError, match='invalid DSH JSONL'):
            worker.run_once()
        assert json.loads((tmp_path / 'state/status.json').read_text())['status'] == 'failed'
        assert worker.outbox.count_by_status() == {'pending': 5}
    finally:
        worker.close()
    row = json.loads(native_events.read_text().splitlines()[0])
    row['tool_result'] = 'private body'
    native_events.write_text(json.dumps(row) + '\n')
    worker = DshObserverWorker(source_root=native_events, state_dir=tmp_path / 'unsafe-state', flush=False)
    try:
        with pytest.raises(ValueError, match='metadata-only'):
            worker.run_once()
        assert worker.outbox.count_by_status() == {}
    finally:
        worker.close()


def test_native_source_io_and_truncation_fail(native_events: Path, tmp_path: Path) -> None:
    assert importlib.util.find_spec('cowork_runtime.observer.dsh_worker') is not None, 'durable DSH worker must exist'
    from cowork_runtime.observer.dsh_worker import DshObserverWorker
    worker = DshObserverWorker(source_root=tmp_path / 'missing', state_dir=tmp_path / 'state', flush=False)
    try:
        with pytest.raises(FileNotFoundError):
            worker.run_once()
    finally:
        worker.close()
    native_events.write_bytes(native_events.read_bytes().rstrip(b'\n'))
    worker = DshObserverWorker(source_root=native_events, state_dir=tmp_path / 'truncated', flush=False)
    try:
        with pytest.raises(ValueError, match='truncated'):
            worker.run_once()
    finally:
        worker.close()


def test_cli_reports_real_native_records_and_visible_failure(native_events: Path, tmp_path: Path) -> None:
    command = [str(ROOT / '.venv/bin/python'), str(ROOT / 'scripts/dsh_observer_worker.py'), '--source-root', str(native_events), '--state-dir', str(tmp_path / 'cli-state')]
    checked = subprocess.run([*command, '--check', '--no-flush'], cwd=tmp_path, capture_output=True, text=True, check=True)
    assert json.loads(checked.stdout)['delivery'] == 'local-only'
    consumed = subprocess.run([*command, '--once', '--no-flush'], cwd=tmp_path, capture_output=True, text=True, check=True)
    assert json.loads(consumed.stdout)['terminal_enqueued'] == 5
    with native_events.open('a') as handle:
        handle.write('{private-invalid-marker}\n')
    failed = subprocess.run([*command, '--once', '--no-flush'], cwd=tmp_path, capture_output=True, text=True)
    assert failed.returncode == 1
    assert json.loads(failed.stdout)['status'] == 'failed'
    assert json.loads(failed.stdout)['error_type'] == 'ValueError'
    assert 'private-invalid-marker' not in failed.stdout
    collector = subprocess.run([str(ROOT / '.venv/bin/python'), str(ROOT / 'scripts/observer_collect.py'), 'collect', '--runtime', 'dsh', '--source', str(native_events), '--state-dir', str(tmp_path / 'collector')], cwd=ROOT, env={**os.environ, 'PYTHONPATH': str(ROOT / 'packages/contracts/src') + ':' + str(ROOT / 'apps/cowork-runtime/src')}, capture_output=True, text=True)
    assert collector.returncode == 1
    assert json.loads(collector.stdout)['status'] == 'error'
    assert 'private-invalid-marker' not in collector.stdout


def _native_queued_payloads(native_events: Path, state: Path):
    from datetime import datetime, timezone
    from cowork_runtime.observer.dsh_worker import DshObserverWorker
    from cowork_runtime.telemetry.langfuse import _payload
    worker = DshObserverWorker(source_root=native_events, state_dir=state, flush=False)
    try:
        assert worker.run_once().terminal_enqueued == 5
        items = worker.outbox.claim(owner_id='native-payload-review', now=datetime.now(timezone.utc))
        assert len(items) == 5
        return [(item.event, json.loads(json.dumps(_payload(item.event)[0]))) for item in items]
    finally:
        worker.close()


def test_native_unknown_execution_timing_exports_terminal_observation(native_events: Path, tmp_path: Path) -> None:
    for event, payload in _native_queued_payloads(native_events, tmp_path / 'timing-state'):
        assert event.started_at is None
        span = payload['resourceSpans'][0]['scopeSpans'][0]['spans'][0]
        attrs = {attr['key']: attr['value'] for attr in span['attributes']}
        assert attrs['langfuse.observation.type'] == {'stringValue': 'event'}
        assert attrs['cowork.execution_start_known'] == {'boolValue': False}
        assert attrs['cowork.execution_duration_known'] == {'boolValue': False}
        assert attrs['cowork.timestamp_semantics'] == {'stringValue': 'terminal_observation'}
        terminal_ns = int(event.occurred_at.timestamp()) * 1_000_000_000 + event.occurred_at.microsecond * 1_000
        assert span['startTimeUnixNano'] == span['endTimeUnixNano'] == terminal_ns
        assert attrs['cowork.observation_kind'] == {'stringValue': event.observation_kind}
        assert not any('execution_duration' in key and key != 'cowork.execution_duration_known' for key in attrs)


def test_native_cancelled_export_is_not_success(native_events: Path, tmp_path: Path) -> None:
    queued = _native_queued_payloads(native_events, tmp_path / 'status-state')
    cancelled = [(event, payload) for event, payload in queued if event.stage == 'cancelled']
    assert len(cancelled) == 1
    event, payload = cancelled[0]
    assert event.error_code == 'ABORTED_BEFORE_DISPATCH'
    span = payload['resourceSpans'][0]['scopeSpans'][0]['spans'][0]
    assert span['status']['code'] == 0  # OTLP UNSET: cancellation is not successful completion.
    attrs = {attr['key']: attr['value'] for attr in span['attributes']}
    assert attrs['cowork.stage'] == {'stringValue': 'cancelled'}
    assert attrs['cowork.error_code'] == {'stringValue': event.error_code}
    for event, payload in queued:
        span = payload['resourceSpans'][0]['scopeSpans'][0]['spans'][0]
        if event.stage == 'completed':
            assert span['status']['code'] == 1
        elif event.stage == 'failed':
            assert span['status']['code'] == 2
