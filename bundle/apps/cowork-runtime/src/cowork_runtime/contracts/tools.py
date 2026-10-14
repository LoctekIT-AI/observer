"""Tool invocation contract models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import Field

from .base import ContractModel, utc_now
from .enums import ToolCallStatus
from .errors import RuntimeError
from .recovery import RecoveryGuidance


class ToolCall(ContractModel):
    tool_call_id: str
    run_id: str
    tool_name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    status: ToolCallStatus = ToolCallStatus.CREATED
    step_id: Optional[str] = None
    requested_at: datetime = Field(default_factory=utc_now)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ToolResult(ContractModel):
    tool_call_id: str
    run_id: str
    status: ToolCallStatus
    output: Any = None
    error: Optional[RuntimeError] = None
    recovery: Optional[RecoveryGuidance] = None
    finished_at: datetime = Field(default_factory=utc_now)
    metadata: Dict[str, Any] = Field(default_factory=dict)
