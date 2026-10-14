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
from ..telemetry.langfuse_outbox import LangfuseContentOutbox


@dataclass(frozen=True)
class DshObserverRun:
    files: int
    events: int
    terminal_enqueued: int


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
        self.store = SQLiteRuntimeStore(self.state_dir / "observer.sqlite3")
        self.registry = PluginRegistry(self.store)
        self.outbox = LangfuseContentOutbox(self.state_dir / "langfuse-content-outbox.sqlite3")
        self.adapter = DshAdapter()
        self.delivery = ComponentEventDelivery(EventStoreTraceEmitter(self.store), self.registry)
        self.reader_id = "install-dsh-reader-" + hashlib.sha256(str(self.source_root).encode()).hexdigest()[:16]
        self._register(self.reader_id)
        self._stop = threading.Event()

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
        events = 0
        queued_before = sum(self.outbox.count_by_status().values())
        for path in files:
            stat = path.stat()
            identity = f"{stat.st_dev}:{stat.st_ino}"
            cursor = self.registry.cursor(self.reader_id, str(path))
            offset = cursor.offset if cursor and cursor.file_identity == identity and cursor.offset <= stat.st_size else 0
            for event, next_offset in self.adapter.read(path, offset=offset):
                if event is None:  # skipped line: just move past it
                    self.registry.save_cursor(CodexCursor(self.reader_id, str(path), identity, next_offset, datetime.now(timezone.utc)))
                    continue
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
        terminals = sum(self.outbox.count_by_status().values()) - queued_before
        if self.flush_enabled:
            result = self.outbox.flush(LangfuseExporter(LangfuseConfig.from_env()).export, owner_id=f"dsh-observer-{os.getpid()}-{uuid4().hex}")
            if result.failed:
                raise RuntimeError("Observer delivery failed; pending events retained")
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
        _atomic_status(self.state_dir / "status.json", {
            "status": status, "runtime": "dsh", "pid": os.getpid(), "launch_pid": os.getpid(),
            "checked_at": datetime.now(timezone.utc).isoformat(), "outbox": self.outbox.count_by_status(), **fields,
        })

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
