"""v0.2.37: Codex Skill reads are uploaded, the session tool list is not a call, coverage self-check."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from cowork_runtime.observer.codex_worker import SKILL_BACKFILL_MARKER, CodexObserverWorker
from cowork_runtime.telemetry.health import coverage_gaps

SKILL = "/Users/u/.agents/skills/test-driven-development/SKILL.md"


def _ms(at: datetime) -> int:
    return int(at.timestamp() * 1000)


def _session(at: datetime, n: int = 1) -> list[dict]:
    """Records shaped like a current Codex Desktop (code mode) rollout."""
    ts = at.isoformat().replace("+00:00", "Z")
    return [
        {"timestamp": ts, "type": "response_item", "payload": {
            "type": "additional_tools", "id": f"at_{n}", "role": "developer",
            "tools": [{"type": "namespace", "name": "mcp__codex_app", "description": "d",
                       "tools": [{"name": "create_worktree", "parameters": {"type": "object"}}]}]},
         "metadata": {"client_authored": True, "mcp_attribution": {"status": "unattributed"}}},
        {"timestamp": ts, "type": "response_item", "payload": {
            "type": "custom_tool_call", "id": f"ctc_{n}", "status": "completed", "call_id": f"call_{n}", "name": "exec",
            "input": f'await tools.exec_command({{cmd:"cat {SKILL}"}}); text(await tools.mcp__codex_app__create_worktree({{name:"w"}}));'}},
        {"timestamp": ts, "type": "event_msg", "payload": {
            "type": "item_completed", "started_at_ms": _ms(at), "completed_at_ms": _ms(at),
            "item": {"type": "CommandExecution", "id": f"exec_cmd_{n}", "status": "completed",
                     "command": ["/bin/zsh", "-lc", f"cat {SKILL}"], "aggregated_output": "skill body"}}},
        {"timestamp": ts, "type": "event_msg", "payload": {
            "type": "item_completed", "started_at_ms": _ms(at), "completed_at_ms": _ms(at),
            "item": {"type": "McpToolCall", "id": f"mcp_{n}", "server": "codex_app", "tool": "create_worktree",
                     "status": "completed", "result": "secret"}}},
        {"timestamp": ts, "type": "response_item", "payload": {
            "type": "custom_tool_call_output", "id": f"cto_{n}", "call_id": f"call_{n}", "output": "secret output"}},
    ]


def _write(path, records):
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _queued(worker):
    items = worker.outbox.claim(owner_id="test", now=datetime.now(timezone.utc), limit=1000)
    return sorted((item.event.component_kind, item.event.component_id) for item in items)


def test_code_mode_session_uploads_skill_and_mcp_without_unrecognized(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    worker = CodexObserverWorker(source_root=root, state_dir=tmp_path / "state", flush=False)
    assert not worker.run_once().errors  # installation
    _write(root / "rollout-new.jsonl", _session(datetime.now(timezone.utc)))
    report = worker.run_once()
    assert not report.errors
    assert _queued(worker) == [("mcp_tool", "mcp__codex_app__create_worktree"), ("skill", "test-driven-development")]
    health = json.loads((tmp_path / "state" / "health.json").read_text())["totals"]
    assert health["unrecognized_calls"] == 0
    assert (health["skill_signals"], health["skill_reported"], health["mcp_signals"], health["mcp_reported"]) == (1, 1, 1, 1)
    worker.close()


def test_one_time_backfill_uploads_skills_read_before_the_fix(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    state = tmp_path / "state"
    worker = CodexObserverWorker(source_root=root, state_dir=state, flush=False)
    worker.run_once()
    installed_at = worker._installed_at
    worker.close()
    rollout = root / "rollout-old.jsonl"
    _write(rollout, _session(installed_at - timedelta(days=1)))  # history from before installation
    _write(rollout, _session(installed_at + timedelta(minutes=5), 2))
    # An older collector already read the whole file (cursor at the end) but never uploaded the Skill.
    worker = CodexObserverWorker(source_root=root, state_dir=state, flush=False)
    worker._initialize_high_water(worker._files())
    from cowork_runtime.adapters.registry import CodexCursor
    from cowork_runtime.observer.codex_worker import INSTALLATION_ID, _file_identity
    worker.registry.save_cursor(CodexCursor(installation_id=INSTALLATION_ID, source_path=str(rollout),
        file_identity=_file_identity(rollout), offset=rollout.stat().st_size, updated_at=datetime.now(timezone.utc)))
    (state / SKILL_BACKFILL_MARKER).unlink()
    assert not worker.run_once().errors
    queued = _queued(worker)
    assert queued.count(("skill", "test-driven-development")) == 1  # only the read after installation
    assert (state / SKILL_BACKFILL_MARKER).exists()
    assert worker.run_once().events == 0  # once only
    worker.close()


def test_coverage_gap_flags_records_that_look_like_calls_but_were_not_reported():
    assert coverage_gaps({"skill_signals": 10, "skill_reported": 0}, None) == ["coverage_gap:skill"]
    assert coverage_gaps({"skill_signals": 10, "skill_reported": 9}, None) == []
    assert coverage_gaps({"mcp_signals": 2, "mcp_reported": 0}, None) == []  # too few to judge
    previous = {"skill_signals": 10, "skill_reported": 0}
    assert coverage_gaps({"skill_signals": 15, "skill_reported": 6}, previous) == []  # judged since the last record
