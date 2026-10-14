from __future__ import annotations

import json
import socket

from cowork_runtime.observer.claude_worker import ClaudeDesktopObserverWorker


def _record(call: str, name: str, stage: str, *, loaded_skill: str | None = None) -> bytes:
    occurred = "2026-09-25T02:03:18.886431Z" if stage == "started" else "2026-09-25T02:03:19.011472Z"
    if stage == "started":
        return (
            f'start_timestamp"\x1b{occurred}"\x0estop_timestamp"\x1b{occurred}"'
            f'\x04type"\x08tool_use"\x02id"\x1e{call}"\x04name"\x01{name}"'
        ).encode()
    loaded = f"Loaded 1 {loaded_skill} tool" if loaded_skill else ""
    return (
        f'start_timestamp"\x1b{occurred}"\x0estop_timestamp"\x1b{occurred}"'
        f'\x04type"\x0btool_result"\x0btool_use_id"\x1e{call}"\x04name"\x01{name}"'
        f'is_errorFcontent{loaded}'
    ).encode()


def _task() -> bytes:
    return (
        _record("search-1", "tool_search", "started")
        + _record("search-1", "tool_search", "completed", loaded_skill="observer-workspace")
        + _record("mcp-1", "+observer-workspace:observer_workspace_check", "started")
        + _record("mcp-1", "+observer-workspace:observer_workspace_check", "completed")
    )


def test_claude_worker_skips_install_history_then_collects_new_skill_and_mcp(tmp_path) -> None:
    source_root = tmp_path / "IndexedDB"
    source_root.mkdir()
    source = source_root / "000001.log"
    source.write_bytes(_task())
    worker = ClaudeDesktopObserverWorker(
        source_root=source_root,
        state_dir=tmp_path / "state",
        skill_ids=("observer-workspace",),
        flush=False,
    )

    first = worker.run_once()
    assert first.events == first.terminal_enqueued == 0
    with source.open("ab") as stream:
        stream.write(_task())

    second = worker.run_once()
    assert not second.errors
    assert second.events == 3
    assert second.terminal_enqueued == 2
    assert worker.outbox.count_by_status() == {"pending": 2}
    assert worker.run_once().events == 0
    rows = [event.payload for event in worker.store.list_events("claude-desktop:000001.log")]
    assert all(row["user_input"] is None and row["model_output"] is None and row["tool_result"] is None for row in rows)
    assert "observer workspace" not in json.dumps(rows).lower()
    worker.close()


def test_claude_worker_does_not_use_claude_code_transcripts(tmp_path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    (source_root / "transcript.jsonl").write_text('{"type":"tool_use"}\n', encoding="utf-8")
    worker = ClaudeDesktopObserverWorker(source_root=source_root, state_dir=tmp_path / "state", flush=False)
    report = worker.run_once()
    assert not report.errors
    assert report.files == 0
    assert report.events == 0
    worker.close()


