"""Persistence ports shared by Runtime application, Loop, and adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable, List, Optional, Protocol

from cowork_runtime.contracts import Run, RuntimeEvent, Turn
from cowork_runtime.contracts.enums import EventVisibility, RunStatus, RuntimeEventType, TurnStatus

if TYPE_CHECKING:
    from cowork_runtime.loop.types import LoopCheckpoint


class EventStore(Protocol):
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
        ...

    def append(self, event: RuntimeEvent) -> RuntimeEvent:
        ...

    def list_events(
        self,
        run_id: str,
        after_seq: int = 0,
        limit: int = 100,
    ) -> List[RuntimeEvent]:
        ...

    def latest_seq(self, run_id: str) -> int:
        ...

    def purge_run(self, run_id: str) -> int:
        """Delete terminal-run event facts for an explicit retention operation."""
        ...


class StateStore(Protocol):
    def get(self, run_id: str) -> Optional["LoopCheckpoint"]:
        ...

    def save(self, checkpoint: "LoopCheckpoint") -> None:
        ...

    def delete(self, run_id: str) -> None:
        ...


class RuntimeRecordStore(Protocol):
    def get_run(self, run_id: str) -> Optional[Run]:
        ...

    def save_run(self, run: Run) -> None:
        ...

    def list_runs(self, *, statuses: Optional[Iterable[RunStatus]] = None) -> List[Run]:
        ...

    def get_turn(self, turn_id: str) -> Optional[Turn]:
        ...

    def save_turn(self, turn: Turn) -> None:
        ...

    def list_turns(
        self,
        run_id: str,
        *,
        statuses: Optional[Iterable[TurnStatus]] = None,
    ) -> List[Turn]:
        ...

    def delete_session(self, session_id: str) -> int:
        """Delete all locally persisted Runtime facts belonging to a session."""
        ...

    def get_adapter_plugin(self, plugin_id: str, installation_id: str) -> Any | None:
        ...

    def find_active_adapter_installation(self, installation_id: str) -> Any | None:
        ...

    def save_adapter_plugin(self, manifest: Any, *, now: Any) -> None:
        ...

    def set_adapter_enabled(self, plugin_id: str, installation_id: str, enabled: bool, *, now: Any) -> None:
        ...

    def get_adapter_binding(self, installation_id: str) -> Any | None:
        ...

    def save_adapter_binding(self, binding: Any, *, now: Any) -> None:
        ...

    def clear_adapter_binding(self, installation_id: str, *, now: Any) -> None:
        ...

    def create_adapter_pairing_challenge(
        self,
        challenge_digest: str,
        installation_id: str,
        expires_at: Any,
        *,
        created_at: Any,
    ) -> None:
        ...

    def pair_adapter_installation(
        self,
        challenge_digest: str,
        installation_id: str,
        user_id: str,
        tenant_id: str,
        *,
        now: Any,
    ) -> dict[str, Any] | None:
        ...

    def get_adapter_cursor(self, installation_id: str, source_path: str) -> dict[str, Any] | None:
        ...

    def save_adapter_cursor(self, cursor: Any) -> None:
        ...

    def append_telemetry_outbox(self, event: dict[str, Any], *, now: Any) -> Any:
        ...

    def claim_telemetry_outbox(self, *, owner_id: str, now: Any, lease: Any, limit: int = 100) -> list[Any]:
        ...

    def mark_telemetry_delivered(self, event_id: str, *, owner_id: str, now: Any) -> None:
        ...

    def mark_telemetry_failed(self, event_id: str, *, owner_id: str, now: Any, error_code: str, retry_delay: Any, max_attempts: int = 5) -> None:
        ...


class CloseableStore(Protocol):
    def close(self) -> None:
        ...
