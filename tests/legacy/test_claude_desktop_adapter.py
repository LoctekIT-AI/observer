from __future__ import annotations

from datetime import datetime, timezone

from cowork_contracts import COMPONENT_EVENT_CONTRACT_VERSION, PluginManifestV1
from cowork_runtime.adapters.claude_desktop import ClaudeDesktopAdapter
from cowork_runtime.adapters.registry import PluginRegistry
from cowork_runtime.persistence.sqlite_store import SQLiteRuntimeStore


def _manifest() -> PluginManifestV1:
    return PluginManifestV1(
        plugin_id="claude-desktop-adapter",
        runtime="claude",
        contract_version=COMPONENT_EVENT_CONTRACT_VERSION,
        installation_id="install-claude-desktop-1",
        enabled=True,
        capabilities=["tool_calls", "desktop", "indexeddb"],
    )


def _record(call: str, name: str, stage: str, *, loaded_skill: str | None = None) -> bytes:
    occurred = "2026-09-22T02:03:18.886431Z" if stage == "started" else "2026-09-22T02:03:19.011472Z"
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


def _native_message_record(call: str, name: str, stage: str) -> bytes:
    timestamp = "2026-10-10T01:03:09.244Z"
    if stage == "started":
        return (
            f'created_at"\x18{timestamp}"\ncontenta\x05I\x00o"\x04type"\x08tool_use"'
            f'\x02id"\x1e{call}"\x04name"\n{name}"'
        ).encode()
    return (
        f'created_at"\x18{timestamp}"\ncontenta\x05I\x00o"\x04type"\x0btool_result"'
        f'\x0btool_use_id"\x1e{call}"\x04name"\n{name}"\x08is_errorF'
        '"\x07contenta\x00'
    ).encode()


