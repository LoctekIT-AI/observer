"""Durable metadata-only DSH worker using the existing ledger and outbox."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from .locking import single_instance
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator
from uuid import uuid4

from cowork_contracts import COMPONENT_EVENT_CONTRACT_VERSION, PluginManifestV1
from ..adapters.dsh import DshAdapter, PLUGIN_ID
from ..adapters.delivery import ComponentEventDelivery
from ..adapters.registry import CodexCursor, PluginRegistry
from ..persistence.sqlite_store import SQLiteRuntimeStore
from ..telemetry.emitter import EventStoreTraceEmitter
from ..telemetry.identity import ObserverIdentity
from ..telemetry.langfuse import LangfuseConfig, LangfuseExporter
from ..telemetry.health import HealthReporter
from .replay import ReplayLedger, apply_due
from ..telemetry.langfuse_outbox import LangfuseContentOutbox
from ..telemetry.turns import Turn, TurnSpool, capture_enabled, default_send

TURN_FILE = "turns.ndjson"  # written by the DSH plugin next to its event file


@dataclass(frozen=True)
class DshObserverRun:
    files: int
    events: int
    terminal_enqueued: int


def _identity(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_dev}:{stat.st_ino}"


def _atomic_status(path: Path, payload: dict[str, object]) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=".status-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class DshObserverWorker:
    def __init__(self, *, source_root: str | Path, state_dir: str | Path, flush: bool = True, interval_seconds: float = 5.0) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.source_root = Path(source_root).expanduser().resolve()
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.interval_seconds = interval_seconds
        self.flush_enabled = flush
        self.health = HealthReporter(self.state_dir, "dsh", enabled=flush)
        self._unrecognized = 0
        self.replay = ReplayLedger(self.state_dir)
        self._replay_checked = False
        self._replayed = 0
        self.store = SQLiteRuntimeStore(self.state_dir / "observer.sqlite3")
        self.registry = PluginRegistry(self.store)
        self.outbox = LangfuseContentOutbox(self.state_dir / "langfuse-content-outbox.sqlite3")
        self.adapter = DshAdapter()
        self.delivery = ComponentEventDelivery(EventStoreTraceEmitter(self.store), self.registry)
        self.reader_id = "install-dsh-reader-" + hashlib.sha256(str(self.source_root).encode()).hexdigest()[:16]
        self._register(self.reader_id)
        self._stop = threading.Event()
        # Turns that used a Skill / MCP: user input + final reply (telemetry.turns).
        self.turn_spool = TurnSpool(self.state_dir)
        self._turns_cursor = self.state_dir / "turns-cursor.json"
        self._turns_reported = 0

    def _read_turns(self) -> None:
        """Queue the plugin's finished turns (one JSON line each) that were not read yet."""
        if not capture_enabled():
            return
        root = self.source_root if self.source_root.is_dir() else self.source_root.parent
        try:
            cursors = json.loads(self._turns_cursor.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cursors = {}
        for path in sorted(root.rglob(TURN_FILE)):
            identity = _identity(path)
            known = cursors.get(str(path)) or {}
            offset = int(known.get("offset", 0)) if known.get("identity") == identity else 0
            if offset > path.stat().st_size:
                offset = 0
            with path.open("rb") as handle:
                handle.seek(offset)
                for raw in handle:
                    if not raw.endswith(b"\n"):
                        break  # being written: read it next time
                    offset += len(raw)
                    try:
                        data = json.loads(raw)
                        if data.get("schema_version") != "dsh_turn.v1":
                            continue
                        self.turn_spool.add(Turn(
                            runtime="dsh", session_id=str(data["session_id"]), turn_id=str(data["turn_id"]),
                            started_at=datetime.fromisoformat(str(data["started_at"]).replace("Z", "+00:00")),
                            ended_at=datetime.fromisoformat(str(data["ended_at"]).replace("Z", "+00:00")),
                            user_input=data.get("user_input"), final_output=data.get("final_output"),
                            skills=[str(item) for item in data.get("skills") or []],
                            mcp_tools=[str(item) for item in data.get("mcp_tools") or []],
                            status=str(data.get("status") or "completed")))
                    except (ValueError, KeyError, TypeError, AttributeError):
                        continue  # a line this version cannot read is skipped
            cursors[str(path)] = {"identity": identity, "offset": offset}
        temporary = self._turns_cursor.with_suffix(".tmp")
        temporary.write_text(json.dumps(cursors), encoding="utf-8")
        os.replace(temporary, self._turns_cursor)

    def _register(self, installation_id: str) -> None:
        if self.store.get_adapter_plugin(PLUGIN_ID, installation_id) is None:
            self.registry.register(PluginManifestV1(
                plugin_id=PLUGIN_ID, runtime="dsh", contract_version=COMPONENT_EVENT_CONTRACT_VERSION,
                installation_id=installation_id, enabled=True,
                capabilities=["tool_calls", "skill_invocations", "source:dsh-native-plugin"],
            ))
        identity = ObserverIdentity.from_env()
        if identity is not None:
            self.registry.bind_pseudonyms(installation_id, tenant_pseudonym=identity.tenant_pseudonym, user_pseudonym=identity.user_pseudonym)

    @contextmanager
    def _single_instance(self) -> Iterator[None]:
        with single_instance(self.state_dir / "dsh-observer.lock", "DSH"):
            yield

    def _files(self) -> list[Path]:
        self.source_root.stat()
        if self.source_root.is_file():
            return [self.source_root]
        if not self.source_root.is_dir():
            raise ValueError("DSH source must be a file or directory")
        files: list[Path] = []
        def failed(error: OSError) -> None:
            raise error
        for directory, _, names in os.walk(self.source_root, onerror=failed):
            files.extend(Path(directory) / name for name in names if name.endswith(".jsonl"))
        return sorted(files)

    def _poll(self) -> DshObserverRun:
        files = self._files()
        if not self._replay_checked:
            # Once per process: a newer version re-reads lines an older one skipped.
            self._replay_checked = True
            self._replayed += apply_due(self.replay, self.registry, _identity)[0]
        events = 0
        queued_before = sum(self.outbox.count_by_status().values())
        for path in files:
            stat = path.stat()
            identity = _identity(path)
            cursor = self.registry.cursor(self.reader_id, str(path))
            offset = cursor.offset if cursor and cursor.file_identity == identity and cursor.offset <= stat.st_size else 0
            line_start = offset
            for event, next_offset in self.adapter.read(path, offset=offset):
                if event is None:  # skipped line: remember it for the next version, move past it
                    self.replay.note(self.reader_id, str(path), identity, line_start)
                    line_start = next_offset
                    self.registry.save_cursor(CodexCursor(self.reader_id, str(path), identity, next_offset, datetime.now(timezone.utc)))
                    continue
                if event.component_kind == "tool":
                    # DSH built-in tools (bash, write, read_image, ...) are not reported: only Skill
                    # and MCP calls (and unrecognized calls) are, as for the other clients.
                    line_start = next_offset
                    self.registry.save_cursor(CodexCursor(self.reader_id, str(path), identity, next_offset, datetime.now(timezone.utc)))
                    continue
                self._unrecognized += int(event.component_kind == "unknown")
                self._register(event.installation_id)
                binding = self.registry.binding(event.installation_id)
                if binding is not None:
                    event = event.model_copy(update={"tenant_id": binding.tenant_id, "user_id": binding.user_id})
                before = self.store.latest_seq(event.run_id)
                self.delivery.deliver(event)
                # 先持久化账本/队列，再推进游标。中断后重读依靠 event_id 幂等恢复。
                self.outbox.enqueue(event)
                inserted = self.store.latest_seq(event.run_id) > before
                events += int(inserted)
                self.registry.save_cursor(CodexCursor(self.reader_id, str(path), identity, next_offset, datetime.now(timezone.utc)))
                line_start = next_offset
        terminals = sum(self.outbox.count_by_status().values()) - queued_before
        self._read_turns()
        if self.flush_enabled:
            result = self.outbox.flush(LangfuseExporter(LangfuseConfig.from_env()).export, owner_id=f"dsh-observer-{os.getpid()}-{uuid4().hex}")
            if result.failed:
                raise RuntimeError("Observer delivery failed; pending events retained")
            sent, failed = self.turn_spool.flush(default_send)
            self._turns_reported += sent
            if failed:
                raise RuntimeError("Observer turn delivery failed; pending turns retained")
        return DshObserverRun(len(files), events, terminals)

    def _run_checked(self) -> DshObserverRun:
        try:
            result = self._poll()
        except (OSError, ValueError, RuntimeError) as exc:
            self._status("failed", error_type=type(exc).__name__)
            raise
        self._status("ok", files=result.files, events=result.events, terminal_enqueued=result.terminal_enqueued,
                     skipped_records=self.adapter.skipped)
        return result

    def _status(self, status: str, **fields: object) -> None:
        payload = {
            "status": status, "runtime": "dsh", "pid": os.getpid(), "launch_pid": os.getpid(),
            "checked_at": datetime.now(timezone.utc).isoformat(), "outbox": self.outbox.count_by_status(),
            "turns_pending": self.turn_spool.pending(), **fields,
        }
        _atomic_status(self.state_dir / "status.json", payload)
        self.health.observe(payload, unrecognized_calls=self._unrecognized, replayed_sources=self._replayed,
                            turns_reported=self._turns_reported)
        self._turns_reported = 0
        self._unrecognized = 0
        self._replayed = 0

    def run_once(self) -> DshObserverRun:
        with self._single_instance():
            return self._run_checked()

    def run_forever(self, on_report: Callable[[DshObserverRun], None] | None = None) -> None:
        with self._single_instance():
            while not self._stop.is_set():
                result = self._run_checked()
                if on_report is not None:
                    on_report(result)
                self._stop.wait(self.interval_seconds)

    def request_stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        self.request_stop()
        self.outbox.close()
        self.store.close()
