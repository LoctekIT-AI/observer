"""Human approval contract models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import Field

from .base import ContractModel, utc_now
from .enums import ApprovalStatus


class Approval(ContractModel):
    approval_id: str
    run_id: str
    status: ApprovalStatus = ApprovalStatus.REQUESTED
    kind: str
    resource_id: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    requested_at: datetime = Field(default_factory=utc_now)
    decided_at: Optional[datetime] = None
    decided_by: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
