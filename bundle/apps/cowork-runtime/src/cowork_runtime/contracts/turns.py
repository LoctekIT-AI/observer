"""Turn and step contract models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import Field

from .base import ContractModel, utc_now
from .enums import StepKind, StepStatus, TurnRole, TurnStatus


class Turn(ContractModel):
    turn_id: str
    run_id: str
    role: TurnRole
    status: TurnStatus = TurnStatus.CREATED
    content: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class Step(ContractModel):
    step_id: str
    run_id: str
    kind: StepKind
    status: StepStatus = StepStatus.CREATED
    turn_id: Optional[str] = None
    label: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
