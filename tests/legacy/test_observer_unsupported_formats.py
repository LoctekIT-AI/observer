from __future__ import annotations

import json
from pathlib import Path

import pytest

from cowork_contracts import COMPONENT_EVENT_CONTRACT_VERSION, PluginManifestV1
from cowork_runtime.adapters.claude_desktop import ClaudeDesktopAdapter, ClaudeDesktopParseError
from cowork_runtime.adapters.codex import CodexIncrementalAdapter, CodexParseError
from cowork_runtime.adapters.registry import PluginRegistry
from cowork_runtime.adapters.workbuddy_desktop import WorkBuddyDesktopAdapter, WorkBuddyParseError
from cowork_runtime.persistence.sqlite_store import SQLiteRuntimeStore


def test_workbuddy_unknown_schema_is_counted_as_unrecognized(tmp_path: Path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.sqlite3")
    registry = PluginRegistry(store)
    registry.register(PluginManifestV1(
        plugin_id="workbuddy-desktop-adapter",
        runtime="workbuddy",
        contract_version=COMPONENT_EVENT_CONTRACT_VERSION,
        installation_id="install-workbuddy-desktop-1",
        enabled=True,
        capabilities=["tool_calls", "hooks", "desktop", "source:official-workbuddy-command-hook"],
    ))
    source = tmp_path / "workbuddy-hook-events.jsonl"
    source.write_text(json.dumps({
        "schema_version": "workbuddy_hook_event.v2",
        "stage": "completed",
        "session_id": "session-1",
        "invocation_id": "call-1",
        "tool_name": "mcp__demo__search",
    }) + "\n", encoding="utf-8")

    result = WorkBuddyDesktopAdapter(registry).read(
        source,
        installation_id="install-workbuddy-desktop-1",
        session_id="session-1",
    )
    [event] = result.events
    assert (event.component_kind, event.evidence_level) == ("unknown", "inferred")
    store.close()


def test_codex_unknown_tool_payload_is_counted_as_unrecognized_call(tmp_path: Path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.sqlite3")
    registry = PluginRegistry(store)
    registry.register(PluginManifestV1(
        plugin_id="codex-adapter",
        runtime="codex",
        contract_version=COMPONENT_EVENT_CONTRACT_VERSION,
        installation_id="install-codex-1",
        enabled=True,
        capabilities=["tool_calls", "rollout_jsonl"],
    ))
    source = tmp_path / "rollout.jsonl"
    source.write_text(json.dumps({
        "type": "response_item", "timestamp": "2026-10-14T08:00:00.000Z",
        "payload": {"type": "future_mcp_tool_invocation", "call_id": "call-1"},
    }) + "\n", encoding="utf-8")

    result = CodexIncrementalAdapter(registry).read(source, installation_id="install-codex-1", session_id="session-1")

    [event] = result.events
    assert (event.component_kind, event.evidence_level, event.stage) == ("unknown", "inferred", "completed")
    assert event.component_id == "unrecognized:future_mcp_tool_invocation"
    assert [d.code for d in result.diagnostics] == ["unrecognized_record"]
    store.close()

