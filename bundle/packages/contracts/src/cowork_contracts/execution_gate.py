"""Typed clarification and external execution-gate contracts.

Clarification completes the current conversational turn.  A gate instead
keeps the same Run resumable because a trusted external decision is still
required.  Keeping those cases separate prevents renderer state from
guessing whether an input box should be locked.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


CLARIFICATION_OUTCOME_CONTRACT_VERSION = "cowork.clarification-outcome.v1"
EXECUTION_GATE_OUTCOME_CONTRACT_VERSION = "cowork.execution-gate-outcome.v1"

ClarificationReasonCode = Literal[
    "clarification.policy_gate",
    "clarification.artifact_intent",
    "clarification.asset_selection",
    "clarification.user_question",
]
ExecutionGateKind = Literal["approval", "user_input", "desktop_takeover"]
ExecutionGateState = Literal["pending", "resumed", "cancelled", "timed_out"]


class _OutcomeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClarificationOption(_OutcomeModel):
    """One stable option shown to a user during a clarification turn."""

    option_id: str = Field(min_length=1, max_length=160)
    display_index: int = Field(ge=1, le=100)
    label: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=2_000)
    value: dict[str, Any] = Field(default_factory=dict)


class ClarificationRequest(_OutcomeModel):
    """Durable pending choice bound to one task snapshot."""

    clarification_id: str = Field(min_length=1, max_length=160)
    task_id: str = Field(min_length=1, max_length=160)
    conversation_id: str = Field(min_length=1, max_length=160)
    source_revision: int = Field(ge=1)
    question: str = Field(min_length=1, max_length=4_000)
    options: list[ClarificationOption] = Field(min_length=1, max_length=20)
    blocking_policy: Literal["required", "optional"] = "required"
    default_value: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def _timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clarification expiry must include timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _unique_indexes(self) -> "ClarificationRequest":
        indexes = [item.display_index for item in self.options]
        ids = [item.option_id for item in self.options]
        if len(indexes) != len(set(indexes)) or len(ids) != len(set(ids)):
            raise ValueError("clarification options must have unique indexes and option_ids")
        return self


class ClarificationSelection(_OutcomeModel):
    """Validated user choice; display text is never treated as the value."""

    clarification_id: str = Field(min_length=1, max_length=160)
    option_id: str = Field(min_length=1, max_length=160)
    display_index: int | None = Field(default=None, ge=1, le=100)
    value: dict[str, Any] = Field(default_factory=dict)


class ClarificationOutcome(_OutcomeModel):
    """A completed response that asks the next Turn for more information."""

    schema_version: Literal["cowork.clarification-outcome.v1"] = CLARIFICATION_OUTCOME_CONTRACT_VERSION
    turn_id: str = Field(min_length=1, max_length=160)
    task_id: str = Field(min_length=1, max_length=160)
    reason_code: ClarificationReasonCode
    next_turn_required: Literal[True] = True


class ExecutionGateOutcome(_OutcomeModel):
    """One durable same-Run pause owned by Runtime, never by renderer memory."""

    schema_version: Literal["cowork.execution-gate-outcome.v1"] = EXECUTION_GATE_OUTCOME_CONTRACT_VERSION
    run_id: str = Field(min_length=1, max_length=160)
    gate_id: str = Field(min_length=1, max_length=160)
    kind: ExecutionGateKind
    state: ExecutionGateState = "pending"
    reason_code: str = Field(pattern=r"^gate\.[a-z0-9_.-]{1,112}$")
    owner: Literal["runtime"] = "runtime"
    created_at: datetime
    expires_at: datetime | None = None

    @field_validator("created_at", "expires_at")
    @classmethod
    def _timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("execution gate timestamps must include timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _expiry_is_monotonic(self) -> "ExecutionGateOutcome":
        if self.expires_at is not None and self.expires_at < self.created_at:
            raise ValueError("execution gate expiry must not precede creation")
        return self


def parse_clarification_outcome(payload: object) -> ClarificationOutcome:
    return ClarificationOutcome.model_validate(payload)


def parse_execution_gate_outcome(payload: object) -> ExecutionGateOutcome:
    return ExecutionGateOutcome.model_validate(payload)


__all__ = [
    "CLARIFICATION_OUTCOME_CONTRACT_VERSION",
    "EXECUTION_GATE_OUTCOME_CONTRACT_VERSION",
    "ClarificationOutcome",
    "ClarificationOption",
    "ClarificationRequest",
    "ClarificationSelection",
    "ExecutionGateOutcome",
    "ExecutionGateKind",
    "ExecutionGateState",
    "parse_clarification_outcome",
    "parse_execution_gate_outcome",
]
