"""Transport-neutral MCP 生命周期/进度公共事件契约（MCP-TRACE-011）。

API（PostgreSQL）与 Desktop sidecar（SQLite）分别拥有各自事实源，但共享
本版本化 contract 的公共语义；adapter 分别把本 contract 投影为
CoworkRuntimeEvent（API）与本地 RuntimeEvent（sidecar）。

安全边界（ADR-002 §G + 011 非目标）：
- contract 不含 credential、参数、SQL 或原始输出字段；
- schema version mismatch fail-closed，不静默猜测终态；
- 去重/重启 replay 由 call_id + seq + 各自事实源 owner 保证。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MCP_LIFECYCLE_CONTRACT_SCHEMA_VERSION = "cowork.mcp.lifecycle.v1"

McpCallStatus = Literal["dispatched", "completed", "failed", "cancelled", "timeout"]
# 有限 reason code（与 004 的 MCP_PROGRESS_REASON_CODES 对齐）
McpReasonCode = Literal[
    "argument_normalized",
    "transport_connecting",
    "retry_scheduled",
    "retry_exhausted",
    "timeout",
    "cancelled",
]

ALLOWED_REASON_CODES = frozenset({
    "argument_normalized",
    "transport_connecting",
    "retry_scheduled",
    "retry_exhausted",
    "timeout",
    "cancelled",
})


class McpPublicSummary(BaseModel):
    """脱敏公共摘要：只含规模/标签，不含参数/SQL/凭证/结果正文。"""

    model_config = ConfigDict(extra="forbid")

    server: str = Field(min_length=1, max_length=256)
    tool: str = Field(min_length=1, max_length=256)
    row_count: int | None = None
    byte_size: int | None = None
    artifact_id: str | None = None


class McpLifecycleEvent(BaseModel):
    """一次 MCP 调用的生命周期/进度事件（transport-neutral）。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["cowork.mcp.lifecycle.v1"]
    call_id: str = Field(min_length=1, max_length=128)
    status: McpCallStatus
    # 进度事件（dispatched 后、终态前）用 reason_code 表达中间状态
    reason_code: McpReasonCode | None = None
    summary: McpPublicSummary | None = None
    error_code: str | None = Field(default=None, max_length=128)
    duration_ms: int = Field(default=0, ge=0)
    # 终态事件需要 seq 支持重启 replay；dispatched/progress 可不带
    seq: int | None = Field(default=None, ge=0)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("error_code")
    @classmethod
    def canonical_error_code(cls, value: str | None) -> str | None:
        if value is not None and value != value.strip():
            raise ValueError("error_code must not contain surrounding whitespace")
        return value

    @field_validator("reason_code")
    @classmethod
    def reason_code_is_known(cls, value: str | None) -> str | None:
        if value is not None and value not in ALLOWED_REASON_CODES:
            raise ValueError(f"unknown reason_code: {value}")
        return value

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include timezone")
        return value.astimezone(timezone.utc)


def parse_mcp_lifecycle_event(payload: dict[str, Any]) -> McpLifecycleEvent:
    """schema mismatch 时 fail-closed（抛 pydantic ValidationError），不猜测终态。"""
    return McpLifecycleEvent.model_validate(payload)


def is_terminal_status(status: str) -> bool:
    """判断是否终态（completed/failed/cancelled/timeout）。"""
    return status in {"completed", "failed", "cancelled", "timeout"}


__all__ = [
    "ALLOWED_REASON_CODES",
    "MCP_LIFECYCLE_CONTRACT_SCHEMA_VERSION",
    "McpCallStatus",
    "McpLifecycleEvent",
    "McpPublicSummary",
    "McpReasonCode",
    "is_terminal_status",
    "parse_mcp_lifecycle_event",
]
