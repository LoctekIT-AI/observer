"""Persistence port for Runtime-owned task and context snapshot facts."""

from __future__ import annotations

from copy import deepcopy
from threading import RLock
from typing import Protocol

from cowork_contracts import ContextSnapshot, TaskEnvelope


class TaskContextStore(Protocol):
    """Store one active task and immutable snapshots per local Conversation."""

    def get_active_task(self, conversation_id: str) -> TaskEnvelope | None:
        ...

    def next_context_snapshot_revision(self, conversation_id: str) -> int:
        ...

    def save_task_context(self, task: TaskEnvelope, snapshot: ContextSnapshot) -> ContextSnapshot:
        ...

    def get_context_snapshot(self, conversation_id: str, revision: int) -> ContextSnapshot | None:
        ...

    def get_latest_context_snapshot(self, conversation_id: str) -> ContextSnapshot | None:
        ...


class InMemoryTaskContextStore:
    """Test/default task-context store with the same revision semantics as SQLite."""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskEnvelope] = {}
        self._snapshots: dict[tuple[str, int], ContextSnapshot] = {}
        self._lock = RLock()

    def get_active_task(self, conversation_id: str) -> TaskEnvelope | None:
        with self._lock:
            task = self._tasks.get(conversation_id)
            return task.model_copy(deep=True) if task is not None else None

    def next_context_snapshot_revision(self, conversation_id: str) -> int:
        with self._lock:
            revisions = [revision for owner, revision in self._snapshots if owner == conversation_id]
            return (max(revisions) if revisions else 0) + 1

    def save_task_context(self, task: TaskEnvelope, snapshot: ContextSnapshot) -> ContextSnapshot:
        if task.conversation_id != snapshot.conversation_id or task.task_id != snapshot.task_id:
            raise ValueError("task context snapshot lineage mismatch")
        with self._lock:
            existing = self._snapshots.get((snapshot.conversation_id, snapshot.revision))
            if existing is not None:
                if existing.snapshot_id != snapshot.snapshot_id:
                    raise ValueError("context snapshot revision already belongs to another snapshot")
                return existing.model_copy(deep=True)
            self._tasks[task.conversation_id] = task.model_copy(deep=True)
            self._snapshots[(snapshot.conversation_id, snapshot.revision)] = snapshot.model_copy(deep=True)
            return snapshot.model_copy(deep=True)

    def get_context_snapshot(self, conversation_id: str, revision: int) -> ContextSnapshot | None:
        with self._lock:
            snapshot = self._snapshots.get((conversation_id, revision))
            return snapshot.model_copy(deep=True) if snapshot is not None else None

    def get_latest_context_snapshot(self, conversation_id: str) -> ContextSnapshot | None:
        with self._lock:
            snapshots = [
                snapshot
                for (owner, _revision), snapshot in self._snapshots.items()
                if owner == conversation_id
            ]
            if not snapshots:
                return None
            return max(snapshots, key=lambda item: item.revision).model_copy(deep=True)
