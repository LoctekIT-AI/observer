"""SQLite persistence adapter for the single-process local Runtime sidecar."""

from __future__ import annotations

import json
import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any, Iterable, List, Optional, Sequence

from cowork_contracts import AssetRef as CatalogAssetRef
from cowork_runtime.contracts import (
    ArtifactRef,
    CompactionRecord,
    PinnedConstraint,
    Run,
    RuntimeEvent,
    SessionMessage,
    Turn,
)
from cowork_runtime.contracts.base import utc_now
from cowork_runtime.contracts.enums import (
    EventVisibility,
    RunStatus,
    RuntimeEventType,
    TurnStatus,
)
from cowork_runtime.contracts.errors import RuntimeSequenceError
from cowork_runtime.contracts.ids import new_contract_id
from cowork_contracts import (
    InstallationBindingV1,
    PluginManifestV1,
    ContextSnapshot,
    DelegationAggregate,
    DelegationReceipt,
    DelegationReceiptVerification,
    DelegationSpec,
    DelegationTaskStatus,
    DelegationVerificationAggregate,
    TaskEnvelope,
)
from cowork_contracts.crawler import CrawlerDataset, CrawlerDatasetVersion, SessionCrawlerDatasetLink
from cowork_runtime.dataset_quality import with_dataset_quality

from .asset_catalog import asset_ref_from_artifact, asset_ref_from_dataset

from .checkpoint_codec import (
    checkpoint_from_dict as _checkpoint_from_dict,
    checkpoint_to_dict as _checkpoint_to_dict,
)
from .delegation_inbox import (
    DelegationChildIdentity,
    DelegationContinuationRequest,
    DelegationInboxConflict,
    DelegationTask,
    _assert_receipt_matches,
    _assert_receipt_matches_task,
    _assert_task_matches,
    _assert_verification_matches,
    _assert_verification_matches_task,
    _expired_receipt_for_task,
)

if TYPE_CHECKING:
    from cowork_runtime.loop.types import LoopCheckpoint
    from cowork_runtime.orchestration.types import OrchestrationState

SQLITE_SCHEMA_VERSION = 10
DEFAULT_DATABASE_NAME = "cowork-runtime.sqlite3"


@dataclass(frozen=True)
class TelemetryOutboxItem:
    event_id: str
    idempotency_key: str
    payload_hash: str
    event: dict[str, Any]
    priority: str
    status: str
    attempt_count: int
    next_attempt_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class TelemetryOutboxStatus:
    status: str
    attempt_count: int
    error_code: str | None


@dataclass(frozen=True)
class TelemetryOutboxLimits:
    max_rows: int = 50_000
    max_bytes: int = 64 * 1024 * 1024


class TelemetryOutboxCapacityError(RuntimeError):
    """The durable outbox is full and no progress event can be evicted."""


