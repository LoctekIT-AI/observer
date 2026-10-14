from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from cowork_contracts import COMPONENT_EVENT_CONTRACT_VERSION, PluginManifestV1
from cowork_runtime.adapters.registry import PluginRegistry
from cowork_runtime.adapters.workbuddy_desktop import WorkBuddyDesktopAdapter, WorkBuddyParseError
from cowork_runtime.persistence.sqlite_store import SQLiteRuntimeStore


def _manifest() -> PluginManifestV1:
    return PluginManifestV1(
        plugin_id="workbuddy-desktop-adapter",
        runtime="workbuddy",
        contract_version=COMPONENT_EVENT_CONTRACT_VERSION,
        installation_id="install-workbuddy-desktop-1",
        enabled=True,
        capabilities=["tool_calls", "hooks", "desktop", "source:official-workbuddy-command-hook"],
    )


def _record(
    *,
    stage: str,
    invocation_id: str,
    tool_name: str = "mcp__demo__search",
    session_id: str = "session-1",
    tool_result: str | None = None,
) -> dict[str, object]:
    hook_event = {
        "started": "PreToolUse",
        "completed": "PostToolUse",
        "failed": "PostToolUseFailure",
    }[stage]
    record = {
        "schema_version": "workbuddy_hook_event.v1",
        "occurred_at": "2026-09-11T02:00:00Z",
        "stage": stage,
        "session_id": session_id,
        "invocation_id": invocation_id,
        "tool_name": tool_name,
        "hook_event_name": hook_event,
    }
    if tool_result is not None:
        record["tool_result"] = tool_result
    return record


