"""Durable, metadata-only Claude Desktop observer worker."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from .locking import single_instance
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator
from uuid import uuid4

from cowork_contracts import COMPONENT_EVENT_CONTRACT_VERSION, PluginManifestV1

from ..adapters.claude_desktop import ClaudeDesktopAdapter
from ..adapters.degraded import is_degraded
from .replay import ReplayLedger, apply_due
from ..adapters.delivery import ComponentEventDelivery
from ..adapters.registry import CodexCursor, PluginRegistry
from ..persistence.sqlite_store import SQLiteRuntimeStore
from ..telemetry.emitter import EventStoreTraceEmitter
from ..telemetry.langfuse import LangfuseConfig, LangfuseExporter
from ..telemetry.health import HealthReporter
from ..telemetry.langfuse_outbox import LangfuseContentOutbox
from ..telemetry.identity import ObserverIdentity

INSTALLATION_ID = "install-claude-desktop-1"
PLUGIN_ID = "claude-desktop-adapter"


@dataclass(frozen=True)
class ClaudeObserverRun:
    files: int
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


def _complete_offset(path: Path) -> int:
    with path.open("rb") as handle:
        position = handle.seek(0, os.SEEK_END)
        while position:
            start = max(0, position - 65536)
            handle.seek(start)
            chunk = handle.read(position - start)
            newline = chunk.rfind(b"\n")
            if newline >= 0:
                return start + newline + 1
            position = start
    return 0


def _skill_ids_from_manifests(root: Path) -> tuple[str, ...]:
    if not root.exists():
        return ()
    values: set[str] = set()
    for path in root.rglob("manifest.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        skills = payload.get("skills") if isinstance(payload, dict) else None
        if not isinstance(skills, list):
            continue
        for item in skills:
            if isinstance(item, dict) and isinstance(item.get("skillId"), str) and item["skillId"].strip():
                values.add(item["skillId"].strip())
    return tuple(sorted(values))


class ClaudeDesktopObserverWorker:
    """Read new Claude Desktop IndexedDB records and persist safe projections."""

    def __init__(
        self,
        *,
        source_root: str | Path,
        state_dir: str | Path,
        skill_ids: Iterable[str] = (),
        skills_root: str | Path | None = None,
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
        self.health = HealthReporter(self.state_dir, "claude", enabled=flush)
        self._unrecognized = 0
        self.replay = ReplayLedger(self.state_dir)
        self._replay_checked = False
        self._replayed = 0
        self._guarded: set[str] = set()
        configured_skills = {value.strip() for value in skill_ids if isinstance(value, str) and value.strip()}
        if skills_root is not None:
            configured_skills.update(_skill_ids_from_manifests(Path(skills_root).expanduser().resolve()))
        self.skill_ids = tuple(sorted(configured_skills))
        self.store = SQLiteRuntimeStore(self.state_dir / "observer.sqlite3")
        self.registry = PluginRegistry(self.store)
        if self.store.get_adapter_plugin(PLUGIN_ID, INSTALLATION_ID) is None:
            self.registry.register(PluginManifestV1(
                plugin_id=PLUGIN_ID,
                runtime="claude",
                contract_version=COMPONENT_EVENT_CONTRACT_VERSION,
                installation_id=INSTALLATION_ID,
                enabled=True,
                capabilities=["tool_calls", "desktop", "indexeddb", "deferred_skill_search"],
            ))
        identity = ObserverIdentity.from_env()
        if identity is not None:
            self.registry.bind_pseudonyms(
                INSTALLATION_ID,
                tenant_pseudonym=identity.tenant_pseudonym,
                user_pseudonym=identity.user_pseudonym,
            )
        self.adapter = ClaudeDesktopAdapter(self.registry)
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
        with single_instance(self.state_dir / "claude-observer.lock", "Claude Desktop"):
            yield

    def _files(self) -> list[Path]:
        self.source_root.stat()
        if self.source_root.is_file():
            return [self.source_root]
        if not self.source_root.is_dir():
            raise ValueError("Claude Desktop source is not a file or directory")
        files: list[Path] = []
        def failed(error: OSError) -> None:
            raise error
        for directory, _, names in os.walk(self.source_root, onerror=failed):
            files.extend(
                Path(directory) / name
                for name in names
                if name.endswith((".ldb", ".log"))
            )
        return sorted(files)

    def _initialize_high_water(self, files: list[Path]) -> int:
        marker = self.state_dir / "installation.json"
        if marker.exists():
            installation = json.loads(marker.read_text(encoding="utf-8"))
            if installation["source_root"] != str(self.source_root):
                raise ValueError("state directory belongs to a different Claude source root")
            self._installed_at = datetime.fromisoformat(installation["created_at"])
            if installation["complete"]:
                return 0
        else:
            installation = {
                "source_root": str(self.source_root),
                "complete": False,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "sources": [
                    # LevelDB log/ldb files are binary records and do not use
                    # newline-delimited framing; the adapter can resume at a
                    # byte boundary, so installation high-water is the file
                    # size rather than the last newline.
                    {"path": str(path), "identity": _file_identity(path), "offset": path.stat().st_size}
                    for path in files
                ],
            }
            _atomic_json(marker, installation)
        initialized = 0
        for source in installation["sources"]:
            if self.registry.cursor(INSTALLATION_ID, source["path"]) is None:
                self.registry.save_cursor(CodexCursor(
                    installation_id=INSTALLATION_ID,
                    source_path=source["path"],
                    file_identity=source["identity"],
                    offset=source["offset"],
                    updated_at=datetime.fromisoformat(installation["created_at"]),
                ))
                initialized += 1
        installation["complete"] = True
        _atomic_json(marker, installation)
        self._installed_at = datetime.fromisoformat(installation["created_at"])
        return initialized

    @staticmethod
    def _session_id(source: Path) -> str:
        return f"claude-desktop:{source.name}"

    def _apply_replay(self) -> None:
        # Once per process: a newer version re-reads where an older one could not.
        if self._replay_checked:
            return
        self._replay_checked = True
        rewound, guarded = apply_due(self.replay, self.registry, _file_identity)
        self._replayed += rewound
        self._guarded |= guarded

    def _process(self, source: Path) -> tuple[int, int]:
        cursor = self.registry.cursor(INSTALLATION_ID, str(source))
        identity = _file_identity(source)
        if cursor and cursor.file_identity == identity and cursor.offset == source.stat().st_size:
            return 0, 0
        start = cursor.offset if cursor and cursor.file_identity == identity else 0
        result = self.adapter.read(
            source,
            installation_id=INSTALLATION_ID,
            session_id=self._session_id(source),
            skill_ids=self.skill_ids,
            persist_cursor=False,
        )
        table = source.suffix in (".ldb", ".sst")
        guarded = (cursor is None and table) or str(source) in self._guarded
        installed_at = getattr(self, "_installed_at", None) if guarded else None
        events = []
        for event in result.events:
            if event.component_kind not in {"skill", "mcp_tool"} and not is_degraded(event):
                continue
            # LevelDB compaction rewrites records from the log into a new table
            # file. A new file is read from its start, so drop calls from before
            # the installation and calls already reported from another file.
            if installed_at is not None and event.occurred_at < installed_at:
                continue
            if event.stage != "started" and self.outbox.has_invocation(event.invocation_id, event.stage):
                continue
            events.append(event)
        degraded = sum(1 for event in events if is_degraded(event) and event.stage != "started")
        self._unrecognized += degraded
        if degraded:
            self.replay.note(INSTALLATION_ID, str(source), identity, start, guarded=installed_at is not None)
        terminal = 0
        for event in events:
            safe = event.model_copy(update={"user_input": None, "model_output": None, "tool_result": None})
            self.delivery.deliver(safe)
            if event.stage != "started":
                self.outbox.enqueue(safe)
                terminal += 1
        self.registry.save_cursor(result.next_cursor)
        self._guarded.discard(str(source))
        return len(events), terminal

    def _flush(self) -> None:
        if self.flush_enabled:
            config = LangfuseConfig.from_env()
            report = self.outbox.flush(
                LangfuseExporter(config).export,
                owner_id=f"claude-observer-{os.getpid()}-{uuid4().hex}",
            )
            if report.failed:
                raise RuntimeError("Observer delivery failed; pending events retained")

    @staticmethod
    def _error(code: str, path: Path, exc: Exception) -> dict[str, str]:
        result = {"code": code, "path": str(path), "error_type": type(exc).__name__}
        if hasattr(exc, "offset"):
            result["offset"] = str(exc.offset)
        return result

    def _poll(self) -> ClaudeObserverRun:
        errors: list[dict[str, str]] = []
        files: list[Path] = []
        events = terminal = initialized = 0
        try:
            files = self._files()
            initialized = self._initialize_high_water(files)
            self._apply_replay()
        except (OSError, ValueError, RuntimeError) as exc:
            errors.append(self._error("source_unavailable", self.source_root, exc))
        if not errors:
            for source in files:
                try:
                    found, queued = self._process(source)
                    events += found
                    terminal += queued
                except (OSError, RuntimeError, ValueError) as exc:
                    errors.append(self._error("source_invalid", source, exc))
        try:
            self._flush()
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(self._error("delivery_failed", self.state_dir / "langfuse-content-outbox.sqlite3", exc))
        report = ClaudeObserverRun(len(files), events, terminal, initialized, tuple(errors))
        status = {
            "status": "failed" if errors else "ok",
            "pid": os.getpid(),
            "launch_pid": os.getpid(),
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "runtime": "claude",
            "files": report.files,
            "events": report.events,
            "terminal_enqueued": report.terminal_enqueued,
            "errors": list(report.errors),
            "outbox": self.outbox.count_by_status(),
        }
        _atomic_json(self.state_dir / "status.json", status)
        self.health.observe(status, unrecognized_calls=self._unrecognized, replayed_sources=self._replayed)
        self._unrecognized = 0
        self._replayed = 0
        return report

    def run_once(self) -> ClaudeObserverRun:
        with self._single_instance():
            return self._poll()

    def run_forever(self, on_report: Callable[[ClaudeObserverRun], None] | None = None) -> None:
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
        self._thread = threading.Thread(target=self.run_forever, name="claude-observer", daemon=True)
        self._thread.start()


__all__ = ["ClaudeDesktopObserverWorker", "ClaudeObserverRun"]
