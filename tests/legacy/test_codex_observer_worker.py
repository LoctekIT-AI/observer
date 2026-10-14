from __future__ import annotations

import json
import socket
from pathlib import Path

from cowork_runtime.observer.codex_worker import CodexObserverWorker

FIXTURE = Path(__file__).parent / 'fixtures/codex-observer-real.jsonl'
SESSION = '01a0ce8f-096a-7040-bb6f-c65017c2127e'


def test_install_high_water_skips_history_and_collects_appended_real_records(tmp_path) -> None:
    source = tmp_path / 'rollout.jsonl'
    source.write_bytes(FIXTURE.read_bytes())
    worker = CodexObserverWorker(source_root=source, state_dir=tmp_path / 'state', flush=False)
    first = worker.run_once()
    assert first.events == first.terminal_enqueued == 0
    with source.open('ab') as stream:
        stream.write(FIXTURE.read_bytes())
    second = worker.run_once()
    assert not second.errors
    assert second.events == 3
    assert second.terminal_enqueued == 1
    assert worker.outbox.count_by_status() == {'pending': 1}
    assert worker.run_once().events == 0
    worker.close()


def test_real_tool_result_is_absent_from_ledger_and_outbox(tmp_path) -> None:
    source = tmp_path / 'rollout.jsonl'
    source.touch()
    worker = CodexObserverWorker(source_root=source, state_dir=tmp_path / 'state', flush=False)
    worker.run_once()
    source.write_bytes(FIXTURE.read_bytes())
    assert not worker.run_once().errors
    rows = [event.payload for event in worker.store.list_events(SESSION)]
    assert len(rows) == 3
    assert all(row['tool_result'] is None and row['user_input'] is None and row['model_output'] is None for row in rows)
    from datetime import datetime, timezone
    queued = worker.outbox.claim(owner_id='privacy-check', now=datetime.now(timezone.utc))
    assert len(queued) == 1
    assert queued[0].event.tool_result is None
    assert 'desktop_package_json_name' not in queued[0].event.model_dump_json()
    assert 'desktop_package_json_name' not in json.dumps(rows)
    worker.close()


def test_worker_writes_diagnostic_status_without_source_content(tmp_path) -> None:
    source = tmp_path / 'rollout.jsonl'
    source.touch()
    state = tmp_path / 'state'
    worker = CodexObserverWorker(source_root=source, state_dir=state, flush=False)
    assert not worker.run_once().errors
    status = json.loads((state / 'status.json').read_text())
    assert status['status'] == 'ok'
    assert status['pid'] > 0
    assert status['launch_pid'] == status['pid']
    assert 'rollout' not in json.dumps(status)
    worker.close()


def test_new_rollout_after_install_is_read_from_start_after_restart(tmp_path) -> None:
    source_root = tmp_path / 'sources'
    source_root.mkdir()
    state = tmp_path / 'state'
    worker = CodexObserverWorker(source_root=source_root, state_dir=state, flush=False)
    assert not worker.run_once().errors
    worker.close()
    (source_root / 'new.jsonl').write_bytes(FIXTURE.read_bytes())
    worker = CodexObserverWorker(source_root=source_root, state_dir=state, flush=False)
    report = worker.run_once()
    assert not report.errors
    assert report.events == 3
    assert worker.outbox.count_by_status() == {'pending': 1}
    assert worker.run_once().events == 0
    worker.close()


def test_replaced_source_does_not_duplicate_terminal_event(tmp_path) -> None:
    source = tmp_path / 'source.jsonl'
    source.touch()
    worker = CodexObserverWorker(source_root=source, state_dir=tmp_path / 'state', flush=False)
    worker.run_once()
    source.write_bytes(FIXTURE.read_bytes())
    assert not worker.run_once().errors
    replacement = tmp_path / 'replacement'
    replacement.write_bytes(FIXTURE.read_bytes())
    replacement.replace(source)
    assert not worker.run_once().errors
    assert worker.outbox.count_by_status() == {'pending': 1}
    assert len(worker.store.list_events(SESSION)) == 3
    worker.close()


def test_partial_record_is_diagnosed_without_cursor_loss_then_resumes(tmp_path) -> None:
    source = tmp_path / 'source.jsonl'
    source.touch()
    worker = CodexObserverWorker(source_root=source, state_dir=tmp_path / 'state', flush=False)
    worker.run_once()
    data = FIXTURE.read_bytes()
    source.write_bytes(data[:-5])
    report = worker.run_once()
    assert report.errors[0]['code'] == 'source_invalid'
    assert 'result' not in json.dumps(report.errors)
    with source.open('ab') as stream:
        stream.write(data[-5:])
    assert not worker.run_once().errors
    assert worker.outbox.count_by_status() == {'pending': 1}
    worker.close()


def test_generic_shell_tools_do_not_enter_skill_mcp_production_route(tmp_path) -> None:
    source = tmp_path / 'source.jsonl'
    source.touch()
    worker = CodexObserverWorker(source_root=source, state_dir=tmp_path / 'state', flush=False)
    worker.run_once()
    source.write_bytes((FIXTURE.parent / 'codex-generic-tool-real.jsonl').read_bytes())
    result = worker.run_once()
    assert not result.errors
    assert result.events == 0
    assert worker.outbox.count_by_status() == {}
    worker.close()
