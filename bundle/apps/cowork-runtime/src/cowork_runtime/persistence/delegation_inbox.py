"""Durable, runtime-neutral delegation Inbox facts.

The Inbox intentionally stores only contracts and lifecycle metadata. It never
interprets a child receipt as a verified Parent fact; ISSUE-065 owns that
decision. SQLite and the future Server adapter share this small port.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from threading import RLock
from typing import Dict, List, Protocol

from cowork_contracts import (
    DelegationAggregate,
    DelegationReceipt,
    DelegationReceiptVerification,
    DelegationSpec,
    DelegationTaskStatus,
    DelegationUsage,
    DelegationVerificationAggregate,
    delegation_objective_hash,
    delegation_spec_hash,
)
from cowork_runtime.contracts import RuntimeEvent


@dataclass(frozen=True)
class DelegationChildIdentity:
    child_run_id: str
    child_session_id: str
    checkpoint_key: str
    trace_id: str


@dataclass(frozen=True)
class DelegationTask:
    spec: DelegationSpec
    child: DelegationChildIdentity
    revision: int
    status: DelegationTaskStatus
    created_at: datetime
    updated_at: datetime
    scan_attempt: int = 0
    next_scan_at: datetime | None = None
    # Server persistence records the immutable Parent plan that authorized a
    # child. Local v1 tasks predate that audit field, so it remains optional at
    # the runtime-neutral port boundary.
    plan_id: str | None = None
    plan_fingerprint: str | None = None
    authorization_fingerprint: str | None = None


@dataclass(frozen=True)
class DelegationContinuationRequest:
    continuation_id: str
    parent_run_id: str
    join_group: str
    revision: int
    aggregate: DelegationAggregate
    created_at: datetime


class DelegationInboxConflict(ValueError):
    """A retry reused an idempotency identity with different immutable facts."""


class DelegationInbox(Protocol):
    def ensure_task(
        self,
        spec: DelegationSpec,
        child: DelegationChildIdentity,
        *,
        revision: int = 0,
        now: datetime,
    ) -> DelegationTask:
        ...

    def get_task(self, delegation_id: str, attempt: int) -> DelegationTask | None:
        ...

    def claim_task_launch(self, delegation_id: str, attempt: int, *, now: datetime) -> bool:
        """Atomically grant the one process allowed to start this child."""
        ...

    def list_parent_tasks(self, parent_run_id: str) -> List[DelegationTask]:
        ...

    def list_parent_run_ids(self) -> List[str]:
        """Return durable Parent ids for reconciliation workers.

        This is intentionally an Inbox lookup rather than an in-memory wakeup:
        a restart must rediscover receipts and verification aggregates without
        relying on a previously delivered notification.
        """
        ...

    def list_due_parent_run_ids(self, now: datetime) -> List[str]:
        ...

    def get_receipt(self, delegation_id: str, attempt: int) -> DelegationReceipt | None:
        ...

    def put_receipt(self, receipt: DelegationReceipt) -> DelegationReceipt:
        ...

    def expire_task(self, delegation_id: str, attempt: int, *, now: datetime) -> DelegationReceipt | None:
        """Commit an expired receipt and terminal trace fact when its deadline passes."""
        ...

    def list_parent_receipts(self, parent_run_id: str) -> List[DelegationReceipt]:
        ...

    def get_receipt_verification(
        self,
        parent_run_id: str,
        delegation_id: str,
        attempt: int,
    ) -> DelegationReceiptVerification | None:
        ...

    def commit_receipt_verification(
        self,
        verification: DelegationReceiptVerification,
        *,
        trace_id: str | None = None,
    ) -> DelegationReceiptVerification:
        """Persist exactly one immutable accept/reject verdict per attempt."""
        ...

    def list_receipt_verifications(self, parent_run_id: str) -> List[DelegationReceiptVerification]:
        ...

    def commit_child_terminal(
        self,
        receipt: DelegationReceipt,
        terminal_event: RuntimeEvent,
    ) -> DelegationReceipt:
        ...

    def claim_parent_lease(
        self,
        parent_run_id: str,
        owner_id: str,
        *,
        now: datetime,
        ttl: timedelta,
    ) -> bool:
        ...

    def schedule_parent_scan(self, parent_run_id: str, *, next_scan_at: datetime, scan_attempt: int) -> None:
        ...

    def cancel_parent(self, parent_run_id: str, *, now: datetime) -> None:
        ...

    def supersede_parent(self, parent_run_id: str, *, revision: int, now: datetime) -> None:
        ...

    def commit_aggregate(
        self,
        aggregate: DelegationAggregate,
        *,
        continuation_id: str,
        trace_id: str | None = None,
    ) -> DelegationContinuationRequest | None:
        ...

    def list_continuations(self, parent_run_id: str) -> List[DelegationContinuationRequest]:
        ...

    def commit_verification_aggregate(
        self,
        aggregate: DelegationVerificationAggregate,
        *,
        trace_id: str | None = None,
    ) -> bool:
        ...

    def list_verification_aggregates(
        self,
        parent_run_id: str,
    ) -> List[DelegationVerificationAggregate]:
        """Return durable, Parent-safe aggregation facts for recovery reads."""
        ...


class InMemoryDelegationInbox:
    """Thread-safe deterministic adapter for scheduler and runner tests."""

    def __init__(self) -> None:
        self._tasks: Dict[tuple[str, int], DelegationTask] = {}
        self._task_keys: Dict[tuple[str, str], tuple[str, int]] = {}
        self._receipts: Dict[tuple[str, int], DelegationReceipt] = {}
        self._receipt_verifications: Dict[tuple[str, str, int], DelegationReceiptVerification] = {}
        self._leases: Dict[str, tuple[str, datetime]] = {}
        self._continuations: Dict[tuple[str, str, int], DelegationContinuationRequest] = {}
        self._verification_aggregates: Dict[tuple[str, str, int], DelegationVerificationAggregate] = {}
        self._events: Dict[str, RuntimeEvent] = {}
        self._lock = RLock()

    def ensure_task(self, spec: DelegationSpec, child: DelegationChildIdentity, *, revision: int = 0, now: datetime) -> DelegationTask:
        key = (spec.delegation_id, spec.attempt)
        parent_key = (spec.parent_run_id, spec.idempotency_key)
        with self._lock:
            existing_key = self._task_keys.get(parent_key)
            if existing_key is not None:
                existing = self._tasks[existing_key]
                _assert_task_matches(existing, spec, child, revision)
                return existing
            existing = self._tasks.get(key)
            if existing is not None:
                _assert_task_matches(existing, spec, child, revision)
                return existing
            if len([task for task in self._tasks.values() if task.spec.parent_run_id == spec.parent_run_id]) >= 3:
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
            self._tasks[key] = task
            self._task_keys[parent_key] = key
            return task

    def get_task(self, delegation_id: str, attempt: int) -> DelegationTask | None:
        with self._lock:
            return self._tasks.get((delegation_id, attempt))

    def claim_task_launch(self, delegation_id: str, attempt: int, *, now: datetime) -> bool:
        key = (delegation_id, attempt)
        with self._lock:
            task = self._tasks.get(key)
            if task is None or task.status is not DelegationTaskStatus.PLANNED:
                return False
            self._tasks[key] = replace(task, status=DelegationTaskStatus.RUNNING, updated_at=now)
            return True

    def list_parent_tasks(self, parent_run_id: str) -> List[DelegationTask]:
        with self._lock:
            return sorted(
                (task for task in self._tasks.values() if task.spec.parent_run_id == parent_run_id),
                key=lambda task: (task.spec.join_group, task.spec.delegation_id, task.spec.attempt),
            )

    def list_parent_run_ids(self) -> List[str]:
        with self._lock:
            return sorted({task.spec.parent_run_id for task in self._tasks.values()})

    def list_due_parent_run_ids(self, now: datetime) -> List[str]:
        with self._lock:
            return sorted({
                task.spec.parent_run_id
                for task in self._tasks.values()
                if task.status in {DelegationTaskStatus.PLANNED, DelegationTaskStatus.DISPATCHED, DelegationTaskStatus.RUNNING, DelegationTaskStatus.RECEIPT_READY}
                and (task.next_scan_at is None or task.next_scan_at <= now)
                and (task.spec.parent_run_id, task.spec.join_group, task.revision) not in self._continuations
            })

    def get_receipt(self, delegation_id: str, attempt: int) -> DelegationReceipt | None:
        with self._lock:
            receipt = self._receipts.get((delegation_id, attempt))
            return receipt.model_copy(deep=True) if receipt is not None else None

    def put_receipt(self, receipt: DelegationReceipt) -> DelegationReceipt:
        key = (receipt.delegation_id, receipt.attempt)
        with self._lock:
            existing = self._receipts.get(key)
            if existing is not None:
                _assert_receipt_matches(existing, receipt)
                return existing.model_copy(deep=True)
            task = self._tasks.get(key)
            if task is None:
                raise DelegationInboxConflict("receipt has no durable delegation task")
            _assert_receipt_matches_task(receipt, task)
            self._receipts[key] = receipt.model_copy(deep=True)
            if task.status not in {DelegationTaskStatus.CANCELLED, DelegationTaskStatus.SUPERSEDED}:
                self._tasks[key] = replace(
                    task,
                    status=DelegationTaskStatus.RECEIPT_READY,
                    updated_at=receipt.completed_at,
                    next_scan_at=receipt.completed_at,
                )
            return receipt.model_copy(deep=True)

    def expire_task(self, delegation_id: str, attempt: int, *, now: datetime) -> DelegationReceipt | None:
        key = (delegation_id, attempt)
        with self._lock:
            existing = self._receipts.get(key)
            if existing is not None:
                return existing.model_copy(deep=True)
            task = self._tasks.get(key)
            if (
                task is None
                or task.status in {DelegationTaskStatus.CANCELLED, DelegationTaskStatus.SUPERSEDED}
                or now < task.spec.budget.deadline_at
            ):
                return None
            receipt = _expired_receipt_for_task(task, now)
            persisted = self.put_receipt(receipt)
            self._events.setdefault(
                f"delegation.receipt:{receipt.delegation_id}:{receipt.attempt}",
                RuntimeEvent(
                    run_id=task.child.child_run_id,
                    seq=1,
                    event_id=f"delegation.receipt:{receipt.delegation_id}:{receipt.attempt}",
                    type="run.blocked",
                    occurred_at=now,
                    visibility="debug",
                    payload={"status": receipt.status, "reason_code": receipt.reason_code},
                    trace_id=task.child.trace_id,
                ),
            )
            return persisted

    def list_parent_receipts(self, parent_run_id: str) -> List[DelegationReceipt]:
        with self._lock:
            return sorted(
                (receipt.model_copy(deep=True) for receipt in self._receipts.values() if receipt.parent_run_id == parent_run_id),
                key=lambda receipt: (receipt.delegation_id, receipt.attempt),
            )

    def get_receipt_verification(
        self,
        parent_run_id: str,
        delegation_id: str,
        attempt: int,
    ) -> DelegationReceiptVerification | None:
        with self._lock:
            item = self._receipt_verifications.get((parent_run_id, delegation_id, attempt))
            return item.model_copy(deep=True) if item is not None else None

    def commit_receipt_verification(
        self,
        verification: DelegationReceiptVerification,
        *,
        trace_id: str | None = None,
    ) -> DelegationReceiptVerification:
        del trace_id
        key = (verification.parent_run_id, verification.delegation_id, verification.attempt)
        with self._lock:
            existing = self._receipt_verifications.get(key)
            if existing is not None:
                _assert_verification_matches(existing, verification)
                return existing.model_copy(deep=True)
            task = self._tasks.get((verification.delegation_id, verification.attempt))
            receipt = self._receipts.get((verification.delegation_id, verification.attempt))
            if task is None or receipt is None:
                raise DelegationInboxConflict("receipt verification has no durable task and receipt")
            _assert_verification_matches_task(task, receipt, verification)
            self._receipt_verifications[key] = verification.model_copy(deep=True)
            return verification.model_copy(deep=True)

    def list_receipt_verifications(self, parent_run_id: str) -> List[DelegationReceiptVerification]:
        with self._lock:
            return sorted(
                (
                    item.model_copy(deep=True)
                    for item in self._receipt_verifications.values()
                    if item.parent_run_id == parent_run_id
                ),
                key=lambda item: (item.join_group, item.revision, item.delegation_id, item.attempt),
            )

    def commit_child_terminal(self, receipt: DelegationReceipt, terminal_event: RuntimeEvent) -> DelegationReceipt:
        with self._lock:
            persisted = self.put_receipt(receipt)
            existing = self._events.get(terminal_event.event_id)
            if existing is not None and existing.run_id != terminal_event.run_id:
                raise DelegationInboxConflict("terminal event id belongs to another child")
            self._events.setdefault(terminal_event.event_id, terminal_event)
            return persisted

    def claim_parent_lease(self, parent_run_id: str, owner_id: str, *, now: datetime, ttl: timedelta) -> bool:
        with self._lock:
            existing = self._leases.get(parent_run_id)
            if existing is not None and existing[0] != owner_id and existing[1] > now:
                return False
            self._leases[parent_run_id] = (owner_id, now + ttl)
            return True

    def schedule_parent_scan(self, parent_run_id: str, *, next_scan_at: datetime, scan_attempt: int) -> None:
        with self._lock:
            for key, task in list(self._tasks.items()):
                if task.spec.parent_run_id == parent_run_id and task.status in {DelegationTaskStatus.PLANNED, DelegationTaskStatus.DISPATCHED, DelegationTaskStatus.RUNNING, DelegationTaskStatus.RECEIPT_READY}:
                    self._tasks[key] = replace(task, next_scan_at=next_scan_at, scan_attempt=scan_attempt, updated_at=next_scan_at)

    def cancel_parent(self, parent_run_id: str, *, now: datetime) -> None:
        self._mark_parent(parent_run_id, DelegationTaskStatus.CANCELLED, now=now)

    def supersede_parent(self, parent_run_id: str, *, revision: int, now: datetime) -> None:
        with self._lock:
            for key, task in list(self._tasks.items()):
                if task.spec.parent_run_id == parent_run_id and task.revision < revision and task.status not in {DelegationTaskStatus.CANCELLED, DelegationTaskStatus.SUPERSEDED}:
                    self._tasks[key] = replace(task, status=DelegationTaskStatus.SUPERSEDED, updated_at=now)

    def commit_aggregate(self, aggregate: DelegationAggregate, *, continuation_id: str, trace_id: str | None = None) -> DelegationContinuationRequest | None:
        key = (aggregate.parent_run_id, aggregate.join_group, aggregate.revision)
        with self._lock:
            existing = self._continuations.get(key)
            if existing is not None:
                return None
            request = DelegationContinuationRequest(
                continuation_id=continuation_id,
                parent_run_id=aggregate.parent_run_id,
                join_group=aggregate.join_group,
                revision=aggregate.revision,
                aggregate=aggregate.model_copy(deep=True),
                created_at=aggregate.generated_at,
            )
            self._continuations[key] = request
            return request

    def list_continuations(self, parent_run_id: str) -> List[DelegationContinuationRequest]:
        with self._lock:
            return sorted(
                (item for item in self._continuations.values() if item.parent_run_id == parent_run_id),
                key=lambda item: (item.revision, item.join_group),
            )

    def commit_verification_aggregate(
        self,
        aggregate: DelegationVerificationAggregate,
        *,
        trace_id: str | None = None,
    ) -> bool:
        del trace_id
        key = (aggregate.parent_run_id, aggregate.join_group, aggregate.revision)
        with self._lock:
            if key in self._verification_aggregates:
                return False
            self._verification_aggregates[key] = aggregate.model_copy(deep=True)
            return True

    def list_verification_aggregates(
        self,
        parent_run_id: str,
    ) -> List[DelegationVerificationAggregate]:
        with self._lock:
            return sorted(
                (
                    item.model_copy(deep=True)
                    for item in self._verification_aggregates.values()
                    if item.parent_run_id == parent_run_id
                ),
                key=lambda item: (item.revision, item.join_group),
            )

    def _mark_parent(self, parent_run_id: str, status: DelegationTaskStatus, *, now: datetime) -> None:
        with self._lock:
            for key, task in list(self._tasks.items()):
                if task.spec.parent_run_id == parent_run_id and task.status not in {DelegationTaskStatus.CANCELLED, DelegationTaskStatus.SUPERSEDED}:
                    self._tasks[key] = replace(task, status=status, updated_at=now)


def _assert_task_matches(task: DelegationTask, spec: DelegationSpec, child: DelegationChildIdentity, revision: int) -> None:
    del child
    if task.spec != spec or task.revision != revision:
        raise DelegationInboxConflict("delegation task idempotency identity conflicts with immutable facts")


def _assert_receipt_matches(existing: DelegationReceipt, candidate: DelegationReceipt) -> None:
    if existing != candidate:
        raise DelegationInboxConflict("delegation receipt identity conflicts with immutable facts")


def _assert_verification_matches(
    existing: DelegationReceiptVerification,
    candidate: DelegationReceiptVerification,
) -> None:
    if existing != candidate:
        raise DelegationInboxConflict("delegation verification identity conflicts with immutable facts")


def _assert_receipt_matches_task(receipt: DelegationReceipt, task: DelegationTask) -> None:
    spec = task.spec
    if (
        receipt.parent_run_id != spec.parent_run_id
        or receipt.parent_step_id != spec.parent_step_id
        or receipt.idempotency_key != spec.idempotency_key
        or receipt.child_run_id != task.child.child_run_id
    ):
        raise DelegationInboxConflict("delegation receipt does not match durable task identity")


def _assert_verification_matches_task(
    task: DelegationTask,
    receipt: DelegationReceipt,
    verification: DelegationReceiptVerification,
) -> None:
    spec = task.spec
    if (
        verification.parent_run_id != spec.parent_run_id
        or verification.delegation_id != spec.delegation_id
        or verification.attempt != spec.attempt
        or verification.join_group != spec.join_group
        or verification.revision != task.revision
        or verification.result_policy != spec.result_policy
        or verification.receipt_status != receipt.status
    ):
        raise DelegationInboxConflict("delegation verification does not match durable task and receipt")
    if (
        verification.verdict.value == "accepted"
        and task.status is not DelegationTaskStatus.RECEIPT_READY
    ):
        raise DelegationInboxConflict("accepted verification requires a receipt-ready delegation task")


def _expired_receipt_for_task(task: DelegationTask, now: datetime) -> DelegationReceipt:
    return DelegationReceipt(
        delegation_id=task.spec.delegation_id,
        parent_run_id=task.spec.parent_run_id,
        parent_step_id=task.spec.parent_step_id,
        tenant_id=task.spec.tenant_id,
        attempt=task.spec.attempt,
        idempotency_key=task.spec.idempotency_key,
        child_run_id=task.child.child_run_id,
        objective_hash=delegation_objective_hash(task.spec),
        spec_hash=delegation_spec_hash(task.spec),
        capability_snapshot=sorted(task.spec.capability_allowlist),
        trace_ref=f"trace_digest:{task.child.trace_id}",
        status="expired",
        reason_code="delegation.deadline_expired",
        usage=DelegationUsage(model_turns=0, tool_calls=0, duration_ms=0),
        recommended_next_action="replan",
        completed_at=now,
    )


__all__ = [
    "DelegationChildIdentity",
    "DelegationContinuationRequest",
    "DelegationInbox",
    "DelegationInboxConflict",
    "DelegationTask",
    "InMemoryDelegationInbox",
]
