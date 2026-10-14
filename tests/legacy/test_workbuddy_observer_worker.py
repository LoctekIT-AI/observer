from __future__ import annotations

import json

from cowork_runtime.observer.workbuddy_worker import WorkBuddyDesktopObserverWorker, _installation_id


def _record(*, stage: str, invocation_id: str, tool_name: str, session_id: str = "session-1") -> dict[str, object]:
    return {
        "schema_version": "workbuddy_hook_event.v1",
        "occurred_at": "2026-09-25T04:37:47.860000Z",
        "stage": stage,
        "session_id": session_id,
        "invocation_id": invocation_id,
        "tool_name": tool_name,
        "hook_event_name": "PreToolUse" if stage == "started" else "PostToolUse",
        "tool_result": "secret body" if stage != "started" else None,
    }


def _append(path, records: list[dict[str, object]]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def test_workbuddy_worker_skips_existing_hook_history_then_collects_skill_and_mcp(tmp_path) -> None:
    source = tmp_path / "workbuddy-hook-events.jsonl"
    source.write_text(json.dumps(_record(stage="started", invocation_id="old", tool_name="Skill")) + "\n", encoding="utf-8")
    worker = WorkBuddyDesktopObserverWorker(source_root=source, state_dir=tmp_path / "state", flush=False)

    first = worker.run_once()
    assert first.events == first.terminal_enqueued == 0
    _append(source, [
        _record(stage="started", invocation_id="skill-1", tool_name="Skill"),
        _record(stage="completed", invocation_id="skill-1", tool_name="Skill"),
        _record(stage="started", invocation_id="mcp-1", tool_name="mcp__observer__check"),
        _record(stage="completed", invocation_id="mcp-1", tool_name="mcp__observer__check"),
    ])

    second = worker.run_once()
    assert not second.errors
    assert second.sessions == 1
    assert second.events == 4
    assert second.terminal_enqueued == 2
    assert worker.outbox.count_by_status() == {"pending": 2}
    assert worker.run_once().events == 0
    rows = [event.payload for event in worker.store.list_events("session-1")]
    assert all(row["user_input"] is None and row["model_output"] is None and row["tool_result"] is None for row in rows)
    worker.close()


def test_workbuddy_worker_preserves_cursor_when_delivery_fails(tmp_path, monkeypatch) -> None:
    source = tmp_path / "workbuddy-hook-events.jsonl"
    source.write_text(json.dumps(_record(stage="started", invocation_id="old", tool_name="Skill")) + "\n", encoding="utf-8")
    worker = WorkBuddyDesktopObserverWorker(source_root=source, state_dir=tmp_path / "state", flush=False)
    worker.run_once()
    _append(source, [
        _record(stage="started", invocation_id="skill-1", tool_name="Skill"),
        _record(stage="completed", invocation_id="skill-1", tool_name="Skill"),
    ])
    monkeypatch.setattr(worker.delivery, "deliver", lambda _event: (_ for _ in ()).throw(RuntimeError("ledger unavailable")))
    report = worker.run_once()
    assert report.errors and report.errors[0]["code"] == "source_invalid"
    assert worker.outbox.count_by_status() == {}
    cursor = worker.registry.cursor(_installation_id("session-1"), str(source))
    assert cursor is not None and cursor.offset < source.stat().st_size
    worker.close()
