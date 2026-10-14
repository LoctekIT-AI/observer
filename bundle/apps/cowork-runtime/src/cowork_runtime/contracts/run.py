"""Run contract models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import Field

from .base import ContractModel, utc_now
from .enums import RunStatus


class Run(ContractModel):
    run_id: str
    session_id: str
    objective: str
    status: RunStatus = RunStatus.CREATED
    title: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    metadata: Dict[str, Any] = Field(default_factory=dict)
