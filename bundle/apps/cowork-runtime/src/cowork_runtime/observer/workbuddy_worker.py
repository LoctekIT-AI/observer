"""Durable, metadata-only WorkBuddy Desktop command-hook observer worker."""

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

from ..adapters.degraded import is_degraded
from ..adapters.delivery import ComponentEventDelivery
from ..adapters.registry import CodexCursor, PluginRegistry
from ..adapters.workbuddy_desktop import WorkBuddyDesktopAdapter
from ..persistence.sqlite_store import SQLiteRuntimeStore
from ..telemetry.emitter import EventStoreTraceEmitter
from ..telemetry.langfuse import LangfuseConfig, LangfuseExporter
from ..telemetry.health import HealthReporter
from ..telemetry.langfuse_outbox import LangfuseContentOutbox
from ..telemetry.identity import ObserverIdentity


PLUGIN_ID = "workbuddy-desktop-adapter"
SOURCE_CAPABILITY = "source:official-workbuddy-command-hook"


@dataclass(frozen=True)
class WorkBuddyObserverRun:
    files: int
    sessions: int
    events: int
    terminal_enqueued: int
    initialized: int
    errors: tuple[dict[str, str], ...]


def _file_identity(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_dev}:{stat.st_ino}"


def _atomic_json(path: Path, payload: dict) -> None:
    descriptor, raw_path = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(raw_path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _installation_id(session_id: str) -> str:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:20]
    return f"install-workbuddy-desktop-{digest}"


