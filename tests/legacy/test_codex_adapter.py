from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from cowork_contracts import COMPONENT_EVENT_CONTRACT_VERSION, PluginManifestV1
from cowork_runtime.adapters.codex import (
    CodexIncrementalAdapter,
    CodexParseError,
)
from cowork_runtime.adapters.registry import PluginRegistry
from cowork_runtime.persistence.sqlite_store import SQLiteRuntimeStore


NOW = "2026-09-06T12:00:00+00:00"


def _manifest(*, installation_id: str = "install-codex-1") -> PluginManifestV1:
    return PluginManifestV1(
        plugin_id="codex-adapter",
        runtime="codex",
        contract_version=COMPONENT_EVENT_CONTRACT_VERSION,
        installation_id=installation_id,
        enabled=True,
        capabilities=["tool_calls", "usage"],
    )


def _start(
    call_id: str,
    name: str,
    *,
    session_id: str = "session-1",
    status: str | None = None,
    payload_timestamp: bool = True,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "type": "custom_tool_call",
        "call_id": call_id,
        "name": name,
        "session_id": session_id,
        "arguments": {"secret": "must-not-be-emitted"},
    }
    if payload_timestamp:
        payload["started_at"] = NOW
    if status is not None:
        payload["status"] = status
    return {
        "type": "response_item",
        "payload": payload,
    }


def _result(
    call_id: str,
    *,
    status: str = "completed",
    session_id: str = "session-1",
    error_code: str | None = None,
) -> dict[str, object]:
    return {
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call_output",
            "call_id": call_id,
            "session_id": session_id,
            "status": status,
            "occurred_at": NOW,
            "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
            "error_code": error_code,
            "output": "secret result body",
        },
    }


def _function_start(call_id: str, namespace: str, name: str) -> dict[str, object]:
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "call_id": call_id,
            "namespace": namespace,
            "name": name,
            "arguments": "{\"secret\":\"must-not-be-emitted\"}",
        },
    }


def _function_result(call_id: str) -> dict[str, object]:
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": "secret result body",
        },
    }


def _skill_selection(name: str, path: str) -> dict[str, object]:
    return {
        "type": "response_item",
        "timestamp": NOW,
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "skill", "name": name, "path": path}],
        },
    }


def _skill_file_read(call_id: str, skill_path: str) -> dict[str, object]:
    return {
        "type": "response_item",
        "timestamp": NOW,
        "payload": {
            "type": "function_call",
            "call_id": call_id,
            "name": "exec_command",
            "arguments": json.dumps({"cmd": f"sed -n '1,240p' {skill_path}"}),
        },
    }