class SQLiteRuntimeStore:
    """Persist Runtime records, Loop checkpoints, plans, and ordered events."""

    def __init__(self, database_path: str | Path, *, telemetry_outbox_limits: TelemetryOutboxLimits | None = None) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._closed = False
        self._telemetry_outbox_limits = telemetry_outbox_limits or TelemetryOutboxLimits()
        self._connection = sqlite3.connect(
            self.database_path,
            timeout=5.0,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._configure()
        self._initialize_schema()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True

    @property
    def schema_version(self) -> int:
        """Return the forward-migrated schema version for the readiness probe."""

        row = self._fetch_one("SELECT version FROM runtime_schema LIMIT 1", ())
        return int(row["version"]) if row is not None else 0

    def get_run(self, run_id: str) -> Optional[Run]:
        row = self._fetch_one(
            "SELECT document_json FROM runtime_runs WHERE run_id = ?",
            (run_id,),
        )
        return Run.model_validate(_load_json(row["document_json"])) if row else None

    def save_run(self, run: Run) -> None:
        self._execute_write(
            """
            INSERT INTO runtime_runs(run_id, status, document_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                status = excluded.status,
                document_json = excluded.document_json,
                updated_at = excluded.updated_at
            """,
            (
                run.run_id,
                run.status.value,
                _dump_json(run.model_dump(mode="json")),
                run.updated_at.isoformat(),
            ),
        )

    def list_runs(self, *, statuses: Optional[Iterable[RunStatus]] = None) -> List[Run]:
        status_values = _enum_values(statuses)
        query = "SELECT document_json FROM runtime_runs"
        parameters: Sequence[object] = ()
        if status_values:
            query += f" WHERE status IN ({_placeholders(status_values)})"
            parameters = status_values
        query += " ORDER BY updated_at, run_id"
        rows = self._fetch_all(query, parameters)
        return [Run.model_validate(_load_json(row["document_json"])) for row in rows]

    def get_turn(self, turn_id: str) -> Optional[Turn]:
        row = self._fetch_one(
            "SELECT document_json FROM runtime_turns WHERE turn_id = ?",
            (turn_id,),
        )
        return Turn.model_validate(_load_json(row["document_json"])) if row else None

    def save_turn(self, turn: Turn) -> None:
        self._execute_write(
            """
            INSERT INTO runtime_turns(turn_id, run_id, status, document_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(turn_id) DO UPDATE SET
                status = excluded.status,
                document_json = excluded.document_json,
                updated_at = excluded.updated_at
            """,
            (
                turn.turn_id,
                turn.run_id,
                turn.status.value,
                _dump_json(turn.model_dump(mode="json")),
                turn.updated_at.isoformat(),
            ),
        )

    def list_turns(
        self,
        run_id: str,
        *,
        statuses: Optional[Iterable[TurnStatus]] = None,
    ) -> List[Turn]:
        status_values = _enum_values(statuses)
        query = "SELECT document_json FROM runtime_turns WHERE run_id = ?"
        parameters: List[object] = [run_id]
        if status_values:
            query += f" AND status IN ({_placeholders(status_values)})"
            parameters.extend(status_values)
        query += " ORDER BY updated_at, turn_id"
        rows = self._fetch_all(query, parameters)
        return [Turn.model_validate(_load_json(row["document_json"])) for row in rows]

    def get_adapter_plugin(self, plugin_id: str, installation_id: str) -> PluginManifestV1 | None:
        row = self._fetch_one(
            """SELECT plugin_id, runtime, contract_version, installation_id, enabled, capabilities_json
            FROM runtime_plugins WHERE plugin_id = ? AND installation_id = ?""",
            (plugin_id, installation_id),
        )
        return _adapter_plugin_from_row(row) if row is not None else None

    def find_active_adapter_installation(self, installation_id: str) -> PluginManifestV1 | None:
        row = self._fetch_one(
            """SELECT plugin_id, runtime, contract_version, installation_id, enabled, capabilities_json
            FROM runtime_plugins WHERE installation_id = ? AND enabled = 1""",
            (installation_id,),
        )
        return _adapter_plugin_from_row(row) if row is not None else None

    def save_adapter_plugin(self, manifest: PluginManifestV1, *, now: datetime) -> None:
        timestamp = now.isoformat()
        with self._immediate_transaction() as connection:
            existing = connection.execute(
                "SELECT plugin_id FROM runtime_installations WHERE installation_id = ?",
                (manifest.installation_id,),
            ).fetchone()
            if existing is not None and str(existing["plugin_id"]) != manifest.plugin_id:
                raise ValueError("installation_id is already owned by another plugin")
            connection.execute(
                """INSERT INTO runtime_plugins
                (plugin_id, installation_id, runtime, contract_version, enabled, capabilities_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(plugin_id, installation_id) DO UPDATE SET
                    runtime = excluded.runtime,
                    contract_version = excluded.contract_version,
                    enabled = excluded.enabled,
                    capabilities_json = excluded.capabilities_json,
                    updated_at = excluded.updated_at""",
                (
                    manifest.plugin_id,
                    manifest.installation_id,
                    manifest.runtime,
                    manifest.contract_version,
                    int(manifest.enabled),
                    _dump_json(manifest.capabilities),
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """INSERT INTO runtime_installations
                (installation_id, plugin_id, status, user_id, tenant_id, paired_at, updated_at)
                VALUES (?, ?, 'unpaired', NULL, NULL, NULL, ?)
                ON CONFLICT(installation_id) DO UPDATE SET
                    plugin_id = excluded.plugin_id,
                    updated_at = excluded.updated_at""",
                (manifest.installation_id, manifest.plugin_id, timestamp),
            )

    def set_adapter_enabled(
        self,
        plugin_id: str,
        installation_id: str,
        enabled: bool,
        *,
        now: datetime,
    ) -> None:
        self._execute_write(
            """UPDATE runtime_plugins SET enabled = ?, updated_at = ?
            WHERE plugin_id = ? AND installation_id = ?""",
            (int(enabled), now.isoformat(), plugin_id, installation_id),
        )

    def get_adapter_binding(self, installation_id: str) -> InstallationBindingV1 | None:
        row = self._fetch_one(
            """SELECT installation_id, user_id, tenant_id, status
            FROM runtime_installations WHERE installation_id = ? AND status = 'paired'""",
            (installation_id,),
        )
        if row is None:
            return None
        return InstallationBindingV1(
            installation_id=str(row["installation_id"]),
            user_id=str(row["user_id"]),
            tenant_id=str(row["tenant_id"]),
            status="paired",
        )

    def save_adapter_binding(self, binding: InstallationBindingV1, *, now: datetime) -> None:
        with self._immediate_transaction() as connection:
            updated = connection.execute(
                """UPDATE runtime_installations
                SET status = 'paired', user_id = ?, tenant_id = ?, paired_at = ?, updated_at = ?
                WHERE installation_id = ?""",
                (
                    binding.user_id,
                    binding.tenant_id,
                    now.isoformat(),
                    now.isoformat(),
                    binding.installation_id,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("adapter installation is not registered")

    def clear_adapter_binding(self, installation_id: str, *, now: datetime) -> None:
        self._execute_write(
            """UPDATE runtime_installations
            SET status = 'unpaired', user_id = NULL, tenant_id = NULL, paired_at = NULL, updated_at = ?
            WHERE installation_id = ?""",
            (now.isoformat(), installation_id),
        )

    # Telemetry Outbox -------------------------------------------------

    def append_telemetry_outbox(
        self,
        event: dict[str, Any],
        *,
        now: datetime,
        priority: str = "terminal",
    ) -> "TelemetryOutboxItem":
        """Append one validated telemetry event; an equal retry is idempotent."""

        from cowork_contracts import validate_event

        validated = validate_event(event)
        if not validated.valid:
            raise ValueError(f"telemetry event is invalid: {validated.rejection_code}")
        event_id = str(event["event_id"])
        if priority not in {"progress", "terminal", "feedback"}:
            raise ValueError("telemetry outbox priority is invalid")
        payload_json = _dump_json(event)
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        timestamp = now.astimezone(timezone.utc).isoformat()
        with self._immediate_transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM runtime_telemetry_outbox WHERE event_id = ?", (event_id,)
            ).fetchone()
            if existing is not None:
                if str(existing["payload_hash"]) != payload_hash:
                    raise ValueError("telemetry event_id conflicts with an existing payload")
                return _telemetry_outbox_item_from_row(existing)
            existing_key = connection.execute(
                "SELECT * FROM runtime_telemetry_outbox WHERE idempotency_key = ?",
                (str(event["idempotency_key"]),),
            ).fetchone()
            if existing_key is not None:
                if str(existing_key["payload_hash"]) != payload_hash:
                    raise ValueError("telemetry idempotency key conflicts with an existing payload")
                return _telemetry_outbox_item_from_row(existing_key)
            count_row = connection.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(LENGTH(event_json)), 0) AS bytes FROM runtime_telemetry_outbox WHERE status != 'delivered'"
            ).fetchone()
            rows = int(count_row["count"])
            encoded_bytes = int(count_row["bytes"])
            if rows >= self._telemetry_outbox_limits.max_rows or encoded_bytes + len(payload_json.encode("utf-8")) > self._telemetry_outbox_limits.max_bytes:
                if priority == "progress":
                    raise TelemetryOutboxCapacityError("telemetry outbox is full")
                evict = connection.execute(
                    "SELECT event_id FROM runtime_telemetry_outbox WHERE priority = 'progress' AND status = 'pending' ORDER BY created_at, event_id LIMIT 1"
                ).fetchone()
                if evict is None:
                    raise TelemetryOutboxCapacityError("telemetry outbox is full; no evictable progress event")
                connection.execute("DELETE FROM runtime_telemetry_outbox WHERE event_id = ?", (str(evict["event_id"]),))
            connection.execute(
                """INSERT INTO runtime_telemetry_outbox
                (event_id, idempotency_key, payload_hash, event_json, priority, status, attempt_count,
                 next_attempt_at, lease_owner, lease_expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, NULL, NULL, ?, ?)""",
                (event_id, str(event["idempotency_key"]), payload_hash, payload_json, priority, timestamp, timestamp, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM runtime_telemetry_outbox WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:  # pragma: no cover - SQLite INSERT followed by SELECT must return the row.
                raise RuntimeError("telemetry outbox append did not persist")
            return _telemetry_outbox_item_from_row(row)

    def claim_telemetry_outbox(
        self,
        *,
        owner_id: str,
        now: datetime,
        lease: timedelta,
        limit: int = 100,
    ) -> list["TelemetryOutboxItem"]:
        if not owner_id.strip():
            raise ValueError("telemetry outbox owner_id must be non-empty")
        if lease <= timedelta(0):
            raise ValueError("telemetry outbox lease must be positive")
        current = now.astimezone(timezone.utc)
        lease_expires = current + lease
        claimed: list[TelemetryOutboxItem] = []
        with self._immediate_transaction() as connection:
            rows = connection.execute(
                """SELECT * FROM runtime_telemetry_outbox
                WHERE (status = 'pending' AND next_attempt_at <= ?)
                   OR (status = 'leased' AND lease_expires_at <= ?)
                ORDER BY created_at, event_id LIMIT ?""",
                (current.isoformat(), current.isoformat(), max(1, min(limit, 1000))),
            ).fetchall()
            for row in rows:
                updated = connection.execute(
                    """UPDATE runtime_telemetry_outbox
                    SET status = 'leased', lease_owner = ?, lease_expires_at = ?, updated_at = ?
                    WHERE event_id = ? AND (
                        (status = 'pending' AND next_attempt_at <= ?)
                        OR (status = 'leased' AND lease_expires_at <= ?)
                    )""",
                    (
                        owner_id.strip(),
                        lease_expires.isoformat(),
                        current.isoformat(),
                        str(row["event_id"]),
                        current.isoformat(),
                        current.isoformat(),
                    ),
                ).rowcount
                if updated != 1:
                    continue
                claimed_row = connection.execute(
                    "SELECT * FROM runtime_telemetry_outbox WHERE event_id = ?", (str(row["event_id"]),)
                ).fetchone()
                if claimed_row is None:  # pragma: no cover - same transaction retains inserted row.
                    raise RuntimeError("claimed telemetry outbox row disappeared")
                claimed.append(_telemetry_outbox_item_from_row(claimed_row))
        return claimed

    def mark_telemetry_delivered(self, event_id: str, *, owner_id: str, now: datetime) -> None:
        updated = self._execute_write_returning_count(
            """UPDATE runtime_telemetry_outbox
            SET status = 'delivered', lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
            WHERE event_id = ? AND status = 'leased' AND lease_owner = ?""",
            (now.astimezone(timezone.utc).isoformat(), event_id, owner_id),
        )
        if updated != 1:
            raise ValueError("telemetry outbox delivery acknowledgement does not own a leased event")

    def mark_telemetry_failed(
        self,
        event_id: str,
        *,
        owner_id: str,
        now: datetime,
        error_code: str,
        retry_delay: timedelta,
        max_attempts: int = 5,
    ) -> None:
        if retry_delay < timedelta(0) or max_attempts < 1:
            raise ValueError("telemetry retry policy is invalid")
        current = now.astimezone(timezone.utc)
        with self._immediate_transaction() as connection:
            row = connection.execute(
                "SELECT attempt_count FROM runtime_telemetry_outbox WHERE event_id = ? AND status = 'leased' AND lease_owner = ?",
                (event_id, owner_id),
            ).fetchone()
            if row is None:
                raise ValueError("telemetry outbox failure report does not own a leased event")
            attempts = int(row["attempt_count"]) + 1
            status = "dead_letter" if attempts >= max_attempts else "pending"
            connection.execute(
                """UPDATE runtime_telemetry_outbox
                SET status = ?, attempt_count = ?, next_attempt_at = ?, lease_owner = NULL,
                    lease_expires_at = NULL, last_error_code = ?, updated_at = ?
                WHERE event_id = ?""",
                (
                    status,
                    attempts,
                    (current if status == "dead_letter" else current + retry_delay).isoformat(),
                    error_code,
                    current.isoformat(),
                    event_id,
                ),
            )

    def telemetry_outbox_status(self, event_id: str) -> "TelemetryOutboxStatus":
        row = self._fetch_one("SELECT * FROM runtime_telemetry_outbox WHERE event_id = ?", (event_id,))
        if row is None:
            raise KeyError(event_id)
        item = _telemetry_outbox_item_from_row(row)
        return TelemetryOutboxStatus(item.status, item.attempt_count, row["last_error_code"])

    def telemetry_producer_instance(self) -> str:
        """Return the stable opaque producer instance for this local database."""

        with self._immediate_transaction() as connection:
            row = connection.execute(
                "SELECT instance_pseudonym FROM runtime_telemetry_producer WHERE id = 1"
            ).fetchone()
            if row is not None:
                return str(row["instance_pseudonym"])
            instance = "aihub-local-" + secrets.token_urlsafe(24)
            timestamp = datetime.now(timezone.utc).isoformat()
            connection.execute(
                "INSERT INTO runtime_telemetry_producer(id, instance_pseudonym, created_at) VALUES (1, ?, ?)",
                (instance, timestamp),
            )
            return instance

    def create_adapter_pairing_challenge(
        self,
        challenge_digest: str,
        installation_id: str,
        expires_at: datetime,
        *,
        created_at: datetime,
    ) -> None:
        self._execute_write(
            """INSERT INTO runtime_pairing_challenges
            (challenge_digest, installation_id, expires_at, consumed_at, created_at)
            VALUES (?, ?, ?, NULL, ?)""",
            (challenge_digest, installation_id, expires_at.isoformat(), created_at.isoformat()),
        )

    def pair_adapter_installation(
        self,
        challenge_digest: str,
        installation_id: str,
        user_id: str,
        tenant_id: str,
        *,
        now: datetime,
    ) -> dict[str, Any] | None:
        """Consume a challenge and bind identity in one immediate transaction."""

        with self._immediate_transaction() as connection:
            row = connection.execute(
                """SELECT installation_id, expires_at, consumed_at
                FROM runtime_pairing_challenges WHERE challenge_digest = ?""",
                (challenge_digest,),
            ).fetchone()
            if row is None:
                return None
            result = {
                "installation_id": str(row["installation_id"]),
                "expires_at": str(row["expires_at"]),
            }
            if result["installation_id"] != installation_id:
                result["state"] = "installation_mismatch"
                return result
            if row["consumed_at"] is not None:
                result["state"] = "consumed"
                return result
            if _parse_datetime(row["expires_at"]) <= now:
                result["state"] = "expired"
                return result
            updated = connection.execute(
                """UPDATE runtime_installations
                SET status = 'paired', user_id = ?, tenant_id = ?, paired_at = ?, updated_at = ?
                WHERE installation_id = ?""",
                (user_id, tenant_id, now.isoformat(), now.isoformat(), installation_id),
            )
            if updated.rowcount != 1:
                raise ValueError("adapter installation is not registered")
            connection.execute(
                "UPDATE runtime_pairing_challenges SET consumed_at = ? WHERE challenge_digest = ?",
                (now.isoformat(), challenge_digest),
            )
            result["state"] = "accepted"
            return result

    def get_adapter_cursor(self, installation_id: str, source_path: str) -> dict[str, Any] | None:
        row = self._fetch_one(
            """SELECT installation_id, source_path, file_identity, offset, updated_at
            FROM runtime_adapter_cursors WHERE installation_id = ? AND source_path = ?""",
            (installation_id, source_path),
        )
        return dict(row) if row is not None else None

    def save_adapter_cursor(self, cursor: Any) -> None:
        self._execute_write(
            """INSERT INTO runtime_adapter_cursors
            (installation_id, source_path, file_identity, offset, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(installation_id, source_path) DO UPDATE SET
                file_identity = excluded.file_identity,
                offset = excluded.offset,
                updated_at = excluded.updated_at""",
            (
                cursor.installation_id,
                cursor.source_path,
                cursor.file_identity,
                cursor.offset,
                cursor.updated_at.isoformat(),
            ),
        )

    def get(self, run_id: str) -> Optional["LoopCheckpoint"]:
        row = self._fetch_one(
            "SELECT document_json FROM runtime_checkpoints WHERE run_id = ?",
            (run_id,),
        )
        return _checkpoint_from_dict(_load_json(row["document_json"])) if row else None

    def save(self, checkpoint: "LoopCheckpoint") -> None:
        self._execute_write(
            """
            INSERT INTO runtime_checkpoints(run_id, status, document_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                status = excluded.status,
                document_json = excluded.document_json,
                updated_at = excluded.updated_at
            """,
            (
                checkpoint.checkpoint_key or checkpoint.run_id,
                checkpoint.status.value,
                _dump_json(_checkpoint_to_dict(checkpoint)),
                utc_now().isoformat(),
            ),
        )

    def delete(self, run_id: str) -> None:
        self._execute_write(
            "DELETE FROM runtime_checkpoints WHERE run_id = ?",
            (run_id,),
        )

    def get_crawl_job(self, crawl_job_id: str) -> Optional[dict[str, Any]]:
        row = self._fetch_one(
            "SELECT document_json FROM runtime_crawl_jobs WHERE crawl_job_id = ?",
            (crawl_job_id,),
        )
        return _load_json(row["document_json"]) if row else None

    def save_crawl_job(self, checkpoint: dict[str, Any]) -> dict[str, Any]:
        crawl_job_id = _require_crawl_job_id(checkpoint)
        self._execute_write(
            """
            INSERT INTO runtime_crawl_jobs(crawl_job_id, run_id, status, document_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(crawl_job_id) DO UPDATE SET
                run_id = excluded.run_id,
                status = excluded.status,
                document_json = excluded.document_json,
                updated_at = excluded.updated_at
            """,
            (
                crawl_job_id,
                str(checkpoint.get("run_id") or ""),
                str(checkpoint.get("status") or ""),
                _dump_json(checkpoint),
                utc_now().isoformat(),
            ),
        )
        row = self._fetch_one(
            "SELECT document_json FROM runtime_crawl_jobs WHERE crawl_job_id = ?",
            (crawl_job_id,),
        )
        return _load_json(row["document_json"]) if row else dict(checkpoint)

    def delete_crawl_job(self, crawl_job_id: str) -> None:
        self._execute_write(
            "DELETE FROM runtime_crawl_jobs WHERE crawl_job_id = ?",
            (crawl_job_id,),
        )

    def get_state(self, run_id: str) -> Optional["OrchestrationState"]:
        row = self._fetch_one(
            "SELECT document_json FROM runtime_orchestration_states WHERE run_id = ?",
            (run_id,),
        )
        return _orchestration_state_from_dict(_load_json(row["document_json"])) if row else None

    def save_state(self, state: "OrchestrationState") -> None:
        self._execute_write(
            """
            INSERT INTO runtime_orchestration_states(run_id, status, document_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                status = excluded.status,
                document_json = excluded.document_json,
                updated_at = excluded.updated_at
            """,
            (
                state.run_id,
                state.status.value,
                _dump_json(_orchestration_state_to_dict(state)),
                utc_now().isoformat(),
            ),
        )

    def delete_state(self, run_id: str) -> None:
        self._execute_write(
            "DELETE FROM runtime_orchestration_states WHERE run_id = ?",
            (run_id,),
        )

    def delete_session(self, session_id: str) -> int:
        """Delete the local session and every persisted fact owned by it."""
        with self._immediate_transaction() as connection:
            rows = connection.execute(
                """
                SELECT run_id FROM runtime_runs
                WHERE json_extract(document_json, '$.session_id') = ?
                """,
                (session_id,),
            ).fetchall()
            run_ids = [str(row["run_id"]) for row in rows]
            if run_ids:
                placeholders = ",".join("?" for _ in run_ids)
                parameters = tuple(run_ids)
                child_rows = connection.execute(
                    f"SELECT child_run_id FROM runtime_delegation_tasks WHERE parent_run_id IN ({placeholders})",
                    parameters,
                ).fetchall()
                owned_run_ids = [*run_ids, *(str(row["child_run_id"]) for row in child_rows)]
                owned_placeholders = ",".join("?" for _ in owned_run_ids)
                owned_parameters = tuple(owned_run_ids)
                for table in (
                    "runtime_events",
                    "runtime_turns",
                    "runtime_checkpoints",
                    "runtime_orchestration_states",
                ):
                    connection.execute(
                        f"DELETE FROM {table} WHERE run_id IN ({owned_placeholders})",
                        owned_parameters,
                    )
                for table in ("runtime_tool_outputs", "runtime_artifact_metadata"):
                    exists = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (table,),
                    ).fetchone()
                    if exists:
                        connection.execute(
                            f"DELETE FROM {table} WHERE run_id IN ({owned_placeholders})",
                            owned_parameters,
                        )
                connection.execute(
                    f"DELETE FROM runtime_delegation_receipts WHERE parent_run_id IN ({placeholders})",
                    parameters,
                )
                connection.execute(
                    f"DELETE FROM runtime_delegation_consumptions WHERE parent_run_id IN ({placeholders})",
                    parameters,
                )
                connection.execute(
                    f"DELETE FROM runtime_delegation_receipt_consumptions WHERE parent_run_id IN ({placeholders})",
                    parameters,
                )
                connection.execute(
                    f"DELETE FROM runtime_delegation_leases WHERE parent_run_id IN ({placeholders})",
                    parameters,
                )
                connection.execute(
                    f"DELETE FROM runtime_delegation_tasks WHERE parent_run_id IN ({placeholders})",
                    parameters,
                )
                connection.execute(
                    f"DELETE FROM runtime_runs WHERE run_id IN ({placeholders})",
                    parameters,
                )
            connection.execute("DELETE FROM runtime_session_messages WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM runtime_session_artifacts WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM runtime_session_asset_links WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM runtime_asset_versions WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM runtime_session_crawler_datasets WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM runtime_session_crawler_dataset_links WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM runtime_crawler_dataset_versions WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM runtime_crawler_dataset_sync_outbox WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM runtime_pinned_constraints WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM runtime_compactions WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM runtime_context_snapshots WHERE conversation_id = ?", (session_id,))
            connection.execute("DELETE FROM runtime_tasks WHERE conversation_id = ?", (session_id,))
            return len(run_ids)

    def get_active_task(self, conversation_id: str) -> TaskEnvelope | None:
        row = self._fetch_one(
            "SELECT document_json FROM runtime_tasks WHERE conversation_id = ?",
            (conversation_id,),
        )
        return TaskEnvelope.model_validate(_load_json(row["document_json"])) if row else None

    def next_context_snapshot_revision(self, conversation_id: str) -> int:
        row = self._fetch_one(
            "SELECT COALESCE(MAX(revision), 0) AS revision FROM runtime_context_snapshots WHERE conversation_id = ?",
            (conversation_id,),
        )
        return int(row["revision"] if row else 0) + 1

    def save_task_context(self, task: TaskEnvelope, snapshot: ContextSnapshot) -> ContextSnapshot:
        """Commit a task update and its immutable snapshot in one SQLite transaction.

        The task is the active semantic owner for the Conversation.  A snapshot
        may be read many times after restart, but a revision can never be
        reassigned to different task facts.
        """

        if task.conversation_id != snapshot.conversation_id or task.task_id != snapshot.task_id:
            raise ValueError("task context snapshot lineage mismatch")
        with self._immediate_transaction() as connection:
            existing = connection.execute(
                """SELECT document_json FROM runtime_context_snapshots
                WHERE conversation_id = ? AND revision = ?""",
                (snapshot.conversation_id, snapshot.revision),
            ).fetchone()
            if existing is not None:
                persisted = ContextSnapshot.model_validate(_load_json(existing["document_json"]))
                if persisted.snapshot_id != snapshot.snapshot_id:
                    raise ValueError("context snapshot revision already belongs to another snapshot")
                return persisted
            connection.execute(
                """INSERT INTO runtime_tasks(task_id, conversation_id, updated_at, document_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    task_id = excluded.task_id,
                    updated_at = excluded.updated_at,
                    document_json = excluded.document_json""",
                (
                    task.task_id,
                    task.conversation_id,
                    task.updated_at.isoformat(),
                    _dump_json(task.model_dump(mode="json")),
                ),
            )
            connection.execute(
                """INSERT INTO runtime_context_snapshots
                (snapshot_id, conversation_id, task_id, run_id, turn_id, revision, created_at, document_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot.snapshot_id,
                    snapshot.conversation_id,
                    snapshot.task_id,
                    snapshot.run_id,
                    snapshot.turn_id,
                    snapshot.revision,
                    snapshot.created_at.isoformat(),
                    _dump_json(snapshot.model_dump(mode="json")),
                ),
            )
        return snapshot.model_copy(deep=True)

    def get_context_snapshot(self, conversation_id: str, revision: int) -> ContextSnapshot | None:
        row = self._fetch_one(
            """SELECT document_json FROM runtime_context_snapshots
            WHERE conversation_id = ? AND revision = ?""",
            (conversation_id, revision),
        )
        return ContextSnapshot.model_validate(_load_json(row["document_json"])) if row else None

    def get_latest_context_snapshot(self, conversation_id: str) -> ContextSnapshot | None:
        row = self._fetch_one(
            """SELECT document_json FROM runtime_context_snapshots
            WHERE conversation_id = ? ORDER BY revision DESC LIMIT 1""",
            (conversation_id,),
        )
        return ContextSnapshot.model_validate(_load_json(row["document_json"])) if row else None

    def append_message(self, message: SessionMessage) -> SessionMessage:
        self._execute_write(
            """
            INSERT INTO runtime_session_messages(message_id, session_id, run_id, created_at, document_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO NOTHING
            """,
            (
                message.message_id,
                message.session_id,
                message.run_id,
                message.created_at.isoformat(),
                _dump_json(message.model_dump(mode="json")),
            ),
        )
        row = self._fetch_one(
            "SELECT document_json FROM runtime_session_messages WHERE message_id = ?",
            (message.message_id,),
        )
        return SessionMessage.model_validate(_load_json(row["document_json"]))

    def list_messages(self, session_id: str, *, limit: Optional[int] = None) -> List[SessionMessage]:
        # Fetch the newest window first, then restore chronological order. A
        # plain ORDER BY ... LIMIT returns the oldest messages and silently
        # drops the active tail of long sessions.
        if limit is None:
            rows = self._fetch_all(
                """SELECT document_json FROM runtime_session_messages
                WHERE session_id = ? ORDER BY created_at, message_id""",
                (session_id,),
            )
        else:
            rows = self._fetch_all(
                """SELECT document_json FROM (
                    SELECT document_json, created_at, message_id
                    FROM runtime_session_messages
                    WHERE session_id = ?
                    ORDER BY created_at DESC, message_id DESC LIMIT ?
                ) ORDER BY created_at, message_id""",
                (session_id, max(1, min(limit, 1000))),
            )
        return [SessionMessage.model_validate(_load_json(row["document_json"])) for row in rows]

    def append_artifact_ref(self, session_id: str, artifact: ArtifactRef) -> ArtifactRef:
        with self._immediate_transaction() as connection:
            connection.execute(
                """
                INSERT INTO runtime_session_artifacts(session_id, artifact_id, created_at, document_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id, artifact_id) DO NOTHING
                """,
                (
                    session_id,
                    artifact.artifact_id,
                    artifact.created_at.isoformat(),
                    _dump_json(artifact.model_dump(mode="json")),
                ),
            )
            row = connection.execute(
                """SELECT document_json FROM runtime_session_artifacts
                WHERE session_id = ? AND artifact_id = ?""",
                (session_id, artifact.artifact_id),
            ).fetchone()
            persisted = ArtifactRef.model_validate(_load_json(row["document_json"]))
            asset, source = asset_ref_from_artifact(persisted)
            self._register_asset_in_transaction(connection, session_id, asset, source=source)
        return persisted

    def list_artifact_refs(self, session_id: str, *, limit: Optional[int] = None) -> List[ArtifactRef]:
        limit_clause = "" if limit is None else " LIMIT ?"
        parameters: tuple[object, ...] = (session_id,) if limit is None else (session_id, max(1, min(limit, 1000)))
        rows = self._fetch_all(
            f"""
            SELECT document_json FROM runtime_session_artifacts
            WHERE session_id = ? ORDER BY created_at, artifact_id{limit_clause}
            """,
            parameters,
        )
        return [ArtifactRef.model_validate(_load_json(row["document_json"])) for row in rows]

    def register_asset(
        self,
        session_id: str,
        asset: CatalogAssetRef,
        *,
        source: dict[str, Any],
    ) -> CatalogAssetRef:
        """Create an immutable asset version and move the Session's current pin."""

        with self._immediate_transaction() as connection:
            return self._register_asset_in_transaction(connection, session_id, asset, source=source)

    def list_asset_refs(self, session_id: str, *, limit: Optional[int] = None) -> List[CatalogAssetRef]:
        limit_clause = "" if limit is None else " LIMIT ?"
        parameters: tuple[object, ...] = (session_id,) if limit is None else (session_id, max(1, min(limit, 1000)))
        rows = self._fetch_all(
            f"""
            SELECT versions.document_json FROM runtime_session_asset_links AS links
            JOIN runtime_asset_versions AS versions ON versions.asset_version_id = links.asset_version_id
            WHERE links.session_id = ?
            ORDER BY versions.created_at, versions.asset_id, versions.asset_version_id{limit_clause}
            """,
            parameters,
        )
        return [CatalogAssetRef.model_validate(_load_json(row["document_json"])) for row in rows]

    def get_asset_ref(
        self,
        session_id: str,
        asset_id: str,
        *,
        asset_version_id: str | None = None,
    ) -> CatalogAssetRef | None:
        if asset_version_id is None:
            row = self._fetch_one(
                """SELECT versions.document_json FROM runtime_session_asset_links AS links
                JOIN runtime_asset_versions AS versions ON versions.asset_version_id = links.asset_version_id
                WHERE links.session_id = ? AND links.asset_id = ?""",
                (session_id, asset_id),
            )
        else:
            row = self._fetch_one(
                """SELECT document_json FROM runtime_asset_versions
                WHERE session_id = ? AND asset_id = ? AND asset_version_id = ?""",
                (session_id, asset_id, asset_version_id),
            )
        return CatalogAssetRef.model_validate(_load_json(row["document_json"])) if row else None

    def get_asset_source(
        self,
        session_id: str,
        asset_id: str,
        *,
        asset_version_id: str | None = None,
    ) -> dict[str, Any] | None:
        version_id = asset_version_id
        if version_id is None:
            link = self._fetch_one(
                """SELECT asset_version_id FROM runtime_session_asset_links
                WHERE session_id = ? AND asset_id = ?""",
                (session_id, asset_id),
            )
            version_id = str(link["asset_version_id"]) if link else None
        if version_id is None:
            return None
        row = self._fetch_one(
            """SELECT source_json FROM runtime_asset_versions
            WHERE session_id = ? AND asset_id = ? AND asset_version_id = ?""",
            (session_id, asset_id, version_id),
        )
        return _load_json(row["source_json"]) if row else None

    def save_crawler_dataset(self, session_id: str, dataset: CrawlerDataset) -> CrawlerDataset:
        """Persist an immutable version and update this Session's current pin.

        ``runtime_session_crawler_datasets`` remains the backward-compatible
        current-version projection for Runtime databases and tools that still
        read the v1 dataset document directly.
        """

        if dataset.ref.quality is None:
            dataset = with_dataset_quality(dataset)
        existing_link = self.get_crawler_dataset_link(session_id, dataset.ref.dataset_id)
        version_id = dataset.ref.dataset_version_id or new_contract_id("dataset_version")
        parent_version_id = dataset.ref.parent_dataset_version_id or (
            existing_link.dataset_version_id if existing_link else None
        )
        ref = dataset.ref.model_copy(
            update={
                "dataset_version_id": version_id,
                "parent_dataset_version_id": parent_version_id,
            },
            deep=True,
        )
        persisted = dataset.model_copy(update={"ref": ref}, deep=True)
        version = CrawlerDatasetVersion(
            dataset_version_id=version_id,
            dataset_id=ref.dataset_id,
            parent_dataset_version_id=parent_version_id,
            source_run_id=ref.source_run_id,
            created_at=ref.created_at,
            dataset=persisted,
        )
        link = SessionCrawlerDatasetLink(
            session_id=session_id,
            dataset_id=ref.dataset_id,
            dataset_version_id=version_id,
        )
        with self._immediate_transaction() as connection:
            connection.execute(
                """INSERT INTO runtime_crawler_dataset_versions
                (dataset_version_id, session_id, dataset_id, source_run_id, parent_dataset_version_id, created_at, document_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(dataset_version_id) DO NOTHING""",
                (
                    version.dataset_version_id,
                    session_id,
                    version.dataset_id,
                    version.source_run_id,
                    version.parent_dataset_version_id,
                    version.created_at.isoformat(),
                    _dump_json(version.model_dump(mode="json")),
                ),
            )
            connection.execute(
                """INSERT INTO runtime_session_crawler_dataset_links
                (session_id, dataset_id, dataset_version_id, pinned_at, document_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id, dataset_id) DO UPDATE SET
                    dataset_version_id = excluded.dataset_version_id,
                    pinned_at = excluded.pinned_at,
                    document_json = excluded.document_json""",
                (
                    session_id,
                    ref.dataset_id,
                    version_id,
                    link.pinned_at.isoformat(),
                    _dump_json(link.model_dump(mode="json")),
                ),
            )
            connection.execute(
                """INSERT INTO runtime_session_crawler_datasets
                (session_id, dataset_id, created_at, document_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id, dataset_id) DO UPDATE SET
                    created_at = excluded.created_at,
                    document_json = excluded.document_json""",
                (session_id, ref.dataset_id, ref.created_at.isoformat(), _dump_json(persisted.model_dump(mode="json"))),
            )
            asset, source = asset_ref_from_dataset(persisted)
            self._register_asset_in_transaction(connection, session_id, asset, source=source)
            connection.execute(
                """INSERT INTO runtime_crawler_dataset_sync_outbox
                (dataset_version_id, session_id, dataset_id, document_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(dataset_version_id) DO NOTHING""",
                (
                    version_id,
                    session_id,
                    ref.dataset_id,
                    _dump_json({
                        "dataset_version": version.model_dump(mode="json"),
                        "session_link": link.model_dump(mode="json"),
                    }),
                    utc_now().isoformat(),
                    utc_now().isoformat(),
                ),
            )
        return persisted.model_copy(deep=True)

    def list_crawler_datasets(self, session_id: str, *, limit: Optional[int] = None) -> List[CrawlerDataset]:
        limit_clause = "" if limit is None else " LIMIT ?"
        parameters: tuple[object, ...] = (session_id,) if limit is None else (session_id, max(1, min(limit, 1000)))
        rows = self._fetch_all(
            f"""
            SELECT document_json FROM runtime_session_crawler_datasets
            WHERE session_id = ? ORDER BY created_at, dataset_id{limit_clause}
            """,
            parameters,
        )
        return [CrawlerDataset.model_validate(_load_json(row["document_json"])) for row in rows]

    def get_crawler_dataset(self, session_id: str, dataset_id: str) -> Optional[CrawlerDataset]:
        row = self._fetch_one(
            """SELECT document_json FROM runtime_session_crawler_datasets
            WHERE session_id = ? AND dataset_id = ?""",
            (session_id, dataset_id),
        )
        return CrawlerDataset.model_validate(_load_json(row["document_json"])) if row else None

    def list_crawler_dataset_versions(self, session_id: str, dataset_id: str) -> List[CrawlerDatasetVersion]:
        if self.get_crawler_dataset_link(session_id, dataset_id) is None:
            return []
        rows = self._fetch_all(
            """SELECT document_json FROM runtime_crawler_dataset_versions
            WHERE session_id = ? AND dataset_id = ? ORDER BY created_at, dataset_version_id""",
            (session_id, dataset_id),
        )
        return [CrawlerDatasetVersion.model_validate(_load_json(row["document_json"])) for row in rows]

    def get_crawler_dataset_link(self, session_id: str, dataset_id: str) -> Optional[SessionCrawlerDatasetLink]:
        row = self._fetch_one(
            """SELECT document_json FROM runtime_session_crawler_dataset_links
            WHERE session_id = ? AND dataset_id = ?""",
            (session_id, dataset_id),
        )
        return SessionCrawlerDatasetLink.model_validate(_load_json(row["document_json"])) if row else None

    def list_pending_crawler_dataset_sync(self, *, limit: int = 20) -> List[tuple[CrawlerDatasetVersion, SessionCrawlerDatasetLink]]:
        rows = self._fetch_all(
            """SELECT document_json FROM runtime_crawler_dataset_sync_outbox
            ORDER BY created_at, dataset_version_id LIMIT ?""",
            (max(1, min(limit, 100)),),
        )
        pending: List[tuple[CrawlerDatasetVersion, SessionCrawlerDatasetLink]] = []
        for row in rows:
            payload = _load_json(row["document_json"])
            pending.append((
                CrawlerDatasetVersion.model_validate(payload["dataset_version"]),
                SessionCrawlerDatasetLink.model_validate(payload["session_link"]),
            ))
        return pending

    def mark_crawler_dataset_synced(self, dataset_version_id: str) -> None:
        self._execute_write(
            "DELETE FROM runtime_crawler_dataset_sync_outbox WHERE dataset_version_id = ?",
            (dataset_version_id,),
        )

    def save_pinned_constraint(self, constraint: PinnedConstraint) -> PinnedConstraint:
        self._execute_write(
            """
            INSERT INTO runtime_pinned_constraints(constraint_id, session_id, created_at, document_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(constraint_id) DO UPDATE SET document_json = excluded.document_json
            """,
            (
                constraint.constraint_id,
                constraint.session_id,
                constraint.created_at.isoformat(),
                _dump_json(constraint.model_dump(mode="json")),
            ),
        )
        row = self._fetch_one(
            "SELECT document_json FROM runtime_pinned_constraints WHERE constraint_id = ?",
            (constraint.constraint_id,),
        )
        return PinnedConstraint.model_validate(_load_json(row["document_json"]))

    def list_pinned_constraints(self, session_id: str) -> List[PinnedConstraint]:
        rows = self._fetch_all(
            """
            SELECT document_json FROM runtime_pinned_constraints
            WHERE session_id = ? ORDER BY created_at, constraint_id
            """,
            (session_id,),
        )
        return [PinnedConstraint.model_validate(_load_json(row["document_json"])) for row in rows if _load_json(row["document_json"]).get("active", True)]

    def save_compaction(self, record: CompactionRecord) -> CompactionRecord:
        self._execute_write(
            """
            INSERT INTO runtime_compactions(compaction_id, session_id, created_at, document_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(compaction_id) DO UPDATE SET document_json = excluded.document_json
            """,
            (
                record.compaction_id,
                record.session_id,
                record.created_at.isoformat(),
                _dump_json(record.model_dump(mode="json")),
            ),
        )
        row = self._fetch_one(
            "SELECT document_json FROM runtime_compactions WHERE compaction_id = ?",
            (record.compaction_id,),
        )
        return CompactionRecord.model_validate(_load_json(row["document_json"]))

    def list_compactions(self, session_id: str) -> List[CompactionRecord]:
        rows = self._fetch_all(
            """
            SELECT document_json FROM runtime_compactions
            WHERE session_id = ? ORDER BY created_at, compaction_id
            """,
            (session_id,),
        )
        return [CompactionRecord.model_validate(_load_json(row["document_json"])) for row in rows]

    def next_event(
        self,
        run_id: str,
        event_type: RuntimeEventType,
        payload: dict,
        *,
        visibility: EventVisibility = EventVisibility.USER,
        event_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> RuntimeEvent:
        with self._immediate_transaction() as connection:
            if event_id:
                existing = self._event_by_id(connection, event_id)
                if existing is not None:
                    _assert_event_run(existing, run_id)
                    return existing
            seq = self._latest_seq(connection, run_id) + 1
            event = RuntimeEvent(
                run_id=run_id,
                seq=seq,
                event_id=event_id or new_contract_id("evt"),
                type=event_type,
                occurred_at=utc_now(),
                visibility=visibility,
                payload=payload,
                trace_id=trace_id,
            )
            self._insert_event(connection, event)
            return event

    def commit_run_event(
        self,
        run: Run,
        event_type: RuntimeEventType,
        payload: dict,
        *,
        visibility: EventVisibility = EventVisibility.USER,
        event_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> RuntimeEvent:
        """Commit a Run state update and its first/next event atomically."""

        with self._immediate_transaction() as connection:
            if event_id:
                existing = self._event_by_id(connection, event_id)
                if existing is not None:
                    _assert_event_run(existing, run.run_id)
                    return existing
            self._upsert_run(connection, run)
            event = RuntimeEvent(
                run_id=run.run_id,
                seq=self._latest_seq(connection, run.run_id) + 1,
                event_id=event_id or new_contract_id("evt"),
                type=event_type,
                occurred_at=utc_now(),
                visibility=visibility,
                payload=payload,
                trace_id=trace_id,
            )
            self._insert_event(connection, event)
            return event

    def append(self, event: RuntimeEvent) -> RuntimeEvent:
        with self._immediate_transaction() as connection:
            existing = self._event_by_id(connection, event.event_id)
            if existing is not None:
                _assert_event_run(existing, event.run_id)
                return existing
            expected_seq = self._latest_seq(connection, event.run_id) + 1
            if event.seq != expected_seq:
                raise RuntimeSequenceError(
                    f"event seq must be {expected_seq}; got {event.seq}"
                )
            self._insert_event(connection, event)
            return event

    def list_events(
        self,
        run_id: str,
        after_seq: int = 0,
        limit: int = 100,
    ) -> List[RuntimeEvent]:
        bounded_limit = max(1, min(limit or 100, 500))
        rows = self._fetch_all(
            """
            SELECT document_json
            FROM runtime_events
            WHERE run_id = ? AND seq > ?
            ORDER BY seq
            LIMIT ?
            """,
            (run_id, after_seq, bounded_limit),
        )
        return [
            RuntimeEvent.from_json_dict(_load_json(row["document_json"]))
            for row in rows
        ]

    def latest_seq(self, run_id: str) -> int:
        with self._lock:
            self._ensure_open()
            return self._latest_seq(self._connection, run_id)

    def purge_run(self, run_id: str) -> int:
        """Delete only Runtime event facts for a retention-approved terminal Run."""
        with self._immediate_transaction() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM runtime_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            count = int(row["count"] if row else 0)
            connection.execute("DELETE FROM runtime_events WHERE run_id = ?", (run_id,))
            return count

    # Delegation Inbox -------------------------------------------------
    # These methods deliberately persist contracts, lifecycle, leases and
    # replay markers only. Receipt verification and Parent Context injection
    # remain outside this store until ISSUE-065.

    def ensure_task(
        self,
        spec: DelegationSpec,
        child: DelegationChildIdentity,
        *,
        revision: int = 0,
        now: datetime,
    ) -> DelegationTask:
        with self._immediate_transaction() as connection:
            row = connection.execute(
                """SELECT * FROM runtime_delegation_tasks
                WHERE parent_run_id = ? AND idempotency_key = ?""",
                (spec.parent_run_id, spec.idempotency_key),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """SELECT * FROM runtime_delegation_tasks
                    WHERE delegation_id = ? AND attempt = ?""",
                    (spec.delegation_id, spec.attempt),
                ).fetchone()
            if row is not None:
                task = _delegation_task_from_row(row)
                _assert_task_matches(task, spec, child, revision)
                return task
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM runtime_delegation_tasks WHERE parent_run_id = ?",
                (spec.parent_run_id,),
            ).fetchone()
            if int(count["count"] if count else 0) >= 3:
                raise DelegationInboxConflict("delegation parent child limit exceeded")
            task = DelegationTask(
                spec=spec.model_copy(deep=True),
                child=child,
                revision=revision,
                status=DelegationTaskStatus.PLANNED,
                created_at=now,
                updated_at=now,
                next_scan_at=now,
            )
            self._insert_delegation_task(connection, task)
            return task

    def get_task(self, delegation_id: str, attempt: int) -> Optional[DelegationTask]:
        row = self._fetch_one(
            """SELECT * FROM runtime_delegation_tasks
            WHERE delegation_id = ? AND attempt = ?""",
            (delegation_id, attempt),
        )
        return _delegation_task_from_row(row) if row else None

    def claim_task_launch(self, delegation_id: str, attempt: int, *, now: datetime) -> bool:
        with self._immediate_transaction() as connection:
            updated = connection.execute(
                """UPDATE runtime_delegation_tasks
                SET status = ?, updated_at = ?
                WHERE delegation_id = ? AND attempt = ? AND status = ?""",
                (
                    DelegationTaskStatus.RUNNING.value,
                    now.isoformat(),
                    delegation_id,
                    attempt,
                    DelegationTaskStatus.PLANNED.value,
                ),
            ).rowcount
            return bool(updated)

    def list_parent_tasks(self, parent_run_id: str) -> List[DelegationTask]:
        rows = self._fetch_all(
            """SELECT * FROM runtime_delegation_tasks WHERE parent_run_id = ?
            ORDER BY join_group, delegation_id, attempt""",
            (parent_run_id,),
        )
        return [_delegation_task_from_row(row) for row in rows]

    def list_parent_run_ids(self) -> List[str]:
        rows = self._fetch_all(
            """SELECT DISTINCT parent_run_id FROM runtime_delegation_tasks
            ORDER BY parent_run_id""",
            (),
        )
        return [str(row["parent_run_id"]) for row in rows]

    def list_due_parent_run_ids(self, now: datetime) -> List[str]:
        rows = self._fetch_all(
            """SELECT DISTINCT task.parent_run_id FROM runtime_delegation_tasks AS task
            WHERE task.status IN (?, ?, ?, ?)
              AND (task.next_scan_at IS NULL OR task.next_scan_at <= ?)
              AND NOT EXISTS (
                SELECT 1 FROM runtime_delegation_consumptions AS consumption
                WHERE consumption.parent_run_id = task.parent_run_id
                  AND consumption.join_group = task.join_group
                  AND consumption.revision = task.revision
                  AND consumption.kind = 'aggregate'
              )
            ORDER BY task.parent_run_id""",
            (
                DelegationTaskStatus.PLANNED.value,
                DelegationTaskStatus.DISPATCHED.value,
                DelegationTaskStatus.RUNNING.value,
                DelegationTaskStatus.RECEIPT_READY.value,
                now.isoformat(),
            ),
        )
        return [str(row["parent_run_id"]) for row in rows]

    def get_receipt(self, delegation_id: str, attempt: int) -> Optional[DelegationReceipt]:
        row = self._fetch_one(
            """SELECT receipt_json FROM runtime_delegation_receipts
            WHERE delegation_id = ? AND attempt = ?""",
            (delegation_id, attempt),
        )
        return DelegationReceipt.model_validate(_load_json(row["receipt_json"])) if row else None

    def put_receipt(self, receipt: DelegationReceipt) -> DelegationReceipt:
        with self._immediate_transaction() as connection:
            return self._put_receipt(connection, receipt)

    def expire_task(self, delegation_id: str, attempt: int, *, now: datetime) -> Optional[DelegationReceipt]:
        with self._immediate_transaction() as connection:
            existing = connection.execute(
                """SELECT receipt_json FROM runtime_delegation_receipts
                WHERE delegation_id = ? AND attempt = ?""",
                (delegation_id, attempt),
            ).fetchone()
            if existing is not None:
                return DelegationReceipt.model_validate(_load_json(existing["receipt_json"]))
            row = connection.execute(
                """SELECT * FROM runtime_delegation_tasks WHERE delegation_id = ? AND attempt = ?""",
                (delegation_id, attempt),
            ).fetchone()
            if row is None:
                return None
            task = _delegation_task_from_row(row)
            if (
                task.status in {DelegationTaskStatus.CANCELLED, DelegationTaskStatus.SUPERSEDED}
                or now < task.spec.budget.deadline_at
            ):
                return None
            receipt = _expired_receipt_for_task(task, now)
            persisted = self._put_receipt(connection, receipt)
            event_id = f"delegation.receipt:{receipt.delegation_id}:{receipt.attempt}"
            event = RuntimeEvent(
                run_id=task.child.child_run_id,
                seq=self._latest_seq(connection, task.child.child_run_id) + 1,
                event_id=event_id,
                type=RuntimeEventType.RUN_BLOCKED,
                occurred_at=now,
                visibility=EventVisibility.DEBUG,
                payload={"status": receipt.status, "reason_code": receipt.reason_code},
                trace_id=task.child.trace_id,
            )
            existing_event = self._event_by_id(connection, event_id)
            if existing_event is None:
                self._insert_event(connection, event)
            else:
                _assert_event_run(existing_event, task.child.child_run_id)
            return persisted

    def list_parent_receipts(self, parent_run_id: str) -> List[DelegationReceipt]:
        rows = self._fetch_all(
            """SELECT receipt_json FROM runtime_delegation_receipts
            WHERE parent_run_id = ? ORDER BY delegation_id, attempt""",
            (parent_run_id,),
        )
        return [DelegationReceipt.model_validate(_load_json(row["receipt_json"])) for row in rows]

    def get_receipt_verification(
        self,
        parent_run_id: str,
        delegation_id: str,
        attempt: int,
    ) -> Optional[DelegationReceiptVerification]:
        row = self._fetch_one(
            """SELECT verification_json FROM runtime_delegation_receipt_consumptions
            WHERE parent_run_id = ? AND delegation_id = ? AND attempt = ?""",
            (parent_run_id, delegation_id, attempt),
        )
        return DelegationReceiptVerification.model_validate(_load_json(row["verification_json"])) if row else None

    def commit_receipt_verification(
        self,
        verification: DelegationReceiptVerification,
        *,
        trace_id: Optional[str] = None,
    ) -> DelegationReceiptVerification:
        """Atomically write one immutable verdict without exposing child text."""

        with self._immediate_transaction() as connection:
            existing = connection.execute(
                """SELECT verification_json FROM runtime_delegation_receipt_consumptions
                WHERE parent_run_id = ? AND delegation_id = ? AND attempt = ?""",
                (verification.parent_run_id, verification.delegation_id, verification.attempt),
            ).fetchone()
            if existing is not None:
                persisted = DelegationReceiptVerification.model_validate(_load_json(existing["verification_json"]))
                _assert_verification_matches(persisted, verification)
                return persisted
            task_row = connection.execute(
                """SELECT * FROM runtime_delegation_tasks
                WHERE delegation_id = ? AND attempt = ?""",
                (verification.delegation_id, verification.attempt),
            ).fetchone()
            receipt_row = connection.execute(
                """SELECT receipt_json FROM runtime_delegation_receipts
                WHERE delegation_id = ? AND attempt = ?""",
                (verification.delegation_id, verification.attempt),
            ).fetchone()
            if task_row is None or receipt_row is None:
                raise DelegationInboxConflict("receipt verification has no durable task and receipt")
            _assert_verification_matches_task(
                _delegation_task_from_row(task_row),
                DelegationReceipt.model_validate(_load_json(receipt_row["receipt_json"])),
                verification,
            )
            event = RuntimeEvent(
                run_id=verification.parent_run_id,
                seq=self._latest_seq(connection, verification.parent_run_id) + 1,
                event_id=(
                    f"delegation.verification:{verification.parent_run_id}:"
                    f"{verification.delegation_id}:{verification.attempt}"
                ),
                type=RuntimeEventType.DELEGATION_VERIFICATION,
                occurred_at=verification.verified_at,
                visibility=EventVisibility.DEBUG,
                payload={
                    "delegation_id": verification.delegation_id,
                    "attempt": verification.attempt,
                    "join_group": verification.join_group,
                    "revision": verification.revision,
                    "verdict": verification.verdict.value,
                    "reason_code": verification.reason_code,
                    "receipt_status": verification.receipt_status,
                    "evidence_digest": verification.evidence_digest,
                },
                trace_id=trace_id,
            )
            self._insert_event(connection, event)
            connection.execute(
                """INSERT INTO runtime_delegation_receipt_consumptions
                (parent_run_id, delegation_id, attempt, verification_json, verdict, reason_code,
                 evidence_digest, event_id, verified_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    verification.parent_run_id,
                    verification.delegation_id,
                    verification.attempt,
                    _dump_json(verification.model_dump(mode="json")),
                    verification.verdict.value,
                    verification.reason_code,
                    verification.evidence_digest,
                    event.event_id,
                    verification.verified_at.isoformat(),
                ),
            )
            return verification.model_copy(deep=True)

    def list_receipt_verifications(self, parent_run_id: str) -> List[DelegationReceiptVerification]:
        rows = self._fetch_all(
            """SELECT verification_json FROM runtime_delegation_receipt_consumptions
            WHERE parent_run_id = ? ORDER BY verified_at, delegation_id, attempt""",
            (parent_run_id,),
        )
        return [
            DelegationReceiptVerification.model_validate(_load_json(row["verification_json"]))
            for row in rows
        ]

    def commit_child_terminal(self, receipt: DelegationReceipt, terminal_event: RuntimeEvent) -> DelegationReceipt:
        """Atomically persist the unique receipt and the child terminal event."""

        with self._immediate_transaction() as connection:
            persisted = self._put_receipt(connection, receipt)
            existing = self._event_by_id(connection, terminal_event.event_id)
            if existing is not None:
                _assert_event_run(existing, terminal_event.run_id)
            else:
                expected_seq = self._latest_seq(connection, terminal_event.run_id) + 1
                if terminal_event.seq != expected_seq:
                    raise RuntimeSequenceError(
                        f"event seq must be {expected_seq}; got {terminal_event.seq}"
                    )
                self._insert_event(connection, terminal_event)
            return persisted

    def claim_parent_lease(
        self,
        parent_run_id: str,
        owner_id: str,
        *,
        now: datetime,
        ttl: timedelta,
    ) -> bool:
        expires_at = now + max(ttl, timedelta(milliseconds=1))
        with self._immediate_transaction() as connection:
            row = connection.execute(
                "SELECT owner_id, expires_at FROM runtime_delegation_leases WHERE parent_run_id = ?",
                (parent_run_id,),
            ).fetchone()
            if row is not None and str(row["owner_id"]) != owner_id and _parse_datetime(row["expires_at"]) > now:
                return False
            connection.execute(
                """INSERT INTO runtime_delegation_leases(parent_run_id, owner_id, expires_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(parent_run_id) DO UPDATE SET owner_id = excluded.owner_id,
                    expires_at = excluded.expires_at, updated_at = excluded.updated_at""",
                (parent_run_id, owner_id, expires_at.isoformat(), now.isoformat()),
            )
            return True

    def schedule_parent_scan(self, parent_run_id: str, *, next_scan_at: datetime, scan_attempt: int) -> None:
        self._execute_write(
            """UPDATE runtime_delegation_tasks
            SET next_scan_at = ?, scan_attempt = ?, updated_at = ?
            WHERE parent_run_id = ? AND status IN (?, ?, ?, ?)""",
            (
                next_scan_at.isoformat(),
                scan_attempt,
                next_scan_at.isoformat(),
                parent_run_id,
                DelegationTaskStatus.PLANNED.value,
                DelegationTaskStatus.DISPATCHED.value,
                DelegationTaskStatus.RUNNING.value,
                DelegationTaskStatus.RECEIPT_READY.value,
            ),
        )

    def cancel_parent(self, parent_run_id: str, *, now: datetime) -> None:
        self._set_parent_task_status(parent_run_id, DelegationTaskStatus.CANCELLED, now=now)

    def supersede_parent(self, parent_run_id: str, *, revision: int, now: datetime) -> None:
        self._execute_write(
            """UPDATE runtime_delegation_tasks SET status = ?, updated_at = ?
            WHERE parent_run_id = ? AND revision < ? AND status IN (?, ?, ?, ?)""",
            (
                DelegationTaskStatus.SUPERSEDED.value,
                now.isoformat(),
                parent_run_id,
                revision,
                DelegationTaskStatus.PLANNED.value,
                DelegationTaskStatus.DISPATCHED.value,
                DelegationTaskStatus.RUNNING.value,
                DelegationTaskStatus.RECEIPT_READY.value,
            ),
        )

    def commit_aggregate(
        self,
        aggregate: DelegationAggregate,
        *,
        continuation_id: str,
        trace_id: Optional[str] = None,
    ) -> Optional[DelegationContinuationRequest]:
        """Append one safe aggregate event and continuation marker atomically."""

        with self._immediate_transaction() as connection:
            row = connection.execute(
                """SELECT continuation_id, aggregate_json, created_at
                FROM runtime_delegation_consumptions
                WHERE parent_run_id = ? AND join_group = ? AND revision = ? AND kind = 'aggregate'""",
                (aggregate.parent_run_id, aggregate.join_group, aggregate.revision),
            ).fetchone()
            if row is not None:
                return None
            event = RuntimeEvent(
                run_id=aggregate.parent_run_id,
                seq=self._latest_seq(connection, aggregate.parent_run_id) + 1,
                event_id=f"delegation.aggregate:{aggregate.parent_run_id}:{aggregate.join_group}:{aggregate.revision}",
                type=RuntimeEventType.DELEGATION_AGGREGATE,
                occurred_at=aggregate.generated_at,
                visibility=EventVisibility.USER,
                payload=aggregate.model_dump(mode="json"),
                trace_id=trace_id,
            )
            self._insert_event(connection, event)
            connection.execute(
                """INSERT INTO runtime_delegation_consumptions
                (parent_run_id, join_group, revision, kind, continuation_id, aggregate_json, event_id, created_at)
                VALUES (?, ?, ?, 'aggregate', ?, ?, ?, ?)""",
                (
                    aggregate.parent_run_id,
                    aggregate.join_group,
                    aggregate.revision,
                    continuation_id,
                    _dump_json(aggregate.model_dump(mode="json")),
                    event.event_id,
                    aggregate.generated_at.isoformat(),
                ),
            )
            return DelegationContinuationRequest(
                continuation_id=continuation_id,
                parent_run_id=aggregate.parent_run_id,
                join_group=aggregate.join_group,
                revision=aggregate.revision,
                aggregate=aggregate.model_copy(deep=True),
                created_at=aggregate.generated_at,
            )

    def list_continuations(self, parent_run_id: str) -> List[DelegationContinuationRequest]:
        rows = self._fetch_all(
            """SELECT continuation_id, join_group, revision, aggregate_json, created_at
            FROM runtime_delegation_consumptions
            WHERE parent_run_id = ? AND kind = 'aggregate'
            ORDER BY revision, join_group""",
            (parent_run_id,),
        )
        return [
            DelegationContinuationRequest(
                continuation_id=str(row["continuation_id"]),
                parent_run_id=parent_run_id,
                join_group=str(row["join_group"]),
                revision=int(row["revision"]),
                aggregate=DelegationAggregate.model_validate(_load_json(row["aggregate_json"])),
                created_at=_parse_datetime(row["created_at"]),
            )
            for row in rows
        ]

    def commit_verification_aggregate(
        self,
        aggregate: DelegationVerificationAggregate,
        *,
        trace_id: Optional[str] = None,
    ) -> bool:
        with self._immediate_transaction() as connection:
            row = connection.execute(
                """SELECT 1 FROM runtime_delegation_consumptions
                WHERE parent_run_id = ? AND join_group = ? AND revision = ?
                  AND kind = 'verification_aggregate'""",
                (aggregate.parent_run_id, aggregate.join_group, aggregate.revision),
            ).fetchone()
            if row is not None:
                return False
            event = RuntimeEvent(
                run_id=aggregate.parent_run_id,
                seq=self._latest_seq(connection, aggregate.parent_run_id) + 1,
                event_id=(
                    f"delegation.verification_aggregate:{aggregate.parent_run_id}:"
                    f"{aggregate.join_group}:{aggregate.revision}"
                ),
                type=RuntimeEventType.DELEGATION_VERIFICATION,
                occurred_at=aggregate.generated_at,
                visibility=EventVisibility.USER,
                payload={
                    "join_group": aggregate.join_group,
                    "revision": aggregate.revision,
                    "status": aggregate.status.value,
                    "reason_code": aggregate.reason_code,
                    "required_count": aggregate.required_count,
                    "required_accepted_count": aggregate.required_accepted_count,
                    "accepted_count": aggregate.accepted_count,
                    "rejected_count": aggregate.rejected_count,
                    "rejection_reason_codes": list(aggregate.rejection_reason_codes),
                },
                trace_id=trace_id,
            )
            self._insert_event(connection, event)
            connection.execute(
                """INSERT INTO runtime_delegation_consumptions
                (parent_run_id, join_group, revision, kind, continuation_id, aggregate_json, event_id, created_at)
                VALUES (?, ?, ?, 'verification_aggregate', ?, ?, ?, ?)""",
                (
                    aggregate.parent_run_id,
                    aggregate.join_group,
                    aggregate.revision,
                    aggregate.continuation_id,
                    _dump_json(aggregate.model_dump(mode="json")),
                    event.event_id,
                    aggregate.generated_at.isoformat(),
                ),
            )
            return True

    def list_verification_aggregates(
        self,
        parent_run_id: str,
    ) -> List[DelegationVerificationAggregate]:
        rows = self._fetch_all(
            """SELECT aggregate_json FROM runtime_delegation_consumptions
            WHERE parent_run_id = ? AND kind = 'verification_aggregate'
            ORDER BY revision, join_group""",
            (parent_run_id,),
        )
        return [
            DelegationVerificationAggregate.model_validate(_load_json(row["aggregate_json"]))
            for row in rows
        ]

    def _configure(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = NORMAL")
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 5000")

    def _initialize_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_schema (
                    version INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runtime_runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    document_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runtime_turns (
                    turn_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    document_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runtime_runs(run_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS runtime_turns_run_status_idx
                ON runtime_turns(run_id, status);

                CREATE TABLE IF NOT EXISTS runtime_checkpoints (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    document_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runtime_crawl_jobs (
                    crawl_job_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    document_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS runtime_crawl_jobs_run_status_idx
                ON runtime_crawl_jobs(run_id, status);

                CREATE TABLE IF NOT EXISTS runtime_orchestration_states (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    document_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runtime_session_messages (
                    message_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    document_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS runtime_session_messages_session_idx
                ON runtime_session_messages(session_id, created_at, message_id);

                CREATE TABLE IF NOT EXISTS runtime_session_artifacts (
                    session_id TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    document_json TEXT NOT NULL,
                    PRIMARY KEY(session_id, artifact_id)
                );

                CREATE TABLE IF NOT EXISTS runtime_session_crawler_datasets (
                    session_id TEXT NOT NULL,
                    dataset_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    document_json TEXT NOT NULL,
                    PRIMARY KEY(session_id, dataset_id)
                );
                CREATE INDEX IF NOT EXISTS runtime_session_crawler_datasets_session_idx
                ON runtime_session_crawler_datasets(session_id, created_at, dataset_id);

                CREATE TABLE IF NOT EXISTS runtime_pinned_constraints (
                    constraint_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    document_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runtime_compactions (
                    compaction_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    document_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runtime_events (
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    document_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, seq)
                );
                """
            )
            row = self._connection.execute(
                "SELECT version FROM runtime_schema LIMIT 1"
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO runtime_schema(version) VALUES (?)",
                    (1,),
                )
                version = 1
            else:
                version = int(row["version"])
            if version > SQLITE_SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported Runtime SQLite schema version: {version}"
                )
            while version < SQLITE_SCHEMA_VERSION:
                if version == 1:
                    self._migrate_v1_to_v2(self._connection)
                    version = 2
                    self._connection.execute("UPDATE runtime_schema SET version = ?", (version,))
                    continue
                if version == 2:
                    self._migrate_v2_to_v3(self._connection)
                    version = 3
                    self._connection.execute("UPDATE runtime_schema SET version = ?", (version,))
                    continue
                if version == 3:
                    self._migrate_v3_to_v4(self._connection)
                    version = 4
                    self._connection.execute("UPDATE runtime_schema SET version = ?", (version,))
                    continue
                if version == 4:
                    self._migrate_v4_to_v5(self._connection)
                    version = 5
                    self._connection.execute("UPDATE runtime_schema SET version = ?", (version,))
                    continue
                if version == 5:
                    self._migrate_v5_to_v6(self._connection)
                    version = 6
                    self._connection.execute("UPDATE runtime_schema SET version = ?", (version,))
                    continue
                if version == 6:
                    self._migrate_v6_to_v7(self._connection)
                    version = 7
                    self._connection.execute("UPDATE runtime_schema SET version = ?", (version,))
                    continue
                if version == 7:
                    self._migrate_v7_to_v8(self._connection)
                    version = 8
                    self._connection.execute("UPDATE runtime_schema SET version = ?", (version,))
                    continue
                if version == 8:
                    self._migrate_v8_to_v9(self._connection)
                    version = 9
                    self._connection.execute("UPDATE runtime_schema SET version = ?", (version,))
                    continue
                if version == 9:
                    self._migrate_v9_to_v10(self._connection)
                    version = 10
                    self._connection.execute("UPDATE runtime_schema SET version = ?", (version,))
                    continue
                raise RuntimeError(f"missing Runtime SQLite migration from version: {version}")

    @staticmethod
    def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
        """Add the durable delegation Inbox without rewriting v1 Runtime facts."""

        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runtime_delegation_tasks (
                delegation_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                parent_run_id TEXT NOT NULL,
                parent_step_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                join_group TEXT NOT NULL,
                result_policy TEXT NOT NULL,
                revision INTEGER NOT NULL,
                status TEXT NOT NULL,
                child_run_id TEXT NOT NULL,
                child_session_id TEXT NOT NULL,
                checkpoint_key TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                deadline_at TEXT NOT NULL,
                scan_attempt INTEGER NOT NULL DEFAULT 0,
                next_scan_at TEXT,
                spec_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(delegation_id, attempt),
                UNIQUE(parent_run_id, idempotency_key)
            );
            CREATE INDEX IF NOT EXISTS runtime_delegation_tasks_parent_scan_idx
            ON runtime_delegation_tasks(parent_run_id, status, next_scan_at);

            CREATE TABLE IF NOT EXISTS runtime_delegation_receipts (
                delegation_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                parent_run_id TEXT NOT NULL,
                receipt_json TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                PRIMARY KEY(delegation_id, attempt),
                FOREIGN KEY(delegation_id, attempt)
                    REFERENCES runtime_delegation_tasks(delegation_id, attempt)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS runtime_delegation_receipts_parent_idx
            ON runtime_delegation_receipts(parent_run_id, completed_at);

            CREATE TABLE IF NOT EXISTS runtime_delegation_consumptions (
                parent_run_id TEXT NOT NULL,
                join_group TEXT NOT NULL,
                revision INTEGER NOT NULL,
                kind TEXT NOT NULL,
                continuation_id TEXT NOT NULL,
                aggregate_json TEXT NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                PRIMARY KEY(parent_run_id, join_group, revision, kind)
            );

            CREATE TABLE IF NOT EXISTS runtime_delegation_leases (
                parent_run_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

    @staticmethod
    def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
        """Add receipt-attempt consumption without rewriting durable Inbox facts."""

        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runtime_delegation_receipt_consumptions (
                parent_run_id TEXT NOT NULL,
                delegation_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                verification_json TEXT NOT NULL,
                verdict TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                evidence_digest TEXT NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                verified_at TEXT NOT NULL,
                PRIMARY KEY(parent_run_id, delegation_id, attempt)
            );
            CREATE INDEX IF NOT EXISTS runtime_delegation_receipt_consumptions_parent_idx
            ON runtime_delegation_receipt_consumptions(parent_run_id, verified_at);
            """
        )

    @staticmethod
    def _migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
        """Add immutable crawler dataset snapshots and Session current pins."""

        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runtime_crawler_dataset_versions (
                dataset_version_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                dataset_id TEXT NOT NULL,
                source_run_id TEXT NOT NULL,
                parent_dataset_version_id TEXT,
                created_at TEXT NOT NULL,
                document_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS runtime_crawler_dataset_versions_dataset_idx
            ON runtime_crawler_dataset_versions(session_id, dataset_id, created_at, dataset_version_id);

            CREATE TABLE IF NOT EXISTS runtime_session_crawler_dataset_links (
                session_id TEXT NOT NULL,
                dataset_id TEXT NOT NULL,
                dataset_version_id TEXT NOT NULL,
                pinned_at TEXT NOT NULL,
                document_json TEXT NOT NULL,
                PRIMARY KEY(session_id, dataset_id),
                FOREIGN KEY(dataset_version_id)
                    REFERENCES runtime_crawler_dataset_versions(dataset_version_id)
            );
            CREATE INDEX IF NOT EXISTS runtime_session_crawler_dataset_links_version_idx
            ON runtime_session_crawler_dataset_links(dataset_version_id);
            """
        )

    @staticmethod
    def _migrate_v4_to_v5(connection: sqlite3.Connection) -> None:
        """Keep DatasetVersion projection durable when the Desktop is offline."""

        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runtime_crawler_dataset_sync_outbox (
                dataset_version_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                dataset_id TEXT NOT NULL,
                document_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS runtime_crawler_dataset_sync_outbox_created_idx
            ON runtime_crawler_dataset_sync_outbox(created_at, dataset_version_id);
            """
        )

    @staticmethod
    def _migrate_v5_to_v6(connection: sqlite3.Connection) -> None:
        """Add Runtime-owned task/context facts without rewriting legacy Sessions."""

        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runtime_tasks (
                task_id TEXT NOT NULL UNIQUE,
                conversation_id TEXT PRIMARY KEY,
                updated_at TEXT NOT NULL,
                document_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runtime_context_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                document_json TEXT NOT NULL,
                UNIQUE(conversation_id, revision)
            );
            CREATE INDEX IF NOT EXISTS runtime_context_snapshots_task_idx
            ON runtime_context_snapshots(task_id, revision);
            CREATE INDEX IF NOT EXISTS runtime_context_snapshots_run_turn_idx
            ON runtime_context_snapshots(run_id, turn_id);
            """
        )

    @staticmethod
    def _migrate_v6_to_v7(connection: sqlite3.Connection) -> None:
        """Add a common local asset catalog without rewriting legacy sources."""

        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runtime_asset_versions (
                asset_version_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                asset_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                parent_asset_version_id TEXT,
                source_kind TEXT NOT NULL,
                source_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                document_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS runtime_asset_versions_session_asset_idx
            ON runtime_asset_versions(session_id, asset_id, version, asset_version_id);

            CREATE TABLE IF NOT EXISTS runtime_session_asset_links (
                session_id TEXT NOT NULL,
                asset_id TEXT NOT NULL,
                asset_version_id TEXT NOT NULL,
                pinned_at TEXT NOT NULL,
                PRIMARY KEY(session_id, asset_id),
                FOREIGN KEY(asset_version_id) REFERENCES runtime_asset_versions(asset_version_id)
            );
            CREATE INDEX IF NOT EXISTS runtime_session_asset_links_version_idx
            ON runtime_session_asset_links(asset_version_id);
            """
        )

    @staticmethod
    def _migrate_v7_to_v8(connection: sqlite3.Connection) -> None:
        """Add local adapter control-plane state without adding an event outbox."""

        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runtime_plugins (
                plugin_id TEXT NOT NULL,
                installation_id TEXT NOT NULL,
                runtime TEXT NOT NULL,
                contract_version TEXT NOT NULL,
                enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
                capabilities_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(plugin_id, installation_id),
                UNIQUE(installation_id)
            );

            CREATE TABLE IF NOT EXISTS runtime_installations (
                installation_id TEXT PRIMARY KEY,
                plugin_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('paired', 'unpaired')),
                user_id TEXT,
                tenant_id TEXT,
                paired_at TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(plugin_id, installation_id)
                    REFERENCES runtime_plugins(plugin_id, installation_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS runtime_pairing_challenges (
                challenge_digest TEXT PRIMARY KEY,
                installation_id TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                consumed_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(installation_id) REFERENCES runtime_installations(installation_id)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS runtime_pairing_challenges_installation_idx
            ON runtime_pairing_challenges(installation_id, expires_at);

            CREATE TABLE IF NOT EXISTS runtime_adapter_cursors (
                installation_id TEXT NOT NULL,
                source_path TEXT NOT NULL,
                file_identity TEXT NOT NULL,
                offset INTEGER NOT NULL CHECK(offset >= 0),
                updated_at TEXT NOT NULL,
                PRIMARY KEY(installation_id, source_path),
                FOREIGN KEY(installation_id) REFERENCES runtime_installations(installation_id)
                    ON DELETE CASCADE
            );
            """
        )

    @staticmethod
    def _migrate_v8_to_v9(connection: sqlite3.Connection) -> None:
        """Add the durable telemetry outbox with lease state."""

        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runtime_telemetry_outbox (
                event_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                payload_hash TEXT NOT NULL,
                event_json TEXT NOT NULL,
                priority TEXT NOT NULL CHECK(priority IN ('progress', 'terminal', 'feedback')),
                status TEXT NOT NULL CHECK(status IN ('pending', 'leased', 'delivered', 'dead_letter')),
                attempt_count INTEGER NOT NULL CHECK(attempt_count >= 0),
                next_attempt_at TEXT NOT NULL,
                lease_owner TEXT,
                lease_expires_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_error_code TEXT
            );
            CREATE INDEX IF NOT EXISTS runtime_telemetry_outbox_ready_idx
            ON runtime_telemetry_outbox(status, next_attempt_at, created_at, event_id);
            """
        )

    @staticmethod
    def _migrate_v9_to_v10(connection: sqlite3.Connection) -> None:
        """Persist one stable opaque producer instance per local Runtime DB."""

        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runtime_telemetry_producer (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                instance_pseudonym TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            """
        )

    @staticmethod
    def _asset_parent_version(asset: CatalogAssetRef) -> str | None:
        return asset.provenance.parent_asset_version_id

    def _register_asset_in_transaction(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        asset: CatalogAssetRef,
        *,
        source: dict[str, Any],
    ) -> CatalogAssetRef:
        existing = connection.execute(
            "SELECT document_json FROM runtime_asset_versions WHERE asset_version_id = ?",
            (asset.asset_version_id,),
        ).fetchone()
        if existing is not None:
            persisted = CatalogAssetRef.model_validate(_load_json(existing["document_json"]))
        else:
            previous = connection.execute(
                """SELECT versions.version FROM runtime_session_asset_links AS links
                JOIN runtime_asset_versions AS versions ON versions.asset_version_id = links.asset_version_id
                WHERE links.session_id = ? AND links.asset_id = ?""",
                (session_id, asset.asset_id),
            ).fetchone()
            persisted = asset.model_copy(
                update={"version": int(previous["version"]) + 1 if previous else 1},
                deep=True,
            )
            connection.execute(
                """INSERT INTO runtime_asset_versions
                (asset_version_id, session_id, asset_id, version, parent_asset_version_id, source_kind, source_json, created_at, document_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    persisted.asset_version_id,
                    session_id,
                    persisted.asset_id,
                    persisted.version,
                    self._asset_parent_version(persisted),
                    str(source.get("source_kind") or persisted.kind),
                    _dump_json(source),
                    persisted.created_at.isoformat(),
                    _dump_json(persisted.model_dump(mode="json")),
                ),
            )
        connection.execute(
            """INSERT INTO runtime_session_asset_links(session_id, asset_id, asset_version_id, pinned_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(session_id, asset_id) DO UPDATE SET
                asset_version_id = excluded.asset_version_id,
                pinned_at = excluded.pinned_at""",
            (session_id, persisted.asset_id, persisted.asset_version_id, utc_now().isoformat()),
        )
        return persisted.model_copy(deep=True)

    def _execute_write(self, query: str, parameters: Sequence[object]) -> None:
        with self._lock:
            self._ensure_open()
            with self._connection:
                self._connection.execute(query, parameters)

    def _execute_write_returning_count(self, query: str, parameters: Sequence[object]) -> int:
        with self._lock:
            self._ensure_open()
            with self._connection:
                return int(self._connection.execute(query, parameters).rowcount)

    def _fetch_one(
        self,
        query: str,
        parameters: Sequence[object],
    ) -> Optional[sqlite3.Row]:
        with self._lock:
            self._ensure_open()
            return self._connection.execute(query, parameters).fetchone()

    def _fetch_all(
        self,
        query: str,
        parameters: Sequence[object],
    ) -> List[sqlite3.Row]:
        with self._lock:
            self._ensure_open()
            return list(self._connection.execute(query, parameters).fetchall())

    def _immediate_transaction(self) -> "_ImmediateTransaction":
        return _ImmediateTransaction(self)

    def _event_by_id(
        self,
        connection: sqlite3.Connection,
        event_id: str,
    ) -> Optional[RuntimeEvent]:
        row = connection.execute(
            "SELECT document_json FROM runtime_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        return RuntimeEvent.from_json_dict(_load_json(row["document_json"]))

    def _latest_seq(self, connection: sqlite3.Connection, run_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) AS latest_seq FROM runtime_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return int(row["latest_seq"] if row else 0)

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        event: RuntimeEvent,
    ) -> None:
        connection.execute(
            """
            INSERT INTO runtime_events(run_id, seq, event_id, document_json, occurred_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                event.run_id,
                event.seq,
                event.event_id,
                _dump_json(event.to_json_dict()),
                event.occurred_at.isoformat(),
            ),
        )

    def _insert_delegation_task(self, connection: sqlite3.Connection, task: DelegationTask) -> None:
        spec = task.spec
        connection.execute(
            """INSERT INTO runtime_delegation_tasks
            (delegation_id, attempt, parent_run_id, parent_step_id, idempotency_key,
             join_group, result_policy, revision, status, child_run_id, child_session_id,
             checkpoint_key, trace_id, deadline_at, scan_attempt, next_scan_at, spec_json,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                spec.delegation_id,
                spec.attempt,
                spec.parent_run_id,
                spec.parent_step_id,
                spec.idempotency_key,
                spec.join_group,
                spec.result_policy,
                task.revision,
                task.status.value,
                task.child.child_run_id,
                task.child.child_session_id,
                task.child.checkpoint_key,
                task.child.trace_id,
                spec.budget.deadline_at.isoformat(),
                task.scan_attempt,
                task.next_scan_at.isoformat() if task.next_scan_at else None,
                _dump_json(spec.model_dump(mode="json")),
                task.created_at.isoformat(),
                task.updated_at.isoformat(),
            ),
        )

    def _put_receipt(self, connection: sqlite3.Connection, receipt: DelegationReceipt) -> DelegationReceipt:
        existing = connection.execute(
            """SELECT receipt_json FROM runtime_delegation_receipts
            WHERE delegation_id = ? AND attempt = ?""",
            (receipt.delegation_id, receipt.attempt),
        ).fetchone()
        if existing is not None:
            persisted = DelegationReceipt.model_validate(_load_json(existing["receipt_json"]))
            _assert_receipt_matches(persisted, receipt)
            return persisted
        task_row = connection.execute(
            """SELECT * FROM runtime_delegation_tasks WHERE delegation_id = ? AND attempt = ?""",
            (receipt.delegation_id, receipt.attempt),
        ).fetchone()
        if task_row is None:
            raise DelegationInboxConflict("receipt has no durable delegation task")
        task = _delegation_task_from_row(task_row)
        _assert_receipt_matches_task(receipt, task)
        connection.execute(
            """INSERT INTO runtime_delegation_receipts
            (delegation_id, attempt, parent_run_id, receipt_json, completed_at)
            VALUES (?, ?, ?, ?, ?)""",
            (
                receipt.delegation_id,
                receipt.attempt,
                receipt.parent_run_id,
                _dump_json(receipt.model_dump(mode="json")),
                receipt.completed_at.isoformat(),
            ),
        )
        connection.execute(
            """UPDATE runtime_delegation_tasks SET status = ?, updated_at = ?, next_scan_at = ?
            WHERE delegation_id = ? AND attempt = ? AND status NOT IN (?, ?)""",
            (
                DelegationTaskStatus.RECEIPT_READY.value,
                receipt.completed_at.isoformat(),
                receipt.completed_at.isoformat(),
                receipt.delegation_id,
                receipt.attempt,
                DelegationTaskStatus.CANCELLED.value,
                DelegationTaskStatus.SUPERSEDED.value,
            ),
        )
        return receipt.model_copy(deep=True)

    def _set_parent_task_status(
        self,
        parent_run_id: str,
        status: DelegationTaskStatus,
        *,
        now: datetime,
    ) -> None:
        self._execute_write(
            """UPDATE runtime_delegation_tasks SET status = ?, updated_at = ?
            WHERE parent_run_id = ? AND status IN (?, ?, ?, ?)""",
            (
                status.value,
                now.isoformat(),
                parent_run_id,
                DelegationTaskStatus.PLANNED.value,
                DelegationTaskStatus.DISPATCHED.value,
                DelegationTaskStatus.RUNNING.value,
                DelegationTaskStatus.RECEIPT_READY.value,
            ),
        )

    def _upsert_run(self, connection: sqlite3.Connection, run: Run) -> None:
        connection.execute(
            """
            INSERT INTO runtime_runs(run_id, status, document_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                status = excluded.status,
                document_json = excluded.document_json,
                updated_at = excluded.updated_at
            """,
            (
                run.run_id,
                run.status.value,
                _dump_json(run.model_dump(mode="json")),
                run.updated_at.isoformat(),
            ),
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Runtime SQLite store is closed")


class _ImmediateTransaction:
    def __init__(self, store: SQLiteRuntimeStore) -> None:
        self.store = store

    def __enter__(self) -> sqlite3.Connection:
        self.store._lock.acquire()
        try:
            self.store._ensure_open()
            self.store._connection.execute("BEGIN IMMEDIATE")
            return self.store._connection
        except Exception:
            self.store._lock.release()
            raise

    def __exit__(self, error_type, error, traceback) -> bool:
        try:
            if error_type is None:
                self.store._connection.commit()
            else:
                self.store._connection.rollback()
        finally:
            self.store._lock.release()
        return False


def _orchestration_state_to_dict(state: "OrchestrationState") -> dict:
    return {
        "plan_id": state.plan_id,
        "run_id": state.run_id,
        "status": state.status.value,
        "current_index": state.current_index,
        "step_statuses": {key: value.value for key, value in state.step_statuses.items()},
        "attempts": state.attempts,
        "outputs": state.outputs,
        "blocked_step_id": state.blocked_step_id,
        "trace_id": state.trace_id,
        "revision_history": state.revision_history,
        "child_run_ids": state.child_run_ids,
        "child_run_limit": state.child_run_limit,
        "delegation_revision": state.delegation_revision,
        "delegation_groups": state.delegation_groups,
        "execution_elapsed_seconds": state.execution_elapsed_seconds,
    }


def _delegation_task_from_row(row: sqlite3.Row) -> DelegationTask:
    return DelegationTask(
        spec=DelegationSpec.model_validate(_load_json(row["spec_json"])),
        child=DelegationChildIdentity(
            child_run_id=str(row["child_run_id"]),
            child_session_id=str(row["child_session_id"]),
            checkpoint_key=str(row["checkpoint_key"]),
            trace_id=str(row["trace_id"]),
        ),
        revision=int(row["revision"]),
        status=DelegationTaskStatus(str(row["status"])),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
        scan_attempt=int(row["scan_attempt"] or 0),
        next_scan_at=_parse_datetime(row["next_scan_at"]) if row["next_scan_at"] else None,
    )


def _parse_datetime(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("persisted Runtime timestamp must include timezone")
    return parsed


def _adapter_plugin_from_row(row: sqlite3.Row) -> PluginManifestV1:
    capabilities = json.loads(str(row["capabilities_json"]))
    if not isinstance(capabilities, list):
        raise ValueError("stored adapter capabilities must be a list")
    return PluginManifestV1(
        plugin_id=str(row["plugin_id"]),
        runtime=str(row["runtime"]),
        contract_version=str(row["contract_version"]),
        installation_id=str(row["installation_id"]),
        enabled=bool(row["enabled"]),
        capabilities=[str(value) for value in capabilities],
    )


def _telemetry_outbox_item_from_row(row: sqlite3.Row) -> TelemetryOutboxItem:
    event = _load_json(str(row["event_json"]))
    lease_expires_at = row["lease_expires_at"]
    return TelemetryOutboxItem(
        event_id=str(row["event_id"]),
        idempotency_key=str(row["idempotency_key"]),
        payload_hash=str(row["payload_hash"]),
        event=event,
        priority=str(row["priority"]),
        status=str(row["status"]),
        attempt_count=int(row["attempt_count"]),
        next_attempt_at=_parse_datetime(row["next_attempt_at"]),
        lease_owner=str(row["lease_owner"]) if row["lease_owner"] is not None else None,
        lease_expires_at=_parse_datetime(lease_expires_at) if lease_expires_at is not None else None,
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _nonnegative_float(value: object) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _orchestration_state_from_dict(data: dict) -> "OrchestrationState":
    from cowork_runtime.orchestration.types import (
        OrchestrationState,
        OrchestrationStatus,
        PlanStepStatus,
    )

    return OrchestrationState(
        plan_id=str(data["plan_id"]),
        run_id=str(data["run_id"]),
        status=OrchestrationStatus(data.get("status", OrchestrationStatus.PENDING.value)),
        current_index=int(data.get("current_index", 0)),
        step_statuses={
            str(key): PlanStepStatus(value)
            for key, value in dict(data.get("step_statuses") or {}).items()
        },
        attempts={str(key): int(value) for key, value in dict(data.get("attempts") or {}).items()},
        outputs={str(key): dict(value) for key, value in dict(data.get("outputs") or {}).items()},
        blocked_step_id=(str(data["blocked_step_id"]) if data.get("blocked_step_id") else None),
        trace_id=(str(data["trace_id"]) if data.get("trace_id") else None),
        revision_history=[dict(item) for item in list(data.get("revision_history") or [])],
        child_run_ids=[str(item) for item in list(data.get("child_run_ids") or [])],
        child_run_limit=int(data.get("child_run_limit", 0) or 0),
        delegation_revision=int(data.get("delegation_revision", 0) or 0),
        delegation_groups={
            str(key): dict(value)
            for key, value in dict(data.get("delegation_groups") or {}).items()
            if isinstance(value, dict)
        },
        execution_elapsed_seconds=_nonnegative_float(data.get("execution_elapsed_seconds", 0.0)),
    )


def _enum_values(values: Optional[Iterable[object]]) -> List[str]:
    if values is None:
        return []
    return [str(getattr(value, "value", value)) for value in values]


def _placeholders(values: Sequence[object]) -> str:
    return ",".join("?" for _ in values)


def _dump_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _load_json(value: str) -> dict:
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise ValueError("Runtime SQLite document must be a JSON object")
    return loaded


def _require_crawl_job_id(checkpoint: dict[str, Any]) -> str:
    crawl_job_id = str(checkpoint.get("crawl_job_id") or "").strip()
    if not crawl_job_id:
        raise ValueError("crawl job checkpoint requires crawl_job_id")
    return crawl_job_id


def _assert_event_run(event: RuntimeEvent, run_id: str) -> None:
    if event.run_id != run_id:
        raise RuntimeSequenceError(
            f"event id {event.event_id!r} already belongs to run {event.run_id!r}"
        )