class WorkBuddyDesktopObserverWorker:
    """Read the official WorkBuddy hook ledger and persist safe projections."""

    def __init__(
        self,
        *,
        source_root: str | Path,
        state_dir: str | Path,
        interval_seconds: float = 5.0,
        flush: bool = True,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("observer interval must be positive")
        self.source_root = Path(source_root).expanduser().resolve()
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.interval_seconds = interval_seconds
        self.flush_enabled = flush
        self.health = HealthReporter(self.state_dir, "workbuddy", enabled=flush)
        self._unrecognized = 0
        self.store = SQLiteRuntimeStore(self.state_dir / "observer.sqlite3")
        self.registry = PluginRegistry(self.store)
        self.adapter = WorkBuddyDesktopAdapter(self.registry)
        self.outbox = LangfuseContentOutbox(self.state_dir / "langfuse-content-outbox.sqlite3")
        self.delivery = ComponentEventDelivery(EventStoreTraceEmitter(self.store), self.registry)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def close(self) -> None:
        self.stop()
        self.outbox.close()
        self.store.close()

    @contextmanager
    def _single_instance(self) -> Iterator[None]:
        with single_instance(self.state_dir / "workbuddy-observer.lock", "WorkBuddy Desktop"):
            yield

    def _source(self) -> Path:
        self.source_root.stat()
        if not self.source_root.is_file():
            raise ValueError("WorkBuddy Desktop source is not a regular file")
        return self.source_root

    @staticmethod
    def _session_ids(source: Path) -> tuple[str, ...]:
        sessions: set[str] = set()
        with source.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a broken line is skipped (and counted) by the adapter
                if not isinstance(record, dict):
                    continue
                if record.get("schema_version") not in {"workbuddy_hook_event.v1", "workbuddy_interaction_event.v1", "workbuddy_unrecognized_event.v1"}:
                    continue
                session_id = record.get("session_id")
                if isinstance(session_id, str) and session_id.strip():
                    sessions.add(session_id.strip())
        return tuple(sorted(sessions))

    def _marker(self, source: Path) -> tuple[dict, int]:
        path = self.state_dir / "installation.json"
        identity = _file_identity(source)
        size = source.stat().st_size
        if not path.exists():
            return {"source_path": str(source), "file_identity": identity, "high_water": size, "complete": False}, size
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("source_path") != str(source):
            raise ValueError("state directory belongs to a different WorkBuddy source")
        if payload.get("file_identity") != identity or size < int(payload.get("high_water", 0)):
            return {"source_path": str(source), "file_identity": identity, "high_water": 0, "complete": False}, 0
        return payload, int(payload.get("high_water", 0))

    def _register(self, session_id: str) -> str:
        installation_id = _installation_id(session_id)
        if self.store.get_adapter_plugin(PLUGIN_ID, installation_id) is None:
            self.registry.register(PluginManifestV1(
                plugin_id=PLUGIN_ID,
                runtime="workbuddy",
                contract_version=COMPONENT_EVENT_CONTRACT_VERSION,
                installation_id=installation_id,
                enabled=True,
                capabilities=["tool_calls", "hooks", "desktop", SOURCE_CAPABILITY],
            ))
        identity = ObserverIdentity.from_env()
        if identity is not None:
            self.registry.bind_pseudonyms(
                installation_id,
                tenant_pseudonym=identity.tenant_pseudonym,
                user_pseudonym=identity.user_pseudonym,
            )
        return installation_id

    def _initialize(self, source: Path, sessions: tuple[str, ...], marker: dict, previous_high_water: int) -> int:
        initialized = 0
        for session_id in sessions:
            installation_id = self._register(session_id)
            if self.registry.cursor(installation_id, str(source)) is None:
                offset = source.stat().st_size if not marker.get("complete") else previous_high_water
                self.registry.save_cursor(CodexCursor(
                    installation_id=installation_id,
                    source_path=str(source),
                    file_identity=_file_identity(source),
                    offset=offset,
                    updated_at=datetime.now(timezone.utc),
                ))
                initialized += 1
        return initialized

    def _process(self, source: Path, session_id: str) -> tuple[int, int]:
        installation_id = self._register(session_id)
        result = self.adapter.read(
            source,
            installation_id=installation_id,
            session_id=session_id,
            run_id=session_id,
            persist_cursor=False,
        )
        # Malformed source lines the adapter had to skip stay visible in status.json.
        self._skipped_records = getattr(self, "_skipped_records", 0) + len(result.diagnostics)
        events = [event for event in result.events if event.component_kind in {"skill", "mcp_tool"} or is_degraded(event)]
        self._unrecognized += sum(1 for event in events if is_degraded(event) and event.stage != "started")
        terminal = 0
        for event in events:
            safe = event.model_copy(update={"user_input": None, "model_output": None, "tool_result": None})
            self.delivery.deliver(safe)
            if safe.stage != "started":
                self.outbox.enqueue(safe)
                terminal += 1
        self.registry.save_cursor(result.next_cursor)
        return len(events), terminal

    def _flush(self) -> None:
        if not self.flush_enabled:
            return
        config = LangfuseConfig.from_env()
        report = self.outbox.flush(
            LangfuseExporter(config).export,
            owner_id=f"workbuddy-observer-{os.getpid()}-{uuid4().hex}",
        )
        if report.failed:
            raise RuntimeError("Observer delivery failed; pending events retained")

    @staticmethod
    def _error(code: str, path: Path, exc: Exception) -> dict[str, str]:
        result = {"code": code, "path": str(path), "error_type": type(exc).__name__}
        if hasattr(exc, "offset"):
            result["offset"] = str(exc.offset)
        return result

    def _poll(self) -> WorkBuddyObserverRun:
        errors: list[dict[str, str]] = []
        source: Path | None = None
        sessions: tuple[str, ...] = ()
        events = terminal = initialized = 0
        try:
            source = self._source()
            marker, previous_high_water = self._marker(source)
            sessions = self._session_ids(source)
            initialized = self._initialize(source, sessions, marker, previous_high_water)
            if marker.get("complete"):
                for session_id in sessions:
                    found, queued = self._process(source, session_id)
                    events += found
                    terminal += queued
            marker.update({"source_path": str(source), "file_identity": _file_identity(source), "high_water": source.stat().st_size, "complete": True, "updated_at": datetime.now(timezone.utc).isoformat()})
            _atomic_json(self.state_dir / "installation.json", marker)
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            errors.append(self._error("source_invalid", source or self.source_root, exc))
        try:
            self._flush()
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(self._error("delivery_failed", self.state_dir / "langfuse-content-outbox.sqlite3", exc))
        report = WorkBuddyObserverRun(1 if source is not None else 0, len(sessions), events, terminal, initialized, tuple(errors))
        status = {
            "status": "failed" if errors else "ok",
            "pid": os.getpid(),
            "launch_pid": os.getpid(),
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "runtime": "workbuddy",
            "source": str(self.source_root),
            "files": report.files,
            "sessions": report.sessions,
            "events": report.events,
            "terminal_enqueued": report.terminal_enqueued,
            "initialized": report.initialized,
            "errors": list(report.errors),
            "skipped_records": getattr(self, "_skipped_records", 0),
            "outbox": self.outbox.count_by_status(),
        }
        _atomic_json(self.state_dir / "status.json", status)
        self.health.observe(status, unrecognized_calls=self._unrecognized)
        self._unrecognized = 0
        return report

    def run_once(self) -> WorkBuddyObserverRun:
        with self._single_instance():
            return self._poll()

    def run_forever(self, on_report: Callable[[WorkBuddyObserverRun], None] | None = None) -> None:
        with self._single_instance():
            while not self._stop.is_set():
                report = self._poll()
                if on_report is not None:
                    on_report(report)
                self._stop.wait(self.interval_seconds)

    def request_stop(self) -> None:
        self._stop.set()

    def stop(self) -> None:
        self.request_stop()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def start(self) -> None:
        if self.is_running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run_forever, name="workbuddy-observer", daemon=True)
        self._thread.start()


__all__ = ["WorkBuddyDesktopObserverWorker", "WorkBuddyObserverRun"]
