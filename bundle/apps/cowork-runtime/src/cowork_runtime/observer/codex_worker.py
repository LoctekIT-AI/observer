"""Durable, metadata-only Codex Desktop observer worker."""

from __future__ import annotations

import json
import os
import sys
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

from ..adapters.codex import CodexIncrementalAdapter
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

INSTALLATION_ID = "install-codex-1"
PLUGIN_ID = "codex-adapter"


@dataclass(frozen=True)
class CodexObserverRun:
    files: int
    events: int
    terminal_enqueued: int
    initialized: int
    errors: tuple[dict[str, str], ...]


def _file_identity(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_dev}:{stat.st_ino}"


def _session_id(path: Path) -> str:
    with path.open("rb") as handle:
        for raw_line in handle:
            if not raw_line.endswith(b"\n"):
                raise ValueError("rollout contains a truncated final line")
            record = json.loads(raw_line)
            if not isinstance(record, dict):
                continue
            payload = record.get("payload")
            if record.get("type") == "session_meta" and isinstance(payload, dict):
                value = payload.get("id") or payload.get("session_id")
                if isinstance(value, str) and value.strip():
                    return value.strip()
            value = record.get("thread_id")
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(payload, dict):
                value = payload.get("session_id")
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return path.stem


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
    # 安装时只跳过完整记录，避免把半行 cursor 写进 SQLite 后永久卡住。
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