def _write_jsonl(path, records: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _adapter(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.sqlite3")
    registry = PluginRegistry(store)
    registry.register(_manifest())
    return store, CodexIncrementalAdapter(registry)


def test_codex_adapter_maps_mcp_and_generic_exec_calls_with_usage(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "rollout.jsonl"
    _write_jsonl(
        path,
        [
            _start("call-mcp", "mcp__demo__search"),
            _result("call-mcp"),
            _start("call-exec", "exec"),
            _result("call-exec", status="failed", error_code="tool_failed"),
        ],
    )

    result = adapter.read(path, installation_id="install-codex-1", session_id="session-1")

    assert [event.component_kind for event in result.events] == [
        "mcp_tool",
        "mcp_tool",
        "tool",
        "tool",
    ]
    assert [event.stage for event in result.events] == ["started", "completed", "started", "failed"]
    assert result.events[0].component_id == "mcp__demo__search"
    assert result.events[0].plugin_id == "codex-adapter"
    assert result.events[0].installation_id == "install-codex-1"
    assert result.events[1].total_tokens == 18
    assert result.events[3].error_code == "tool_failed"
    assert result.next_cursor.offset == path.stat().st_size
    store.close()


def test_repeated_reads_and_append_only_reads_are_idempotent(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "rollout.jsonl"
    _write_jsonl(path, [_start("call-1", "mcp__demo__search"), _result("call-1")])

    first = adapter.read(path, installation_id="install-codex-1", session_id="session-1")
    second = adapter.read(path, installation_id="install-codex-1", session_id="session-1")
    assert len(first.events) == 2
    assert second.events == ()

    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_start("call-2", "exec")) + "\n")
        handle.write(json.dumps(_result("call-2")) + "\n")
    appended = adapter.read(path, installation_id="install-codex-1", session_id="session-1")
    assert len(appended.events) == 2
    assert {event.invocation_id for event in appended.events} == {"call-2"}
    store.close()


def test_codex_adapter_can_defer_cursor_commit_until_events_are_persisted(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "rollout.jsonl"
    _write_jsonl(path, [_start("call-1", "mcp__demo__search"), _result("call-1")])

    result = adapter.read(
        path,
        installation_id="install-codex-1",
        session_id="session-1",
        persist_cursor=False,
    )

    assert len(result.events) == 2
    assert store.get_adapter_cursor("install-codex-1", str(path.resolve())) is None
    store.close()


def test_codex_adapter_replays_replaced_rollout_for_idempotent_delivery(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "rollout.jsonl"
    original = [_start("call-1", "mcp__demo__search"), _result("call-1")]
    _write_jsonl(path, original)

    first = adapter.read(path, installation_id="install-codex-1", session_id="session-1")
    replacement = tmp_path / "replacement.jsonl"
    _write_jsonl(replacement, [*original, _start("call-2", "exec"), _result("call-2")])
    replacement.replace(path)

    replayed = adapter.read(path, installation_id="install-codex-1", session_id="session-1")

    assert len(first.events) == 2
    assert {(event.invocation_id, event.stage) for event in replayed.events} == {
        ("call-1", "started"), ("call-1", "completed"),
        ("call-2", "started"), ("call-2", "completed"),
    }
    assert replayed.next_cursor.file_identity != first.next_cursor.file_identity
    store.close()


def test_append_only_read_rebuilds_pending_custom_tool_start_context(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "split-rollout.jsonl"
    _write_jsonl(path, [_start("call-split", "mcp__demo__search")])

    first = adapter.read(path, installation_id="install-codex-1", session_id="session-1")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_result("call-split")) + "\n")

    appended = adapter.read(path, installation_id="install-codex-1", session_id="session-1")

    assert [event.stage for event in first.events] == ["started"]
    assert [event.stage for event in appended.events] == ["completed"]
    assert appended.events[0].invocation_id == "call-split"
    store.close()


def test_codex_adapter_maps_native_namespaced_mcp_function_call(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "mcp-function.jsonl"
    _write_jsonl(path, [
        _function_start("call-mcp-function", "mcp__node_repl", "js"),
        _function_result("call-mcp-function"),
    ])

    result = adapter.read(path, installation_id="install-codex-1", session_id="session-1")

    assert [event.component_kind for event in result.events] == ["mcp_tool", "mcp_tool"]
    assert [event.component_id for event in result.events] == ["mcp__node_repl__js", "mcp__node_repl__js"]
    assert [event.stage for event in result.events] == ["started", "completed"]
    store.close()


def test_codex_adapter_ignores_non_mcp_function_records_without_identifiers(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "internal-function.jsonl"
    _write_jsonl(path, [{"type": "response_item", "payload": {"type": "function_call", "id": "fc-1", "call_id": "", "name": ""}}])

    result = adapter.read(path, installation_id="install-codex-1", session_id="session-1")

    assert result.events == ()
    store.close()


def test_codex_adapter_classifies_native_skill_tool(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "skill.jsonl"
    _write_jsonl(path, [_start("call-skill", "Skill"), _result("call-skill")])

    result = adapter.read(path, installation_id="install-codex-1", session_id="session-1")

    assert [event.component_kind for event in result.events] == ["skill", "skill"]
    assert [event.observation_kind for event in result.events] == ["skill_call", "skill_call"]
    store.close()


def test_codex_adapter_records_structured_skill_selection_as_partial_observation(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "skill-selection.jsonl"
    secret_path = "/private/project/skills/openai-docs/SKILL.md"
    _write_jsonl(path, [_skill_selection("openai-docs", secret_path)])

    result = adapter.read(path, installation_id="install-codex-1", session_id="session-1")

    assert len(result.events) == 1
    event = result.events[0]
    assert event.component_kind == "skill"
    assert event.component_id == "openai-docs"
    assert event.stage == "started"
    assert event.observation_kind == "skill_selected"
    assert event.evidence_source == "codex_skill_user_input"
    assert event.evidence_level == "partial"
    assert secret_path not in json.dumps(event.model_dump(mode="json"))
    store.close()


def test_codex_adapter_maps_native_app_server_skill_turn_lifecycle(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "app-server-skill.jsonl"
    _write_jsonl(path, [
        {
            "method": "item/started",
            "params": {
                "threadId": "01a0a960-8949-7c23-a760-c94e683de9a7",
                "turnId": "01a0a960-89e6-7f41-8665-bce0959c0084",
                "startedAtMs": 1789548071767,
                "item": {
                    "type": "userMessage",
                    "id": "01a0a960-8b57-7d93-b6fb-c1700d78c33f",
                    "content": [{
                        "type": "skill",
                        "name": "skill-mcp-observer:skill-mcp-observer",
                        "path": "/Users/ui/.codex/plugins/cache/personal/skill-mcp-observer/0.1.0/skills/skill-mcp-observer/SKILL.md",
                    }],
                },
            },
        },
        {
            "method": "turn/completed",
            "params": {
                "threadId": "01a0a960-8949-7c23-a760-c94e683de9a7",
                "turn": {
                    "id": "01a0a960-89e6-7f41-8665-bce0959c0084",
                    "status": "completed",
                    "completedAt": 1789548081,
                },
            },
        },
        {
            "method": "item/started",
            "params": {
                "threadId": "another-thread",
                "turnId": "another-turn",
                "startedAtMs": 1789548071767,
                "item": {
                    "type": "userMessage",
                    "id": "another-message",
                    "content": [{
                        "type": "skill",
                        "name": "must-not-leak",
                        "path": "/private/other/SKILL.md",
                    }],
                },
            },
        },
    ])

    result = adapter.read(
        path,
        installation_id="install-codex-1",
        session_id="01a0a960-8949-7c23-a760-c94e683de9a7",
    )

    assert [(event.component_id, event.stage, event.evidence_level) for event in result.events] == [
        ("skill-mcp-observer:skill-mcp-observer", "started", "verified"),
        ("skill-mcp-observer:skill-mcp-observer", "completed", "verified"),
    ]
    assert all(event.observation_kind == "skill_call" for event in result.events)
    assert all(event.evidence_source == "codex_app_server" for event in result.events)
    serialized = json.dumps([event.model_dump(mode="json") for event in result.events])
    assert "SKILL.md" not in serialized
    assert "must-not-leak" not in serialized
    store.close()


def test_codex_adapter_maps_native_app_server_mcp_failure_for_exact_thread(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "app-server-mcp.jsonl"
    _write_jsonl(path, [
        {
            "method": "item/started",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "startedAtMs": 1789547947300,
                "item": {
                    "type": "mcpToolCall",
                    "id": "exec-a07812f4-bb0e-4871-bac3-2fb6b6003366",
                    "server": "observer-workspace",
                    "tool": "observer_workspace_check",
                    "status": "inProgress",
                    "arguments": {"secret": "must-not-be-emitted"},
                },
            },
        },
        {
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "completedAtMs": 1789547947303,
                "item": {
                    "type": "mcpToolCall",
                    "id": "exec-a07812f4-bb0e-4871-bac3-2fb6b6003366",
                    "server": "observer-workspace",
                    "tool": "observer_workspace_check",
                    "status": "failed",
                    "result": "must-not-be-emitted",
                    "error": {"message": "MCP tool call requires approval, but approval policy is never"},
                },
            },
        },
        {
            "method": "item/started",
            "params": {
                "threadId": "another-thread",
                "turnId": "another-turn",
                "startedAtMs": 1789547947300,
                "item": {
                    "type": "mcpToolCall",
                    "id": "must-not-leak",
                    "server": "other-server",
                    "tool": "other-tool",
                    "status": "inProgress",
                },
            },
        },
    ])

    result = adapter.read(path, installation_id="install-codex-1", session_id="thread-1")

    assert [(event.component_id, event.stage, event.error_code) for event in result.events] == [
        ("mcp__observer-workspace__observer_workspace_check", "started", None),
        ("mcp__observer-workspace__observer_workspace_check", "failed", "tool_error"),
    ]
    assert all(event.observation_kind == "mcp_call" for event in result.events)
    assert all(event.evidence_source == "codex_app_server" for event in result.events)
    assert all(event.evidence_level == "verified" for event in result.events)
    serialized = json.dumps([event.model_dump(mode="json") for event in result.events])
    assert "must-not-be-emitted" not in serialized
    assert "approval policy" not in serialized
    assert "must-not-leak" not in serialized
    store.close()


def test_codex_adapter_records_structured_skill_file_read_as_inferred_observation(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "skill-read.jsonl"
    secret_path = "/Users/example/.agents/skills/systematic-debugging/SKILL.md"
    _write_jsonl(path, [_skill_file_read("call-read-skill", secret_path)])

    result = adapter.read(path, installation_id="install-codex-1", session_id="session-1")

    assert len(result.events) == 1
    event = result.events[0]
    assert event.component_kind == "skill"
    assert event.component_id == "systematic-debugging"
    assert event.stage == "started"
    assert event.observation_kind == "skill_file_read"
    assert event.evidence_source == "codex_tool_arguments"
    assert event.evidence_level == "inferred"
    assert secret_path not in json.dumps(event.model_dump(mode="json"))
    store.close()


def test_codex_adapter_does_not_infer_skill_from_message_text(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "skill-text.jsonl"
    _write_jsonl(path, [{
        "type": "response_item",
        "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Read demo/SKILL.md"}]},
    }])

    result = adapter.read(path, installation_id="install-codex-1", session_id="session-1")

    assert result.events == ()
    store.close()


def test_codex_adapter_maps_item_completed_mcp_lifecycle(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "item-completed-mcp.jsonl"
    _write_jsonl(path, [{
        "type": "event_msg", "timestamp": NOW,
        "payload": {
            "type": "item_completed", "started_at_ms": 1789344000000, "completed_at_ms": 1789344000100,
            "item": {"type": "McpToolCall", "id": "mcp-new-1", "server": "node_repl", "tool": "js", "status": "completed", "arguments": {"secret": True}, "result": "42"},
        },
    }])
    result = adapter.read(path, installation_id="install-codex-1", session_id="session-1")
    assert [(e.component_id, e.stage, e.evidence_level) for e in result.events] == [
        ("mcp__node_repl__js", "started", "verified"),
        ("mcp__node_repl__js", "completed", "verified"),
    ]
    serialized = json.dumps([e.model_dump(mode="json") for e in result.events])
    assert "secret" not in serialized
    store.close()


@pytest.mark.parametrize("split_after", [0, 1, 2])
def test_native_failed_mcp_completion_is_not_overwritten_by_later_output(tmp_path, split_after: int) -> None:
    """Sanitized real 2026-09-24 Desktop records; arguments/results omitted."""
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "native-failed-mcp.jsonl"
    call_id = "call_ee04AarqAvtw1hlnhvD1j5ZT"
    records = [
        {"timestamp": "2026-09-24T03:56:01.388Z", "type": "response_item", "payload": {
            "type": "function_call", "name": "js", "namespace": "mcp__cua_repl", "call_id": call_id,
        }},
        {"timestamp": "2026-09-24T03:56:01.502Z", "type": "event_msg", "payload": {
            "type": "item_completed", "started_at_ms": 1790222161498, "completed_at_ms": 1790222161502,
            "item": {"type": "McpToolCall", "id": call_id, "server": "cua_repl", "tool": "js", "status": "failed"},
        }},
        {"timestamp": "2026-09-24T03:56:02.329Z", "type": "response_item", "payload": {
            "type": "function_call_output", "call_id": call_id,
        }},
    ]
    if split_after:
        _write_jsonl(path, records[:split_after])
        first = adapter.read(path, installation_id="install-codex-1", session_id="session-1")
        # A new adapter proves recovery relies on cursor context, not memory.
        adapter = CodexIncrementalAdapter(PluginRegistry(store))
        with path.open("a", encoding="utf-8") as handle:
            for record in records[split_after:]:
                handle.write(json.dumps(record) + "\n")
        second = adapter.read(path, installation_id="install-codex-1", session_id="session-1")
        events = (*first.events, *second.events)
    else:
        _write_jsonl(path, records)
        events = adapter.read(path, installation_id="install-codex-1", session_id="session-1").events

    assert [(event.component_id, event.stage) for event in events if event.stage != "started"] == [("mcp__cua_repl__js", "failed")]
    failed = next(event for event in events if event.stage == "failed")
    assert failed.ended_at == datetime(2026, 9, 24, 3, 56, 1, 502000, tzinfo=timezone.utc)
    replay_path = tmp_path / "native-only.jsonl"
    _write_jsonl(replay_path, [records[1]])
    original_native_events = adapter.read(replay_path, installation_id="install-codex-1", session_id="session-1").events
    assert failed.event_id == next(event.event_id for event in original_native_events if event.stage == "failed")
    store.close()


def test_codex_adapter_maps_native_exec_json_mcp_lifecycle(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "native-exec-events.jsonl"
    records = [
        {"type": "thread.started", "thread_id": "native-thread-1"},
        {"type": "item.started", "item": {
            "id": "item-0", "type": "mcp_tool_call", "server": "observer-workspace",
            "tool": "observer_workspace_check", "arguments": {}, "result": None,
        }},
        {"type": "item.completed", "item": {
            "id": "item-0", "type": "mcp_tool_call", "server": "observer-workspace",
            "tool": "observer_workspace_check", "arguments": {},
            "result": {"content": [{"type": "text", "text": "workspace facts"}]},
            "error": None, "status": "completed",
        }},
    ]
    with path.open("w", encoding="utf-8") as handle:
        handle.write("Reading additional input from stdin...\n")
        for record in records:
            handle.write(json.dumps(record) + "\n")

    result = adapter.read(path, installation_id="install-codex-1", session_id="native-thread-1")

    assert [(event.component_id, event.stage, event.evidence_level) for event in result.events] == [
        ("mcp__observer-workspace__observer_workspace_check", "started", "verified"),
        ("mcp__observer-workspace__observer_workspace_check", "completed", "verified"),
    ]
    store.close()


def test_codex_adapter_ignores_native_exec_events_from_another_thread(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "native-exec-other-thread.jsonl"
    _write_jsonl(path, [
        {"type": "thread.started", "thread_id": "other-thread"},
        {"type": "item.started", "item": {
            "id": "item-0", "type": "mcp_tool_call", "server": "observer-workspace",
            "tool": "observer_workspace_check",
        }},
        {"type": "item.completed", "item": {
            "id": "item-0", "type": "mcp_tool_call", "server": "observer-workspace",
            "tool": "observer_workspace_check", "status": "completed",
        }},
    ])

    result = adapter.read(path, installation_id="install-codex-1", session_id="requested-thread")

    assert result.events == ()
    store.close()


def test_codex_adapter_infers_skill_read_from_item_completed_command_execution(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "item-completed-skill-read.jsonl"
    skill_path = "/Users/example/.codex/skills/.system/openai-docs/SKILL.md"
    _write_jsonl(path, [{
        "type": "event_msg", "timestamp": NOW,
        "payload": {
            "type": "item_completed", "started_at_ms": 1789346538995, "completed_at_ms": 1789346538995,
            "item": {
                "type": "CommandExecution", "id": "exec-skill-1", "status": "completed",
                "command": ["/bin/zsh", "-lc", f"sed -n '1,240p' {skill_path}"],
                "stdout": "secret skill contents",
            },
        },
    }])

    result = adapter.read(path, installation_id="install-codex-1", session_id="session-1")

    assert [(e.component_id, e.stage, e.evidence_level) for e in result.events] == [
        ("openai-docs", "started", "inferred"),
    ]
    event = result.events[0]
    assert event.observation_kind == "skill_file_read"
    assert event.evidence_source == "codex_command_execution"
    serialized = json.dumps(event.model_dump(mode="json"))
    assert skill_path not in serialized and "secret skill contents" not in serialized
    store.close()


def test_codex_adapter_attaches_real_interaction_and_tool_result_content(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "item-content.jsonl"
    _write_jsonl(path, [
        {"type": "event_msg", "payload": {"type": "item_completed", "started_at_ms": 1789346500000, "completed_at_ms": 1789346500000, "item": {"type": "UserMessage", "id": "u-1", "content": [{"type": "text", "text": "Find weather"}]}}},
        {"type": "event_msg", "payload": {"type": "item_completed", "started_at_ms": 1789346500100, "completed_at_ms": 1789346500100, "item": {"type": "AgentMessage", "id": "a-1", "content": [{"type": "text", "text": "It is sunny"}]}}},
        {"type": "event_msg", "payload": {"type": "item_completed", "started_at_ms": 1789346500200, "completed_at_ms": 1789346500300, "item": {"type": "McpToolCall", "id": "mcp-content-1", "server": "node_repl", "tool": "js", "status": "completed", "result": {"temperature": 23}}}},
    ])

    result = adapter.read(path, installation_id="install-codex-1", session_id="session-1")

    completed = [event for event in result.events if event.stage == "completed"]
    assert len(completed) == 1
    assert completed[0].user_input == "Find weather"
    assert completed[0].model_output == "It is sunny"
    assert completed[0].tool_result == '{"temperature":23}'
    store.close()


def test_codex_adapter_omits_tool_result_over_contract_limit(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "oversized-content.jsonl"
    _write_jsonl(path, [{
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "started_at_ms": 1789346500000,
            "completed_at_ms": 1789346500000,
            "item": {"type": "McpToolCall", "id": "mcp-large-1", "server": "node_repl", "tool": "js", "status": "completed", "result": "x" * 1_000_001},
        },
    }])

    result = adapter.read(path, installation_id="install-codex-1", session_id="session-1")

    assert next(event for event in result.events if event.stage == "completed").tool_result is None
    store.close()


def test_codex_adapter_collects_native_desktop_skill_selection_from_event_msg(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "desktop-skill-selection.jsonl"
    _write_jsonl(path, [
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "started_at_ms": 1789346500000,
                "completed_at_ms": 1789346500000,
                "item": {
                    "type": "UserMessage",
                    "id": "u-skill-1",
                    "content": [
                        {
                            "type": "skill",
                            "name": "skill-mcp-observer:skill-mcp-observer",
                            "path": "/Users/example/.codex/plugins/cache/personal/skill-mcp-observer/SKILL.md",
                        }
                    ],
                },
            },
        },
    ])

    result = adapter.read(path, installation_id="install-codex-1", session_id="session-1")

    assert [(event.component_kind, event.component_id, event.observation_kind, event.evidence_level) for event in result.events] == [
        ("skill", "skill-mcp-observer:skill-mcp-observer", "skill_selected", "partial"),
    ]
    store.close()


def test_codex_adapter_collects_current_desktop_skill_selection_marker(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "current-desktop-skill-selection.jsonl"
    _write_jsonl(path, [
        {
            "type": "response_item",
            "timestamp": NOW,
            "payload": {
                "type": "message",
                "role": "user",
                "internal_chat_message_metadata_passthrough": {
                    "content_item_kinds": ["skills.selected_skill_instructions"],
                },
                "content": [{
                    "type": "input_text",
                    "text": (
                        "<skill>\n"
                        "<name>skill-mcp-observer:skill-mcp-observer</name>\n"
                        "<path>/Users/ui/.codex/plugins/cache/personal/skill-mcp-observer/SKILL.md</path>\n"
                        "</skill>"
                    ),
                }],
            },
        },
    ])

    result = adapter.read(path, installation_id="install-codex-1", session_id="session-1")

    assert [(event.component_kind, event.component_id, event.observation_kind, event.evidence_level) for event in result.events] == [
        ("skill", "skill-mcp-observer:skill-mcp-observer", "skill_selected", "partial"),
    ]
    store.close()


def test_codex_adapter_attaches_agent_reply_that_follows_tool_result(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "item-content-natural-order.jsonl"
    user = {"type": "UserMessage", "id": "u-1", "content": [{"text": "Find weather"}]}
    tool = {"type": "McpToolCall", "id": "mcp-1", "server": "weather", "tool": "get", "status": "completed", "result": "23 C"}
    agent = {"type": "AgentMessage", "id": "a-1", "content": [{"text": "It is sunny"}]}
    records = []
    for index, item in enumerate((user, tool, agent)):
        timestamp = 1789346500000 + index * 100
        records.append({"type": "event_msg", "payload": {"type": "item_completed", "started_at_ms": timestamp, "completed_at_ms": timestamp, "item": item}})
    _write_jsonl(path, records)

    result = adapter.read(path, installation_id="install-codex-1", session_id="session-1")

    completed = next(event for event in result.events if event.stage == "completed")
    assert completed.user_input == "Find weather"
    assert completed.model_output == "It is sunny"
    assert completed.tool_result == "23 C"
    store.close()


def test_duplicate_item_completed_wrapper_is_ignored_and_sessions_are_isolated(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "rollout.jsonl"
    _write_jsonl(
        path,
        [
            _start("same-call", "exec", session_id="session-a"),
            {"type": "event_msg", "payload": {"type": "item_completed", "item": _start("same-call", "exec")["payload"]}},
            _result("same-call", session_id="session-a"),
        ],
    )
    result = adapter.read(path, installation_id="install-codex-1", session_id="session-a")
    assert len(result.events) == 2

    other_path = tmp_path / "other-rollout.jsonl"
    _write_jsonl(other_path, [_start("same-call", "exec", session_id="session-b"), _result("same-call", session_id="session-b")])
    other = adapter.read(other_path, installation_id="install-codex-1", session_id="session-b")
    assert len(other.events) == 2
    assert result.events[0].event_id != other.events[0].event_id
    store.close()


def test_malformed_json_exposes_path_and_byte_offset(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "broken.jsonl"
    path.write_bytes(b"{\"type\": \"response_item\"}\nnot-json\n")

    with pytest.raises(CodexParseError) as caught:
        adapter.read(path, installation_id="install-codex-1", session_id="session-1")

    assert caught.value.path == str(path.resolve())
    assert caught.value.offset > 0
    store.close()


def test_orphan_result_is_rejected_instead_of_guessing_component_identity(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "orphan.jsonl"
    _write_jsonl(path, [_result("orphan")])

    with pytest.raises(CodexParseError, match="matching tool start"):
        adapter.read(path, installation_id="install-codex-1", session_id="session-1")
    store.close()


def test_codex_rollout_uses_start_status_when_output_has_no_status(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "real-rollout-shape.jsonl"
    _write_jsonl(
        path,
        [
            _start("call-real", "exec", status="completed"),
            _result("call-real", status=None),
        ],
    )

    result = adapter.read(path, installation_id="install-codex-1", session_id="session-1")

    assert [event.stage for event in result.events] == ["started", "completed"]
    store.close()


def test_codex_rollout_uses_outer_timestamp_when_payload_has_no_timestamp(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "outer-timestamp-rollout.jsonl"
    start = _start("call-timestamp", "exec", status="completed", payload_timestamp=False)
    start["timestamp"] = "2026-09-08T09:10:11.123Z"
    result = _result("call-timestamp", status=None)
    result_payload = result["payload"]
    assert isinstance(result_payload, dict)
    result_payload.pop("occurred_at")
    result["timestamp"] = "2026-09-08T09:10:12.456Z"
    _write_jsonl(path, [start, result])

    parsed = adapter.read(path, installation_id="install-codex-1", session_id="session-1")

    assert parsed.events[0].occurred_at == datetime(2026, 9, 8, 9, 10, 11, 123000, tzinfo=timezone.utc)
    assert parsed.events[1].occurred_at == datetime(2026, 9, 8, 9, 10, 12, 456000, tzinfo=timezone.utc)
    store.close()


def test_event_model_does_not_contain_transcript_or_secret_fields(tmp_path) -> None:
    store, adapter = _adapter(tmp_path)
    path = tmp_path / "safe.jsonl"
    _write_jsonl(path, [_start("call-safe", "mcp__demo__search"), _result("call-safe")])
    result = adapter.read(path, installation_id="install-codex-1", session_id="session-1")

    forbidden = {"prompt", "arguments", "input", "output", "stdout", "stderr", "api_key", "token", "cookie"}
    for event in result.events:
        assert forbidden.isdisjoint(event.model_dump().keys())
    store.close()
