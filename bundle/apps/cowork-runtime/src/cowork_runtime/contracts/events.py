"""Runtime event contract."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Literal, Optional

from pydantic import Field, field_validator

from .base import ContractModel, utc_now
from .enums import EventVisibility, RuntimeEventType
from .ids import new_contract_id

RUNTIME_EVENT_SCHEMA_VERSION = "cowork.runtime.event.v1"


class RuntimeEvent(ContractModel):
    schema_version: Literal["cowork.runtime.event.v1"] = RUNTIME_EVENT_SCHEMA_VERSION
    run_id: str
    seq: int = Field(ge=1)
    event_id: str = Field(default_factory=lambda: new_contract_id("evt"))
    type: RuntimeEventType
    occurred_at: datetime = Field(default_factory=utc_now)
    visibility: EventVisibility = EventVisibility.USER
    payload: Dict[str, Any] = Field(default_factory=dict)
    trace_id: Optional[str] = None

    @field_validator("run_id", "event_id")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("value must not be empty")
        return value

    def to_json_dict(self) -> Dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_json_dict(cls, data: Dict[str, Any]) -> "RuntimeEvent":
        return cls.model_validate(data)
