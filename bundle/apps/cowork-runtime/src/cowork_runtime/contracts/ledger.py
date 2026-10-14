"""Append-only RuntimeEvent sequencing helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .base import utc_now
from .enums import EventVisibility, RuntimeEventType
from .errors import RuntimeSequenceError
from .events import RuntimeEvent


@dataclass(frozen=True)
class RuntimeEventLedgerAppend:
    event: RuntimeEvent
    appended: bool
    duplicate: bool


class RuntimeEventLedger:
    """In-memory event ledger used by runtime implementations and tests.

    Persistent stores should preserve the same rule: append exactly latest_seq+1,
    and make retries idempotent by event_id.
    """

    def __init__(self, run_id: str) -> None:
        if not run_id or not run_id.strip():
            raise ValueError("run_id must not be empty")
        self.run_id = run_id
        self._events: List[RuntimeEvent] = []
        self._events_by_id: Dict[str, RuntimeEvent] = {}

    @property
    def latest_seq(self) -> int:
        return self._events[-1].seq if self._events else 0

    def append(self, event: RuntimeEvent) -> RuntimeEventLedgerAppend:
        if event.run_id != self.run_id:
            raise RuntimeSequenceError(
                f"event run_id {event.run_id!r} does not match ledger run_id {self.run_id!r}"
            )

        existing = self._events_by_id.get(event.event_id)
        if existing is not None:
            return RuntimeEventLedgerAppend(event=existing, appended=False, duplicate=True)

        expected_seq = self.latest_seq + 1
        if event.seq != expected_seq:
            raise RuntimeSequenceError(
                f"event seq must be {expected_seq}; got {event.seq}"
            )

        self._events.append(event)
        self._events_by_id[event.event_id] = event
        return RuntimeEventLedgerAppend(event=event, appended=True, duplicate=False)

    def next_event(
        self,
        event_type: RuntimeEventType,
        payload: Optional[Dict[str, Any]] = None,
        *,
        visibility: EventVisibility = EventVisibility.USER,
        event_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> RuntimeEventLedgerAppend:
        event = RuntimeEvent(
            run_id=self.run_id,
            seq=self.latest_seq + 1,
            event_id=event_id or RuntimeEvent.model_fields["event_id"].default_factory(),
            type=event_type,
            occurred_at=utc_now(),
            visibility=visibility,
            payload=payload or {},
            trace_id=trace_id,
        )
        return self.append(event)

    def list_events(self, after_seq: int = 0, limit: int = 100) -> List[RuntimeEvent]:
        bounded_limit = max(1, min(limit or 100, 500))
        return [event for event in self._events if event.seq > after_seq][:bounded_limit]
