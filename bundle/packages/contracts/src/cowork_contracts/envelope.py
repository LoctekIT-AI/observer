from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PLATFORM_PROTOCOL_VERSION = "1.0"
PLATFORM_APP = "cowork"
MAX_ENVELOPE_LIFETIME = timedelta(seconds=60)


class SubjectContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    department_id: str | None = None

    @field_validator("user_id", "tenant_id", "department_id")
    @classmethod
    def require_canonical_subject_value(cls, value: str | None) -> str | None:
        if value is not None and value != value.strip():
            raise ValueError("subject values must not contain surrounding whitespace")
        return value


class ContractEnvelope(BaseModel):
    """Common envelope for service-to-service requests.

    A user Bearer token is deliberately not represented here. The API boundary
    validates it first; adapters use service authentication and carry this
    validated subject context as data.
    """

    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal["1.0"]
    app: Literal["cowork"]
    request_id: UUID
    trace_id: str = Field(min_length=1, max_length=128)
    subject: SubjectContext
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)
    issued_at: datetime
    expires_at: datetime
    payload: dict[str, Any]

    @field_validator("issued_at", "expires_at", mode="before")
    @classmethod
    def require_datetime_value(cls, value: object) -> object:
        if not isinstance(value, (str, datetime)):
            raise ValueError("timestamp must be an RFC3339 string")
        return value

    @field_validator("issued_at", "expires_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include timezone")
        return value.astimezone(timezone.utc)

    @field_validator("trace_id", "idempotency_key")
    @classmethod
    def require_canonical_metadata(cls, value: str | None) -> str | None:
        if value is not None and value != value.strip():
            raise ValueError("envelope metadata must not contain surrounding whitespace")
        return value

    @model_validator(mode="after")
    def require_bounded_window(self) -> "ContractEnvelope":
        lifetime = self.expires_at - self.issued_at
        if lifetime <= timedelta(0):
            raise ValueError("expires_at must be after issued_at")
        if lifetime > MAX_ENVELOPE_LIFETIME:
            raise ValueError("envelope lifetime exceeds 60 seconds")
        return self

    def is_expired(self, now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        return self.expires_at <= current.astimezone(timezone.utc)