def _write_jsonl(path, records: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _adapter(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.sqlite3")
    registry = PluginRegistry(store)
    registry.register(_manifest())
    return store, WorkBuddyDesktopAdapter(registry)


def test_workbuddy_hook_adapter_maps_explicit_tool_lifecycle_without_bodies(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "workbuddy-hook-events.jsonl"
    _write_jsonl(path, [_record(stage="started", invocation_id="tool-use-1"), _record(stage="completed", invocation_id="tool-use-1")])

    result = adapter.read(path, installation_id="install-workbuddy-desktop-1", session_id="session-1")

    assert [event.stage for event in result.events] == ["started", "completed"]
    assert [event.component_kind for event in result.events] == ["mcp_tool", "mcp_tool"]
    assert result.events[0].runtime == "workbuddy"
    assert result.events[0].plugin_id == "workbuddy-desktop-adapter"
    assert result.events[0].source_type == "hook_jsonl"
    assert result.events[0].evidence_level == "verified"
    assert result.events[0].invocation_id == result.events[1].invocation_id == "tool-use-1"
    assert {"tool_input", "tool_response", "prompt", "output"}.isdisjoint(result.events[0].model_dump())
    store.close()


def test_workbuddy_hook_adapter_rejects_terminal_without_explicit_correlation(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "workbuddy-hook-events.jsonl"
    _write_jsonl(path, [_record(stage="completed", invocation_id="")])

    with pytest.raises(WorkBuddyParseError, match="invocation_id"):
        adapter.read(path, installation_id="install-workbuddy-desktop-1", session_id="session-1")
    store.close()


def test_workbuddy_hook_adapter_classifies_native_skill_tool(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "workbuddy-hook-events.jsonl"
    _write_jsonl(path, [
        _record(stage="started", invocation_id="skill-1", tool_name="Skill"),
        _record(stage="completed", invocation_id="skill-1", tool_name="Skill"),
    ])

    result = adapter.read(path, installation_id="install-workbuddy-desktop-1", session_id="session-1")

    assert [event.component_kind for event in result.events] == ["skill", "skill"]
    assert [event.observation_kind for event in result.events] == ["skill_call", "skill_call"]
    store.close()


def test_workbuddy_hook_adapter_extracts_skill_id_from_official_skill_result(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "workbuddy-hook-events.jsonl"
    skill_result = "Base directory for this skill: /Users/ui/.workbuddy/plugins/cache/observer/skills/observer-workspace\n"
    _write_jsonl(path, [
        _record(stage="started", invocation_id="skill-1", tool_name="Skill"),
        _record(stage="completed", invocation_id="skill-1", tool_name="Skill", tool_result=skill_result),
    ])

    result = adapter.read(path, installation_id="install-workbuddy-desktop-1", session_id="session-1")

    assert {event.component_id for event in result.events} == {"observer-workspace"}
    store.close()


def test_workbuddy_hook_adapter_is_incremental_and_session_scoped(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "workbuddy-hook-events.jsonl"
    _write_jsonl(path, [_record(stage="started", invocation_id="a", session_id="session-a"), _record(stage="completed", invocation_id="b", session_id="session-b")])

    first = adapter.read(path, installation_id="install-workbuddy-desktop-1", session_id="session-a")
    second = adapter.read(path, installation_id="install-workbuddy-desktop-1", session_id="session-a")

    assert len(first.events) == 1
    assert first.events[0].invocation_id == "a"
    assert second.events == ()
    assert first.next_cursor.offset == path.stat().st_size
    store.close()


def test_workbuddy_hook_adapter_replays_replaced_ledger_for_idempotent_delivery(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "workbuddy-hook-events.jsonl"
    original = [_record(stage="started", invocation_id="call-1"), _record(stage="completed", invocation_id="call-1")]
    _write_jsonl(path, original)

    first = adapter.read(path, installation_id="install-workbuddy-desktop-1", session_id="session-1")
    replacement = tmp_path / "replacement.jsonl"
    _write_jsonl(replacement, [*original, _record(stage="started", invocation_id="call-2"), _record(stage="completed", invocation_id="call-2")])
    replacement.replace(path)

    replayed = adapter.read(path, installation_id="install-workbuddy-desktop-1", session_id="session-1")

    assert len(first.events) == 2
    assert {(event.invocation_id, event.stage) for event in replayed.events} == {
        ("call-1", "started"), ("call-1", "completed"),
        ("call-2", "started"), ("call-2", "completed"),
    }
    assert replayed.next_cursor.file_identity != first.next_cursor.file_identity
    store.close()


def test_workbuddy_hook_adapter_rebuilds_start_context_for_late_terminal(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "workbuddy-hook-events.jsonl"
    _write_jsonl(path, [_record(stage="started", invocation_id="late-call")])

    first = adapter.read(path, installation_id="install-workbuddy-desktop-1", session_id="session-1")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_record(stage="completed", invocation_id="late-call")) + "\n")

    appended = adapter.read(path, installation_id="install-workbuddy-desktop-1", session_id="session-1")

    assert len(first.events) == 1
    assert len(appended.events) == 1
    assert appended.events[0].started_at == first.events[0].occurred_at
    store.close()


def test_workbuddy_hook_adapter_marks_host_generated_start_id_partial(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "workbuddy-hook-events.jsonl"
    _write_jsonl(path, [_record(stage="started", invocation_id="workbuddy-start:abc")])

    result = adapter.read(path, installation_id="install-workbuddy-desktop-1", session_id="session-1")

    assert result.events[0].evidence_level == "partial"
    store.close()


def test_registry_rejects_unofficial_workbuddy_web_source(tmp_path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.sqlite3")
    registry = PluginRegistry(store)
    manifest = PluginManifestV1(
        plugin_id="workbuddy-web-adapter",
        runtime="workbuddy",
        contract_version=COMPONENT_EVENT_CONTRACT_VERSION,
        installation_id="install-workbuddy-web-1",
        enabled=True,
        capabilities=["web", "hooks"],
    )

    with pytest.raises(ValueError, match="official WorkBuddy Desktop|desktop"):
        registry.register(manifest)
    store.close()


def test_registry_rejects_unknown_workbuddy_source_declaration(tmp_path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.sqlite3")
    registry = PluginRegistry(store)
    manifest = PluginManifestV1(
        plugin_id="workbuddy-desktop-adapter",
        runtime="workbuddy",
        contract_version=COMPONENT_EVENT_CONTRACT_VERSION,
        installation_id="install-workbuddy-unknown-1",
        enabled=True,
        capabilities=["desktop", "hooks", "source:sqlite"],
    )

    with pytest.raises(ValueError, match="official WorkBuddy Desktop|source"):
        registry.register(manifest)
    store.close()
