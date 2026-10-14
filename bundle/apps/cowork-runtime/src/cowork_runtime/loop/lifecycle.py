"""Typed lifecycle facts for one local Runtime turn.

The main AgentLoop deliberately remains the orchestration entry point.  This
module gives it a small, serializable state machine so a restart or a trace
viewer can distinguish a model attempt, tool dispatch, suspension and terminal
transition without inferring them from free-form messages.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, MutableMapping


class TurnPhase(str, Enum):
    CREATED = "created"
    MODEL_ATTEMPT = "model_attempt"
    TOOL_DISPATCH = "tool_dispatch"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_INPUT = "waiting_input"
    COMPACTING = "compacting"
    RESUMED = "resumed"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TurnLifecycle:
    """The latest durable lifecycle position of a Run's active turn."""

    turn_id: str
    phase: TurnPhase = TurnPhase.CREATED
    attempt: int = 0
    last_tool_call_id: str | None = None
    updated_at: str = ""

    @classmethod
    def new(cls, turn_id: str) -> "TurnLifecycle":
        return cls(turn_id=turn_id, updated_at=_now())

    def transition(
        self,
        phase: TurnPhase,
        *,
        tool_call_id: str | None = None,
        increment_attempt: bool = False,
    ) -> "TurnLifecycle":
        return TurnLifecycle(
            turn_id=self.turn_id,
            phase=phase,
            attempt=self.attempt + (1 if increment_attempt else 0),
            last_tool_call_id=tool_call_id if tool_call_id is not None else self.last_tool_call_id,
            updated_at=_now(),
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["phase"] = self.phase.value
        return value

    @classmethod
    def from_mapping(cls, value: object, *, default_turn_id: str) -> "TurnLifecycle":
        if not isinstance(value, MutableMapping):
            return cls.new(default_turn_id)
        try:
            phase = TurnPhase(str(value.get("phase", TurnPhase.CREATED.value)))
        except ValueError:
            phase = TurnPhase.CREATED
        return cls(
            turn_id=str(value.get("turn_id") or default_turn_id),
            phase=phase,
            attempt=max(0, int(value.get("attempt") or 0)),
            last_tool_call_id=(str(value["last_tool_call_id"]) if value.get("last_tool_call_id") else None),
            updated_at=str(value.get("updated_at") or _now()),
        )


def transition_checkpoint_lifecycle(
    metadata: MutableMapping[str, Any],
    *,
    turn_id: str,
    phase: TurnPhase,
    tool_call_id: str | None = None,
    increment_attempt: bool = False,
) -> dict[str, Any]:
    """Advance the lifecycle in the secret-free checkpoint projection."""

    current = TurnLifecycle.from_mapping(metadata.get("turn_lifecycle"), default_turn_id=turn_id)
    next_state = current.transition(
        phase,
        tool_call_id=tool_call_id,
        increment_attempt=increment_attempt,
    )
    metadata["turn_lifecycle"] = next_state.to_dict()
    return next_state.to_dict()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
