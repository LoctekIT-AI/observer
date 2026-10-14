"""Durable content-bearing outbox dedicated to Langfuse delivery."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from cowork_contracts import ComponentEventV1

from .langfuse import LangfuseExporter


@dataclass(frozen=True)
class LangfuseOutboxItem:
    event_id: str
    event: ComponentEventV1


@dataclass(frozen=True)
class LangfuseFlushReport:
    delivered: int
    failed: int


@dataclass(frozen=True)
class LangfuseDeliveryStatus:
    pending: int
    delivered: int
    last_error: str | None


class LangfuseContentOutbox:
    """Persist full terminal component events separately from aggregate telemetry."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.database_path, timeout=5, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS langfuse_content_outbox (
                event_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                lease_owner TEXT, lease_until TEXT, last_error TEXT, created_at TEXT NOT NULL, delivered_at TEXT
            )"""
        )
        self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def enqueue(self, event: ComponentEventV1, *, now: datetime | None = None) -> None:
        if event.stage == "started":
            raise ValueError("Langfuse content outbox accepts terminal events only")
        payload = event.model_dump_json()
        digest = hashlib.sha256(payload.encode()).hexdigest()
        timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_hash,status FROM langfuse_content_outbox WHERE event_id = ?", (event.event_id,)
            ).fetchone()
            if row is not None:
                if row["payload_hash"] != digest:
                    # A delivered event is already immutable at the transport
                    # boundary.  Re-reading the same native record can produce
                    # a different projection when installation identity is
                    # paired after the first delivery; the stable event ID
                    # still makes this replay idempotent.
                    if row["status"] == "delivered":
                        return
                    raise ValueError("Langfuse event_id conflicts with an existing payload")
                return
            self._connection.execute(
                "INSERT INTO langfuse_content_outbox(event_id,payload_json,payload_hash,status,created_at) VALUES(?,?,?,'pending',?)",
                (event.event_id, payload, digest, timestamp),
            )
            self._connection.commit()

    def claim(self, *, owner_id: str, now: datetime, limit: int = 100, lease: timedelta = timedelta(seconds=30)) -> list[LangfuseOutboxItem]:
        current = now.astimezone(timezone.utc)
        until = (current + lease).isoformat()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            rows = self._connection.execute(
                """SELECT event_id,payload_json FROM langfuse_content_outbox
                WHERE status='pending' AND (lease_until IS NULL OR lease_until <= ?)
                ORDER BY created_at,event_id LIMIT ?""", (current.isoformat(), limit)
            ).fetchall()
            for row in rows:
                self._connection.execute(
                    "UPDATE langfuse_content_outbox SET lease_owner=?,lease_until=?,attempts=attempts+1 WHERE event_id=?",
                    (owner_id, until, row["event_id"]),
                )
            self._connection.commit()
        return [LangfuseOutboxItem(row["event_id"], ComponentEventV1.model_validate_json(row["payload_json"])) for row in rows]

    def flush(self, export: Callable[[ComponentEventV1], object], *, owner_id: str, limit: int = 100) -> LangfuseFlushReport:
        items = self.claim(owner_id=owner_id, now=datetime.now(timezone.utc), limit=limit)
        delivered = failed = 0
        for item in items:
            try:
                export(item.event)
            except Exception as exc:
                failed += 1
                self._finish(item.event_id, owner_id, delivered=False, error=str(exc))
            else:
                delivered += 1
                self._finish(item.event_id, owner_id, delivered=True, error=None)
        return LangfuseFlushReport(delivered, failed)

    def _finish(self, event_id: str, owner_id: str, *, delivered: bool, error: str | None) -> None:
        with self._lock:
            if delivered:
                cursor = self._connection.execute(
                    "UPDATE langfuse_content_outbox SET status='delivered',delivered_at=?,lease_owner=NULL,lease_until=NULL,last_error=NULL WHERE event_id=? AND lease_owner=?",
                    (datetime.now(timezone.utc).isoformat(), event_id, owner_id),
                )
            else:
                cursor = self._connection.execute(
                    "UPDATE langfuse_content_outbox SET lease_owner=NULL,lease_until=NULL,last_error=? WHERE event_id=? AND lease_owner=?",
                    ((error or "export failed")[:2000], event_id, owner_id),
                )
            if cursor.rowcount != 1:
                self._connection.rollback()
                raise RuntimeError("Langfuse outbox lease ownership changed")
            self._connection.commit()

    def has_invocation(self, invocation_id: str, stage: str) -> bool:
        """True when a terminal event for this native call is already queued or delivered.

        Used where the same native call can be read from more than one source
        file (Claude's LevelDB moves records from its log into tables).
        """
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM langfuse_content_outbox WHERE json_extract(payload_json,'$.invocation_id') = ? "
                "AND json_extract(payload_json,'$.stage') = ? LIMIT 1",
                (invocation_id, stage),
            ).fetchone()
        return row is not None

    def count_by_status(self) -> dict[str, int]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT status,COUNT(*) AS count FROM langfuse_content_outbox GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def delivery_status(self) -> LangfuseDeliveryStatus:
        with self._lock:
            counts = self.count_by_status()
            row = self._connection.execute(
                """SELECT last_error FROM langfuse_content_outbox
                WHERE last_error IS NOT NULL ORDER BY created_at DESC,event_id DESC LIMIT 1"""
            ).fetchone()
        return LangfuseDeliveryStatus(
            pending=counts.get("pending", 0),
            delivered=counts.get("delivered", 0),
            last_error=str(row["last_error"]) if row is not None else None,
        )


class LangfuseDeliveryWorker:
    """Continuously deliver terminal events from the durable content outbox."""

    def __init__(
        self,
        outbox: LangfuseContentOutbox,
        exporter: LangfuseExporter,
        *,
        interval_seconds: float = 5.0,
        batch_size: int = 100,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("Langfuse delivery interval must be positive")
        if batch_size < 1:
            raise ValueError("Langfuse delivery batch size must be positive")
        self.outbox = outbox
        self.exporter = exporter
        self.interval_seconds = interval_seconds
        self.batch_size = batch_size
        self.worker_id = f"langfuse-delivery-{uuid4().hex}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.RLock()

    @property
    def is_running(self) -> bool:
        with self._lifecycle_lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="langfuse-delivery",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            thread = self._thread
            self._stop.set()
        if thread is not None:
            thread.join()
        with self._lifecycle_lock:
            if self._thread is thread:
                self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            self.outbox.flush(
                self.exporter.export,
                owner_id=self.worker_id,
                limit=self.batch_size,
            )
            self._stop.wait(self.interval_seconds)


__all__ = [
    "LangfuseContentOutbox",
    "LangfuseDeliveryStatus",
    "LangfuseDeliveryWorker",
    "LangfuseFlushReport",
    "LangfuseOutboxItem",
]
