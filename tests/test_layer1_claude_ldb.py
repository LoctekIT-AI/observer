"""Layer 1: Claude records that LevelDB already compacted into .ldb tables are read."""
from __future__ import annotations

from datetime import datetime, timezone

from cowork_contracts import COMPONENT_EVENT_CONTRACT_VERSION, PluginManifestV1
from cowork_runtime.adapters import claude_desktop
from cowork_runtime.adapters.claude_desktop import ClaudeDesktopAdapter
from cowork_runtime.adapters.registry import PluginRegistry
from cowork_runtime.persistence.sqlite_store import SQLiteRuntimeStore

from leveldb_fixtures import current_result, current_use, snappy_compress, table

MCP = "mcp__remote-devices__device_bash"


def _adapter(tmp_path):
    store = SQLiteRuntimeStore(tmp_path / "runtime.sqlite3")
    registry = PluginRegistry(store)
    registry.register(PluginManifestV1(
        plugin_id="claude-desktop-adapter", runtime="claude", contract_version=COMPONENT_EVENT_CONTRACT_VERSION,
        installation_id="install-claude-desktop-1", enabled=True, capabilities=["tool_calls", "desktop", "indexeddb"],
    ))
    return ClaudeDesktopAdapter(registry)


def test_snappy_round_trip_with_overlapping_copies():
    data = b"abcabcabcabcabcabc" * 50 + bytes(range(256)) + b"x" * 300
    assert claude_desktop._snappy_decompress(snappy_compress(data)) == data


def test_compressed_table_records_are_read(tmp_path):
    source = tmp_path / "000012.ldb"
    source.write_bytes(table([
        current_use("toolu_01AAAAAAAAAAAAAAAAAAAAAA", MCP),
        current_result("toolu_01AAAAAAAAAAAAAAAAAAAAAA", MCP),
        current_use("toolu_01BBBBBBBBBBBBBBBBBBBBBB", "Skill", skill="anthropic-skills:docx"),
        current_result("toolu_01BBBBBBBBBBBBBBBBBBBBBB", "Skill", failed=True),
    ]))
    raw = source.read_bytes()
    # Snappy replaces repeated byte runs with back-references, so scanning the
    # raw file (the previous behaviour) misses records the decoded table has.
    raw_records, _ = claude_desktop._parse_records(raw, datetime(2026, 10, 14, tzinfo=timezone.utc))
    decoded_records, _ = claude_desktop._parse_records(
        claude_desktop._leveldb_table_stream(raw).data, datetime(2026, 10, 14, tzinfo=timezone.utc))
    assert len(decoded_records) == 4 and len(raw_records) < 4
    result = _adapter(tmp_path).read(
        source, installation_id="install-claude-desktop-1", session_id="s", now=datetime(2026, 10, 14, tzinfo=timezone.utc),
    )
    kinds = {(event.component_kind, event.component_id, event.stage) for event in result.events}
    assert ("mcp_tool", MCP, "completed") in kinds
    assert ("skill", "anthropic-skills:docx", "failed") in kinds
    assert result.next_cursor.offset == len(raw)


def test_uncompressed_table_and_not_a_table(tmp_path):
    source = tmp_path / "000013.ldb"
    source.write_bytes(table([current_use("toolu_01CCCCCCCCCCCCCCCCCCCCCC", MCP), current_result("toolu_01CCCCCCCCCCCCCCCCCCCCCC", MCP)], compress=False))
    result = _adapter(tmp_path).read(source, installation_id="install-claude-desktop-1", session_id="s")
    assert any(event.stage == "completed" for event in result.events)
    assert claude_desktop._leveldb_table_stream(b"not a table at all" * 10) is None


def test_worker_reads_compacted_table_once_and_skips_pre_install_history(tmp_path):
    from cowork_runtime.observer.claude_worker import ClaudeDesktopObserverWorker

    source = tmp_path / "leveldb"
    source.mkdir()
    log = source / "000003.log"
    log.write_bytes(b"")
    worker = ClaudeDesktopObserverWorker(source_root=source, state_dir=tmp_path / "state", flush=False)
    worker.run_once()  # installation high-water

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    a, b, c = ("toolu_01" + letter * 22 for letter in "ABC")
    log.write_bytes(current_use(a, MCP, when=now) + current_result(a, MCP))
    worker.run_once()
    assert worker.outbox.count_by_status() == {"pending": 1}

    # LevelDB compacts the log into a table: A again, B new, C from before the install.
    (source / "000005.ldb").write_bytes(table([
        current_use(a, MCP, when=now), current_result(a, MCP),
        current_use(b, MCP, when=now), current_result(b, MCP),
        current_use(c, MCP, when="2026-01-01T00:00:00.000Z"), current_result(c, MCP),
    ]))
    report = worker.run_once()
    assert report.errors == ()
    assert worker.outbox.count_by_status() == {"pending": 2}  # A once, B once, C never
    worker.close()
