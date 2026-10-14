"""In-memory stores used by unit tests and local composition."""

from __future__ import annotations

from copy import deepcopy
from threading import RLock
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional

from cowork_runtime.contracts import Run, RuntimeEvent, RuntimeEventLedger, Turn
from cowork_runtime.contracts.enums import (
    EventVisibility,
    RunStatus,
    RuntimeEventType,
    TurnStatus,
)

if TYPE_CHECKING:
    from cowork_runtime.loop.types import LoopCheckpoint


class InMemoryStateStore:
    def __init__(self) -> None:
        self._checkpoints: Dict[str, "LoopCheckpoint"] = {}
        self._lock = RLock()

    def get(self, run_id: str) -> Optional["LoopCheckpoint"]:
        with self._lock:
            checkpoint = self._checkpoints.get(run_id)
            return deepcopy(checkpoint) if checkpoint is not None else None

    def save(self, checkpoint: "LoopCheckpoint") -> None:
        with self._lock:
            self._checkpoints[checkpoint.checkpoint_key or checkpoint.run_id] = deepcopy(checkpoint)

    def delete(self, run_id: str) -> None:
        with self._lock:
            self._checkpoints.pop(run_id, None)


class InMemoryEventStore:
    def __init__(self) -> None:
        self._ledgers: Dict[str, RuntimeEventLedger] = {}
        self._lock = RLock()

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
        with self._lock:
            ledger = self._ledger_for(run_id)
            return ledger.next_event(
                event_type,
                payload,
                visibility=visibility,
                event_id=event_id,
                trace_id=trace_id,
            ).event

    def append(self, event: RuntimeEvent) -> RuntimeEvent:
        with self._lock:
            return self._ledger_for(event.run_id).append(event).event

    def list_events(self, run_id: str, after_seq: int = 0, limit: int = 100) -> List[RuntimeEvent]:
        with self._lock:
            return self._ledger_for(run_id).list_events(after_seq=after_seq, limit=limit)

    def latest_seq(self, run_id: str) -> int:
        with self._lock:
            return self._ledger_for(run_id).latest_seq

    def purge_run(self, run_id: str) -> int:
        with self._lock:
            ledger = self._ledgers.pop(run_id, None)
            return len(ledger.list_events(after_seq=0, limit=500)) if ledger is not None else 0

    def _ledger_for(self, run_id: str) -> RuntimeEventLedger:
        ledger = self._ledgers.get(run_id)
        if ledger is None:
            ledger = RuntimeEventLedger(run_id)
            self._ledgers[run_id] = ledger
        return ledger


class InMemoryRuntimeRecordStore:
    def __init__(self) -> None:
        self._runs: Dict[str, Run] = {}
        self._turns: Dict[str, Turn] = {}
        self._lock = RLock()

    def get_run(self, run_id: str) -> Optional[Run]:
        with self._lock:
            run = self._runs.get(run_id)
            return run.model_copy(deep=True) if run is not None else None

    def save_run(self, run: Run) -> None:
        with self._lock:
            self._runs[run.run_id] = run.model_copy(deep=True)

    def list_runs(self, *, statuses: Optional[Iterable[RunStatus]] = None) -> List[Run]:
        status_set = set(statuses) if statuses is not None else None
        with self._lock:
            return [
                run.model_copy(deep=True)
                for run in self._runs.values()
                if status_set is None or run.status in status_set
            ]

    def get_turn(self, turn_id: str) -> Optional[Turn]:
        with self._lock:
            turn = self._turns.get(turn_id)
            return turn.model_copy(deep=True) if turn is not None else None

    def save_turn(self, turn: Turn) -> None:
        with self._lock:
            self._turns[turn.turn_id] = turn.model_copy(deep=True)

    def list_turns(
        self,
        run_id: str,
        *,
        statuses: Optional[Iterable[TurnStatus]] = None,
    ) -> List[Turn]:
        status_set = set(statuses) if statuses is not None else None
        with self._lock:
            return [
                turn.model_copy(deep=True)
                for turn in self._turns.values()
                if turn.run_id == run_id
                and (status_set is None or turn.status in status_set)
            ]

    def delete_session(self, session_id: str) -> int:
        with self._lock:
            run_ids = {
                run_id
                for run_id, run in self._runs.items()
                if run.session_id == session_id
            }
            for run_id in run_ids:
                self._runs.pop(run_id, None)
            self._turns = {
                turn_id: turn
                for turn_id, turn in self._turns.items()
                if turn.run_id not in run_ids
            }
            return len(run_ids)
