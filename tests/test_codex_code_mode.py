"""Codex code mode: Skill / MCP calls wrapped inside the built-in ``exec`` tool (v0.2.37)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from cowork_contracts import COMPONENT_EVENT_CONTRACT_VERSION, PluginManifestV1
from cowork_runtime.adapters.codex import (
    CodexIncrementalAdapter,
    _code_mode_commands,
    _code_mode_mcp_tools,
)
from cowork_runtime.adapters.registry import PluginRegistry
from cowork_runtime.persistence.sqlite_store import SQLiteRuntimeStore

NOW = datetime(2026, 10, 19, 6, 0, tzinfo=timezone.utc)


def _adapter(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    registry = PluginRegistry(SQLiteRuntimeStore(tmp_path / "runtime.sqlite3"))
    registry.register(PluginManifestV1(
        plugin_id="codex-adapter", runtime="codex", contract_version=COMPONENT_EVENT_CONTRACT_VERSION,
        installation_id="install-codex-1", enabled=True, capabilities=["tool_calls", "usage"]))
    return CodexIncrementalAdapter(registry)


def _exec(call_id: str, source: str, *, status: str = "completed", ts: str = "2026-10-19T05:00:00.000Z"):
    return {"timestamp": ts, "type": "response_item", "payload": {
        "type": "custom_tool_call", "id": f"ctc_{call_id}", "status": status, "call_id": call_id,
        "name": "exec", "input": source, "internal_chat_message_metadata_passthrough": {}}}


def _exec_output(call_id: str):
    return {"timestamp": "2026-10-19T05:00:01.000Z", "type": "response_item", "payload": {
        "type": "custom_tool_call_output", "id": f"cto_{call_id}", "call_id": call_id,
        "output": "body is never read", "internal_chat_message_metadata_passthrough": {}}}


ADDITIONAL_TOOLS = {"timestamp": "2026-10-19T04:59:59.000Z", "type": "response_item", "payload": {
    "type": "additional_tools", "id": "at_1", "role": "developer",
    "tools": [{"type": "namespace", "name": "mcp__cua_repl", "description": "x",
               "tools": [{"name": "js", "parameters": {"type": "object", "additionalProperties": False}}]}]},
    "metadata": {"client_authored": True, "mcp_attribution": {"status": "unattributed"}}}


def _write(path, records):
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def test_code_mode_helpers_parse_commands_and_mcp_calls():
    source = (
        'const r = await tools.exec_command({cmd:"cat /Users/ui/.agents/skills/using-superpowers/SKILL.md",workdir:"/x"});\n'
        "const rs = await Promise.allSettled([\n"
        "tools.exec_command({\"cmd\":'cat /Users/ui/.agents/skills/a/SKILL.md; cat /Users/ui/.codex/skills/b/SKILL.md'}),\n"
        "tools.mcp__codex_app__list_artifacts({})\n]);\n"
        'text(await tools["mcp__codex_app__create_worktree"]({name:"w"}));\n'
        "tools.exec_command({cmd:`ls ${dir}`});\n"
    )
    assert _code_mode_commands(source) == (
        "cat /Users/ui/.agents/skills/using-superpowers/SKILL.md",
        "cat /Users/ui/.agents/skills/a/SKILL.md; cat /Users/ui/.codex/skills/b/SKILL.md",
    )
    assert _code_mode_mcp_tools(source) == ("mcp__codex_app__list_artifacts", "mcp__codex_app__create_worktree")


def test_exec_block_reports_skills_and_mcp_and_tool_list_is_not_a_call(tmp_path):
    rollout = tmp_path / "rollout-2026-10-19T13-26-23-session-1.jsonl"
    _write(rollout, [
        ADDITIONAL_TOOLS,
        _exec("call-1", 'await tools.exec_command({cmd:"cat /Users/ui/.agents/skills/using-superpowers/SKILL.md"});'),
        _exec_output("call-1"),
        _exec("call-2", 'tools.exec_command({cmd:"sed -n 1,80p /Users/ui/.agents/skills/test-driven-development/SKILL.md"});\n'
                        'tools.mcp__codex_app__create_worktree({name:"w"}); tools.mcp__codex_app__create_worktree({name:"v"});'),
        _exec_output("call-2"),
        _exec("call-3", 'await tools.exec_command({cmd:"git status"}); await tools.apply_patch("*** Begin Patch");'),
        _exec_output("call-3"),
    ])
    result = _adapter(tmp_path).read(rollout, installation_id="install-codex-1", session_id="session-1", now=NOW)

    assert not result.diagnostics
    assert not [event for event in result.events if event.component_kind == "unknown"]
    skills = [event for event in result.events if event.component_kind == "skill"]
    assert [event.component_id for event in skills] == ["using-superpowers", "test-driven-development"]
    assert {event.evidence_source for event in skills} == {"codex_code_mode"}
    assert all(event.user_input is None and event.model_output is None and event.tool_result is None for event in skills)
    mcp = [event for event in result.events if event.component_kind == "mcp_tool"]
    assert [(event.component_id, event.stage) for event in mcp] == [
        ("mcp__codex_app__create_worktree", "started"), ("mcp__codex_app__create_worktree", "completed"),
        ("mcp__codex_app__create_worktree", "started"), ("mcp__codex_app__create_worktree", "completed"),
    ]
    assert len({event.invocation_id for event in mcp}) == 2
    assert all(event.parent_invocation_id == "call-2" and event.evidence_level == "inferred" for event in mcp)


def test_code_mode_events_are_stable_across_rereads(tmp_path):
    rollout = tmp_path / "rollout-session-2.jsonl"
    _write(rollout, [ADDITIONAL_TOOLS, _exec("call-9", 'tools.exec_command({cmd:"cat /a/skills/x/SKILL.md"}); tools.mcp__s__t({});')])
    first = _adapter(tmp_path / "a").read(rollout, installation_id="install-codex-1", session_id="session-2", now=NOW, persist_cursor=False)
    second = _adapter(tmp_path / "b").read(rollout, installation_id="install-codex-1", session_id="session-2", now=NOW, persist_cursor=False)
    picked = lambda result: sorted(e.event_id for e in result.events if e.component_kind in {"skill", "mcp_tool"})
    assert picked(first) == picked(second) and len(picked(first)) == 3
