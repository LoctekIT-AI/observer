"""Typed Phase 1 telemetry response contracts and per-event validation."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import jsonschema
from pydantic import BaseModel, ConfigDict, Field, model_validator


TELEMETRY_BATCH_SCHEMA_VERSION = "agent.telemetry.batch.v1"
TELEMETRY_MAX_BATCH_EVENTS = 1000
TELEMETRY_MAX_CLOCK_SKEW_SECONDS = 300


class TelemetryEventStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


class TelemetryRejectionCode(StrEnum):
    SCHEMA_INVALID = "schema_invalid"
    UNSUPPORTED_EVENT_TYPE = "unsupported_event_type"
    SENSITIVE_FIELD = "sensitive_field"
    TENANT_MISMATCH = "tenant_mismatch"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    CLOCK_INVALID = "clock_invalid"
    PAYLOAD_TOO_LARGE = "payload_too_large"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TelemetryEventResult(_StrictModel):
    event_id: str = Field(min_length=1, max_length=256)
    status: TelemetryEventStatus
    idempotency_key: str = Field(min_length=1, max_length=256)
    ingest_id: str | None = Field(default=None, min_length=1, max_length=256)
    rejection_code: TelemetryRejectionCode | None = None

    @model_validator(mode="after")
    def validate_result_fields(self) -> "TelemetryEventResult":
        if self.status is TelemetryEventStatus.REJECTED:
            if self.rejection_code is None:
                raise ValueError("rejected result requires rejection_code")
            if self.ingest_id is not None:
                raise ValueError("rejected result cannot contain ingest_id")
        elif self.rejection_code is not None:
            raise ValueError("accepted or duplicate result cannot contain rejection_code")
        elif self.ingest_id is None:
            raise ValueError("accepted or duplicate result requires ingest_id")
        return self


class AgentTelemetryIngestResponse(_StrictModel):
    batch_id: str = Field(min_length=1, max_length=256)
    accepted_count: int = Field(ge=0)
    duplicate_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    events: list[TelemetryEventResult] = Field(min_length=1, max_length=TELEMETRY_MAX_BATCH_EVENTS)

    @model_validator(mode="after")
    def counts_match_results(self) -> "AgentTelemetryIngestResponse":
        counts = {
            TelemetryEventStatus.ACCEPTED: self.accepted_count,
            TelemetryEventStatus.DUPLICATE: self.duplicate_count,
            TelemetryEventStatus.REJECTED: self.rejected_count,
        }
        actual = {status: sum(item.status is status for item in self.events) for status in counts}
        if counts != actual:
            raise ValueError("response counts must match one result per event")
        return self


class EventValidationResult(_StrictModel):
    valid: bool
    rejection_code: TelemetryRejectionCode | None = None
    errors: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validity_matches_code(self) -> "EventValidationResult":
        if self.valid and self.rejection_code is not None:
            raise ValueError("valid event cannot contain rejection_code")
        if not self.valid and self.rejection_code is None:
            raise ValueError("invalid event requires rejection_code")
        return self


@lru_cache(maxsize=1)
def _event_validator() -> jsonschema.Draft202012Validator:
    schema_path = Path(__file__).resolve().parents[2] / "schemas" / "agent-telemetry-batch-v1.schema.json"
    raw = json.loads(schema_path.read_text(encoding="utf-8"))
    event_schema = {
        "$schema": raw["$schema"],
        "$defs": raw["$defs"],
        "$ref": "#/$defs/event",
    }
    validator = jsonschema.Draft202012Validator(event_schema, format_checker=jsonschema.FormatChecker())
    jsonschema.Draft202012Validator.check_schema(event_schema)
    return validator


def _requires_utc(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed)


def validate_event(event: Mapping[str, Any]) -> EventValidationResult:
    """Validate exactly one event and return a structured result, never a partial success."""

    if not isinstance(event, Mapping):
        return EventValidationResult(valid=False, rejection_code=TelemetryRejectionCode.SCHEMA_INVALID, errors=("event must be an object",))
    errors = sorted(_event_validator().iter_errors(dict(event)), key=lambda item: list(item.path))
    if errors:
        messages = tuple(error.message for error in errors[:8])
        code = (
            TelemetryRejectionCode.UNSUPPORTED_EVENT_TYPE
            if any(error.validator == "const" and list(error.path) == ["event_type"] for error in errors)
            else TelemetryRejectionCode.SCHEMA_INVALID
        )
        return EventValidationResult(valid=False, rejection_code=code, errors=messages)
    occurred_at = event.get("occurred_at")
    if not _requires_utc(occurred_at):
        return EventValidationResult(
            valid=False,
            rejection_code=TelemetryRejectionCode.SCHEMA_INVALID,
            errors=("occurred_at must be an RFC3339 UTC timestamp",),
        )
    return EventValidationResult(valid=True)


__all__ = [
    "AgentTelemetryIngestResponse",
    "EventValidationResult",
    "TELEMETRY_BATCH_SCHEMA_VERSION",
    "TELEMETRY_MAX_BATCH_EVENTS",
    "TELEMETRY_MAX_CLOCK_SKEW_SECONDS",
    "TelemetryEventResult",
    "TelemetryEventStatus",
    "TelemetryRejectionCode",
    "validate_event",
]