def test_claude_desktop_maps_deferred_skill_loading_and_mcp(tmp_path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.sqlite3")
    registry = PluginRegistry(store)
    registry.register(_manifest())
    source = tmp_path / "000001.ldb"
    source.write_bytes(
        _record("search-1", "tool_search", "started")
        + _record("search-1", "tool_search", "completed", loaded_skill="observer-workspace")
        + _record("mcp-1", "+observer-workspace:observer_workspace_check", "started")
        + _record("mcp-1", "+observer-workspace:observer_workspace_check", "completed")
    )

    result = ClaudeDesktopAdapter(registry).read(
        source,
        installation_id="install-claude-desktop-1",
        session_id="desktop-session-1",
        skill_ids=("observer-workspace",),
        now=datetime(2026, 9, 22, tzinfo=timezone.utc),
    )

    assert len(result.events) == 5
    assert [event.component_kind for event in result.events].count("tool") == 2
    assert [event.component_kind for event in result.events].count("mcp_tool") == 2
    skill = next(event for event in result.events if event.component_kind == "skill")
    assert skill.component_id == "observer-workspace"
    assert skill.observation_kind == "skill_selected"
    assert skill.evidence_level == "partial"
    assert {event.component_id for event in result.events if event.component_kind == "mcp_tool"} == {
        "mcp__observer-workspace__observer_workspace_check"
    }
    store.close()


def test_claude_desktop_maps_native_lcl_mcp_name(tmp_path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.sqlite3")
    registry = PluginRegistry(store)
    registry.register(_manifest())
    source = tmp_path / "000001.ldb"
    source.write_bytes(
        _record("mcp-1", "/lcl-observer-workspace-observer_workspace_check", "started")
        + _record("mcp-1", "/lcl-observer-workspace-observer_workspace_check", "completed")
    )

    result = ClaudeDesktopAdapter(registry).read(
        source,
        installation_id="install-claude-desktop-1",
        session_id="desktop-session-1",
    )

    assert [event.component_kind for event in result.events] == ["mcp_tool", "mcp_tool"]
    assert {event.component_id for event in result.events} == {
        "mcp__observer-workspace__observer_workspace_check"
    }
    store.close()


def test_claude_desktop_maps_current_native_message_records(tmp_path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.sqlite3")
    registry = PluginRegistry(store)
    registry.register(_manifest())
    source = tmp_path / "000001.log"
    source.write_bytes(
        _native_message_record("mcp-1", "/lcl-observer-workspace-observer_workspace_check", "started")
        + _native_message_record("mcp-1", "/lcl-observer-workspace-observer_workspace_check", "completed")
    )

    result = ClaudeDesktopAdapter(registry).read(
        source,
        installation_id="install-claude-desktop-1",
        session_id="desktop-session-1",
    )

    assert [event.stage for event in result.events] == ["started", "completed"]
    assert {event.component_id for event in result.events} == {
        "mcp__observer-workspace__observer_workspace_check"
    }
    store.close()


def test_claude_desktop_maps_current_native_lcl_records_without_name_length_byte(tmp_path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.sqlite3")
    registry = PluginRegistry(store)
    registry.register(_manifest())
    source = tmp_path / "000004.log"
    source.write_bytes(
        b'created_at"\x182026-10-10T01:03:09.244Z"\ncontenta\x05I\x00o"\x04type"\x08tool_use"'
        b'\x02id"\x1dcblk_0114qppu3mV7E2QVDhN2Cz4A"\x11tool_display_name",Observer workspace: Observer workspace check'
        b'"\x04name"/lcl-observer-workspace-observer_workspace_check"\x05input'
        b'created_at"\x182026-10-10T01:03:09.244Z"\ncontenta\x05I\x00o"\x04type"\x0btool_result"'
        b'\x0btool_use_id"\x1dcblk_0114qppu3mV7E2QVDhN2Cz4A"\x04name"/lcl-observer-workspace-observer_workspace_check'
        b'"\x08is_errorF',
    )

    result = ClaudeDesktopAdapter(registry).read(
        source,
        installation_id="install-claude-desktop-1",
        session_id="desktop-session-1",
    )

    assert [event.stage for event in result.events] == ["started", "completed"]
    assert {event.component_id for event in result.events} == {
        "mcp__observer-workspace__observer_workspace_check"
    }
    store.close()


def test_claude_desktop_does_not_promote_unknown_loaded_tool_to_skill(tmp_path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.sqlite3")
    registry = PluginRegistry(store)
    registry.register(_manifest())
    source = tmp_path / "000001.ldb"
    source.write_bytes(
        _record("search-1", "tool_search", "started")
        + _record("search-1", "tool_search", "completed", loaded_skill="unknown")
    )

    result = ClaudeDesktopAdapter(registry).read(
        source,
        installation_id="install-claude-desktop-1",
        session_id="desktop-session-1",
        skill_ids=("observer-workspace",),
    )

    assert not [event for event in result.events if event.component_kind == "skill"]
    store.close()


def test_claude_desktop_can_defer_cursor_commit_until_events_are_persisted(tmp_path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.sqlite3")
    registry = PluginRegistry(store)
    registry.register(_manifest())
    source = tmp_path / "000001.log"
    source.write_bytes(
        _record("mcp-1", "+observer-workspace:observer_workspace_check", "started")
        + _record("mcp-1", "+observer-workspace:observer_workspace_check", "completed")
    )

    result = ClaudeDesktopAdapter(registry).read(
        source,
        installation_id="install-claude-desktop-1",
        session_id="desktop-session-1",
        persist_cursor=False,
    )

    assert len(result.events) == 2
    assert registry.cursor("install-claude-desktop-1", str(source)) is None
    registry.save_cursor(result.next_cursor)
    assert registry.cursor("install-claude-desktop-1", str(source)).offset == source.stat().st_size
    store.close()


def test_claude_desktop_rebuilds_tool_start_context_for_appended_result(tmp_path) -> None:
    store = SQLiteRuntimeStore(tmp_path / "runtime.sqlite3")
    registry = PluginRegistry(store)
    registry.register(_manifest())
    source = tmp_path / "000001.log"
    started = _record("mcp-1", "+observer-workspace:observer_workspace_check", "started")
    completed = _record("mcp-1", "+observer-workspace:observer_workspace_check", "completed")
    source.write_bytes(started)
    first = ClaudeDesktopAdapter(registry).read(
        source,
        installation_id="install-claude-desktop-1",
        session_id="desktop-session-1",
    )
    assert [event.stage for event in first.events] == ["started"]
    source.write_bytes(started + completed)
    second = ClaudeDesktopAdapter(registry).read(
        source,
        installation_id="install-claude-desktop-1",
        session_id="desktop-session-1",
    )
    assert [event.stage for event in second.events] == ["completed"]
    store.close()


def _v8(value: str) -> bytes:
    raw = value.encode()
    assert len(raw) < 0x80
    return b'"' + bytes([len(raw)]) + raw


def _current_use(call: str, name: str, *, skill: str | None = None) -> bytes:
    tool_input = b"o" + (_v8("skill") + _v8(skill) if skill else b"") + b"{" + (b"\x01" if skill else b"\x00")
    return (
        _v8("start_timestamp") + _v8("2026-10-13T07:20:01.000Z") + _v8("flags") + b"{\x02"
        + b"o" + _v8("type") + _v8("tool_use") + _v8("id") + _v8(call) + _v8("name") + _v8(name)
        + _v8("tool_display_name") + _v8("Display Name") + _v8("input") + tool_input
        + _v8("message") + _v8("user visible text") + b"{\x06"
    )


def _current_result(call: str, name: str, *, failed: bool = False) -> bytes:
    return (
        b"o" + _v8("type") + _v8("tool_result") + _v8("tool_use_id") + _v8(call) + _v8("name") + _v8(name)
        + _v8("is_error") + (b"T" if failed else b"F") + _v8("content") + b"A\x01" + _v8("private result") + b"$\x00\x01{\x05"
    )


def _adapter(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.sqlite3")
    registry = PluginRegistry(store)
    registry.register(_manifest())
    return store, registry, ClaudeDesktopAdapter(registry)


def test_claude_desktop_maps_current_agent_records(tmp_path) -> None:
    store, _, adapter = _adapter(tmp_path)
    source = tmp_path / "000007.log"
    mcp = "mcp__remote-devices__device_bash"  # 32 chars: V8 length byte is a space
    source.write_bytes(
        _current_use("toolu_01AAAAAAAAAAAAAAAAAAAAAA", mcp)
        + _current_result("toolu_01AAAAAAAAAAAAAAAAAAAAAA", mcp)
        + _current_use("toolu_01BBBBBBBBBBBBBBBBBBBBBB", "Skill", skill="anthropic-skills:docx")
        + _current_result("toolu_01BBBBBBBBBBBBBBBBBBBBBB", "Skill", failed=True)
        + _current_use("toolu_01CCCCCCCCCCCCCCCCCCCCCC", "web_search")
        + _current_result("toolu_01CCCCCCCCCCCCCCCCCCCCCC", "web_search")
    )

    result = adapter.read(source, installation_id="install-claude-desktop-1", session_id="desktop-session-1")

    terminal = {(event.component_kind, event.component_id, event.stage) for event in result.events if event.stage != "started"}
    assert terminal == {
        ("mcp_tool", mcp, "completed"),
        ("skill", "anthropic-skills:docx", "failed"),
        ("tool", "web_search", "completed"),
    }
    skill = next(event for event in result.events if event.component_kind == "skill" and event.stage == "failed")
    assert skill.observation_kind == "skill_call"
    assert skill.evidence_source == "claude_desktop_indexeddb"
    assert skill.occurred_at == datetime(2026, 10, 13, 7, 20, 1, tzinfo=timezone.utc)
    assert result.next_cursor.offset == source.stat().st_size
    store.close()


def test_claude_desktop_skips_builtin_only_records_without_failing(tmp_path) -> None:
    store, _, adapter = _adapter(tmp_path)
    source = tmp_path / "000007.log"
    source.write_bytes(
        _current_use("toolu_01CCCCCCCCCCCCCCCCCCCCCC", "Clock") + _current_result("toolu_01CCCCCCCCCCCCCCCCCCCCCC", "Clock")
    )

    result = adapter.read(source, installation_id="install-claude-desktop-1", session_id="desktop-session-1")

    assert {event.component_kind for event in result.events} == {"tool"}
    store.close()


def test_claude_desktop_counts_tool_records_it_cannot_read_as_unrecognized(tmp_path) -> None:
    # Layer 3: an unreadable call is counted (inferred) instead of stopping the source.
    store, _, adapter = _adapter(tmp_path)
    source = tmp_path / "000007.log"
    source.write_bytes(b"o" + _v8("type") + _v8("tool_use") + _v8("tool_ref") + _v8("x") + b"{\x02")

    result = adapter.read(source, installation_id="install-claude-desktop-1", session_id="desktop-session-1", persist_cursor=False)

    [event] = result.events
    assert (event.component_kind, event.evidence_level, event.component_id) == ("unknown", "inferred", "unrecognized:claude_tool_use")
    assert "unparsed_tool_use_records=1" in result.diagnostics
    again = adapter.read(source, installation_id="install-claude-desktop-1", session_id="desktop-session-1", persist_cursor=False)
    assert again.events[0].event_id == event.event_id  # stable: re-reads do not double count
    store.close()


def _leveldb_log(payloads: list[bytes], *, block: int = 32768) -> bytes:
    """Frame payloads as LevelDB log records (checksums are not verified)."""
    out = bytearray()
    for payload in payloads:
        rest = payload
        first = True
        while True:
            left = block - len(out) % block
            if left < 7:
                out += b"\x00" * left
                left = block
            room = left - 7
            chunk, rest = rest[:room], rest[room:]
            if first and not rest:
                kind = 1
            elif first:
                kind = 2
            elif not rest:
                kind = 4
            else:
                kind = 3
            out += b"\x00\x00\x00\x00" + len(chunk).to_bytes(2, "little") + bytes([kind]) + chunk
            first = False
            if not rest:
                break
    return bytes(out)


def test_claude_desktop_reads_tool_records_split_across_leveldb_blocks(tmp_path) -> None:
    store, registry, adapter = _adapter(tmp_path)
    source = tmp_path / "000007.log"
    mcp = "mcp__observer-workspace__observer_workspace_check"
    filler = b"\x00" * 32700  # pushes the tool record across the first 32 KiB boundary
    record = filler + _current_use("toolu_01DDDDDDDDDDDDDDDDDDDDDD", mcp) + _current_result("toolu_01DDDDDDDDDDDDDDDDDDDDDD", mcp)
    complete = _leveldb_log([b"batch-header", record])
    # A trailing record whose last fragment has not been written yet must not be consumed.
    framed = _leveldb_log([b"batch-header", record, b"x" * 40000])[:-100]
    source.write_bytes(framed)

    result = adapter.read(source, installation_id="install-claude-desktop-1", session_id="desktop-session-1")

    assert {(event.component_id, event.stage) for event in result.events} == {(mcp, "started"), (mcp, "completed")}
    assert "leveldb_log_framing_removed" in result.diagnostics
    assert result.next_cursor.offset == len(complete)

    again = adapter.read(source, installation_id="install-claude-desktop-1", session_id="desktop-session-1", persist_cursor=False)
    assert again.events == ()
    assert again.next_cursor.offset == len(complete)
    store.close()


def test_claude_desktop_reads_skill_name_from_single_input_property(tmp_path) -> None:
    store, _, adapter = _adapter(tmp_path)
    source = tmp_path / "000007.log"
    call = "toolu_01EEEEEEEEEEEEEEEEEEEEEE"
    use = (
        b"o" + _v8("type") + _v8("tool_use") + _v8("id") + _v8(call) + _v8("name") + _v8("Skill")
        + _v8("tool_display_name") + _v8("Skill") + _v8("icon_name") + _v8("skills") + _v8("skill_row") + b"T"
        + _v8("input") + b"o" + _v8("skillid") + _v8("docx") + b"{\x01"
        + _v8("message") + _v8("Loading docx") + b"{\x07"
    )
    source.write_bytes(use + _current_result(call, "Skill"))

    result = adapter.read(source, installation_id="install-claude-desktop-1", session_id="desktop-session-1")

    assert {(event.component_kind, event.component_id, event.stage) for event in result.events} == {
        ("skill", "docx", "started"), ("skill", "docx", "completed"),
    }
    store.close()
