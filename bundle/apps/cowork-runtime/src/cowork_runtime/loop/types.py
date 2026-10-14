"""Loop ports and small data contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

from cowork_contracts import ContextSnapshot, DeliveryOutcome
from cowork_runtime.contracts import Approval, ArtifactRef, RunStatus, RuntimeError, RuntimeEvent, ToolCall, ToolResult
from cowork_runtime.contracts.enums import ApprovalStatus

from .cancellation import CancellationToken


PROVIDER_TOOL_ARGUMENTS_ERROR_METADATA_KEY = "provider_tool_arguments_error"


@dataclass(frozen=True)
class ToolCallIntent:
    tool_name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelTurnOutput:
    content: str = ""
    tool_calls: List[ToolCallIntent] = field(default_factory=list)
    stop_reason: str = "final"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LoopInput:
    run_id: str
    session_id: str
    user_message: str
    checkpoint_key: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    # Runtime-owned system facts for a fresh, isolated loop. HTTP handlers may
    # provide only validated input that the application layer transforms into
    # these messages; raw command metadata never becomes a system message.
    initial_system_messages: List[str] = field(default_factory=list)
    # Runtime intake supplies one already-persisted snapshot for normal chat.
    # Other internal loops may omit it and use the legacy Session adapter.
    context_snapshot: ContextSnapshot | None = None


@dataclass(frozen=True)
class LoopResume:
    run_id: str
    checkpoint_key: Optional[str] = None
    approval_id: Optional[str] = None
    decision: ApprovalStatus = ApprovalStatus.ALLOWED_ONCE
    reason: Optional[str] = None


@dataclass(frozen=True)
class CrawlerInteractionResume:
    """Opaque Desktop confirmation that a pending crawler session is ready.

    The contract deliberately carries identifiers only. Browser cookies and
    credentials remain inside the Desktop-owned browser profile.
    """

    run_id: str
    crawl_job_id: str
    interaction_id: str
    session_ref: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LoopContext:
    run_id: str
    session_id: str
    messages: List[Dict[str, Any]]
    metadata: Dict[str, Any] = field(default_factory=dict)
    cancellation_token: CancellationToken = field(
        default_factory=CancellationToken,
        compare=False,
        repr=False,
    )


@dataclass(frozen=True)
class ToolExecutionContext:
    run_id: str
    session_id: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    cancellation_token: CancellationToken = field(
        default_factory=CancellationToken,
        compare=False,
        repr=False,
    )


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool = True
    requires_approval: bool = False
    reason: Optional[str] = None
    reason_code: Optional[str] = None
    approval_kind: str = "tool_call"
    # 标记该调用是否属于删除/移动/危险命令类操作；随审批 payload 下发给
    # Renderer，用于决定授权范围（非破坏性可会话级授权，破坏性必须逐次确认）。
    destructive: bool = False


@dataclass
class LoopCheckpoint:
    run_id: str
    session_id: str
    messages: List[Dict[str, Any]]
    checkpoint_key: Optional[str] = None
    status: RunStatus = RunStatus.CREATED
    model_turns: int = 0
    # Provider-reported cumulative prompt + completion tokens for this Run.
    # Persisted separately from model_turns because one turn may carry a large
    # tool schema/history and many short turns may still be expensive.
    model_tokens: int = 0
    tool_calls: int = 0
    failures: int = 0
    repair_attempts: int = 0
    transient_retries: int = 0
    model_unavailable_retries: int = 0
    repair_error_signatures: Dict[str, int] = field(default_factory=dict)
    last_error_signature: Optional[str] = None
    # Recovery detector state is intentionally a small, secret-free JSON
    # document so restart/resume cannot reset loop protections.
    recovery_state: Dict[str, Any] = field(default_factory=dict)
    pending_tool_call: Optional[ToolCall] = None
    # A provider tool-call message is an atomic protocol batch. When one call
    # pauses for approval, retain the later calls so approval resume can finish
    # the same batch before making another model request.
    pending_tool_intents: List[ToolCallIntent] = field(default_factory=list)
    pending_approval: Optional[Approval] = None
    final_content: str = ""
    # 当前 Run 的产物属于 checkpoint 状态，不依赖事件查询派生；这样可避免
    # 终态判定的写后读竞态，并支持审批恢复和重启恢复。
    artifact_refs: List[ArtifactRef] = field(default_factory=list)
    delivery_outcome: Optional[DeliveryOutcome] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LoopResult:
    run_id: str
    status: RunStatus
    content: str = ""
    pending_approval: Optional[Approval] = None
    error: Optional[RuntimeError] = None
    events: List[RuntimeEvent] = field(default_factory=list)
    tool_results: List[ToolResult] = field(default_factory=list)
    delivery_outcome: Optional[DeliveryOutcome] = None


class ModelClient(Protocol):
    async def complete(self, context: LoopContext) -> ModelTurnOutput:
        ...


class ToolHost(Protocol):
    async def execute(self, call: ToolCall, context: ToolExecutionContext) -> ToolResult:
        ...


class Policy(Protocol):
    async def check_tool_call(self, call: ToolCall, context: ToolExecutionContext) -> PolicyDecision:
        ...