class CodexObserverWorker:
    """Read new rollout facts, persist metadata, then advance durable cursors."""

    def __init__(self, *, source_root: str | Path, state_dir: str | Path,
                 interval_seconds: float = 5.0, flush: bool = True) -> None:
        if interval_seconds <= 0:
            raise ValueError("observer interval must be positive")
        self.source_root = Path(source_root).expanduser().resolve()
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.interval_seconds = interval_seconds
        self.flush_enabled = flush
        self.health = HealthReporter(self.state_dir, "codex", enabled=flush)
        self._unrecognized = 0
        self.replay = ReplayLedger(self.state_dir)
        self._replay_checked = False
        self._replayed = 0
        self._guarded: set[str] = set()
        self.store = SQLiteRuntimeStore(self.state_dir / "observer.sqlite3")
        self.registry = PluginRegistry(self.store)
        if self.store.get_adapter_plugin(PLUGIN_ID, INSTALLATION_ID) is None:
            self.registry.register(PluginManifestV1(
                plugin_id=PLUGIN_ID, runtime="codex", contract_version=COMPONENT_EVENT_CONTRACT_VERSION,
                installation_id=INSTALLATION_ID, enabled=True,
                capabilities=["tool_calls", "usage", "rollout_jsonl"],
            ))
        identity = ObserverIdentity.from_env()
        if identity is not None:
            self.registry.bind_pseudonyms(
                INSTALLATION_ID,
                tenant_pseudonym=identity.tenant_pseudonym,
                user_pseudonym=identity.user_pseudonym,
            )
        self.adapter = CodexIncrementalAdapter(self.registry)
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
        with single_instance(self.state_dir / "codex-observer.lock", "Codex"):
            yield

    def _files(self) -> list[Path]:
        self.source_root.stat()  # 原生不可读/不存在错误必须可见。
        if self.source_root.is_file():
            return [self.source_root]
        if not self.source_root.is_dir():
            raise ValueError("Codex source is not a file or directory")
        files: list[Path] = []
        def failed(error: OSError) -> None:
            raise error
        for directory, _, names in os.walk(self.source_root, onerror=failed):
            files.extend(Path(directory) / name for name in names if name.endswith(".jsonl"))
        # Codex moves a rollout to ~/.codex/archived_sessions when a thread is
        # archived. Read that folder too, so a call written just before the
        # move is not lost; stable event IDs make the re-read idempotent.
        archived = self.source_root.parent / "archived_sessions"
        if self.source_root.name == "sessions" and archived.is_dir():
            for directory, _, names in os.walk(archived):
                files.extend(Path(directory) / name for name in names if name.endswith(".jsonl"))
        return sorted(files)

    def _initialize_high_water(self, files: list[Path]) -> int:
        marker = self.state_dir / "installation.json"
        if marker.exists():
            installation = json.loads(marker.read_text(encoding="utf-8"))
            if installation["source_root"] != str(self.source_root):
                raise ValueError("state directory belongs to a different source root")
            self._installed_at = datetime.fromisoformat(installation["created_at"])
            if installation["complete"]:
                return 0
        else:
            # 先固化整批安装高水位。崩溃恢复重复使用它，不把新事件当历史跳过。
            installation = {
                "source_root": str(self.source_root), "complete": False,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "sources": [{"path": str(path), "identity": _file_identity(path),
                             "offset": _complete_offset(path)} for path in files],
            }
            _atomic_json(marker, installation)
        initialized = 0
        for source in installation["sources"]:
            if self.registry.cursor(INSTALLATION_ID, source["path"]) is None:
                self.registry.save_cursor(CodexCursor(
                    installation_id=INSTALLATION_ID, source_path=source["path"],
                    file_identity=source["identity"], offset=source["offset"],
                    updated_at=datetime.fromisoformat(installation["created_at"]),
                ))
                initialized += 1
        installation["complete"] = True
        _atomic_json(marker, installation)
        self._installed_at = datetime.fromisoformat(installation["created_at"])
        return initialized

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
        result = self.adapter.read(source, installation_id=INSTALLATION_ID,
                                   session_id=_session_id(source), persist_cursor=False)
        terminal = 0
        # An archived rollout seen for the first time is read from its start;
        # never report calls from before the installation (old archived threads).
        archived = "archived_sessions" in source.parts
        guarded = (cursor is None and archived) or str(source) in self._guarded
        installed_at = getattr(self, "_installed_at", None) if guarded else None
        events = [
            event for event in result.events
            if (event.component_kind in {"skill", "mcp_tool"} or is_degraded(event)) and (installed_at is None or event.occurred_at >= installed_at)
        ]
        degraded = sum(1 for event in events if is_degraded(event) and event.stage != "started")
        self._unrecognized += degraded
        if degraded:
            self.replay.note(INSTALLATION_ID, str(source), identity, start, guarded=installed_at is not None)
        for event in events:
            safe = event.model_copy(update={"user_input": None, "model_output": None, "tool_result": None})
            self.delivery.deliver(safe)
            if event.stage != "started":
                self.outbox.enqueue(safe)
                terminal += 1
        # ledger 和 outbox 都成功后才提交 cursor，重复投影依靠现有 event_id 去重。
        self.registry.save_cursor(result.next_cursor)
        self._guarded.discard(str(source))
        return len(events), terminal

    def _flush(self) -> None:
        if self.flush_enabled:
            config = LangfuseConfig.from_env()
            report = self.outbox.flush(LangfuseExporter(config).export,
                                      owner_id=f"codex-observer-{os.getpid()}-{uuid4().hex}")
            if report.failed:
                raise RuntimeError("Observer delivery failed; pending events retained")

    @staticmethod
    def _error(code: str, path: Path, exc: Exception) -> dict[str, str]:
        # 解析异常可能包含原文；状态只公开类型、来源路径和固定错误码。
        result = {"code": code, "path": str(path), "error_type": type(exc).__name__}
        if hasattr(exc, "offset"):
            result["offset"] = str(exc.offset)
        return result

    def _poll(self) -> CodexObserverRun:
        errors: list[dict[str, str]] = []
        events = terminal = initialized = 0
        files: list[Path] = []
        try:
            files = self._files()
            initialized = self._initialize_high_water(files)
            self._apply_replay()
        except (OSError, ValueError, RuntimeError) as exc:
            errors.append(self._error("source_unavailable", self.source_root, exc))
        if not errors:
            for source in files:
                try:
                    found, enqueued = self._process(source)
                    events += found
                    terminal += enqueued
                except (OSError, RuntimeError, ValueError) as exc:
                    errors.append(self._error("source_invalid", source, exc))
        try:
            self._flush()
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(self._error("delivery_failed", self.state_dir / "langfuse-content-outbox.sqlite3", exc))
        run = CodexObserverRun(len(files), events, terminal, initialized, tuple(errors))
        status = {
            "status": "failed" if errors else "ok", "pid": os.getpid(),
            "launch_pid": os.getppid() if getattr(sys, "frozen", False) else os.getpid(),
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "files": run.files, "events": run.events, "terminal_enqueued": terminal,
            "errors": errors, "outbox": self.outbox.count_by_status(),
        }
        _atomic_json(self.state_dir / "status.json", status)
        self.health.observe(status, unrecognized_calls=self._unrecognized, replayed_sources=self._replayed)
        self._unrecognized = 0
        self._replayed = 0
        return run

    def run_once(self) -> CodexObserverRun:
        with self._single_instance():
            return self._poll()

    def run_forever(self, on_report: Callable[[CodexObserverRun], None] | None = None) -> None:
        # 锁覆盖整个进程循环，等待间隔也不允许第二个 worker 接管同一个 state。
        with self._single_instance():
            while not self._stop.is_set():
                report = self._poll()
                if on_report is not None:
                    on_report(report)
                self._stop.wait(self.interval_seconds)

    def start(self) -> None:
        if self.is_running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run_forever, name="codex-observer", daemon=True)
        self._thread.start()

    def request_stop(self) -> None:
        self._stop.set()

    def stop(self) -> None:
        self.request_stop()
        if self._thread is not None:
            self._thread.join()
            self._thread = None


__all__ = ["CodexObserverRun", "CodexObserverWorker"]
