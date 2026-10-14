"""Desktop interaction task and context snapshot contracts.

The Local Runtime compiles these contracts once at turn intake.  They carry
task semantics across process restart without turning renderer metadata or
historical message text into a second task-intent authority.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .assets import AssetRef
from .execution_gate import ClarificationRequest


TASK_ENVELOPE_CONTRACT_VERSION = "cowork.task-envelope.v1"
CONTEXT_SNAPSHOT_CONTRACT_VERSION = "cowork.context-snapshot.v1"

ExecutionTarget = Literal["desktop", "sandbox"]
ExecutionSurface = Literal["chat", "artifact", "code", "review", "research"]
CapabilityStatus = Literal["available", "blocked", "unavailable"]
VerificationRequirement = Literal[
    "workspace_write",
    "command_execution",
    "tests_or_build",
    "dev_server_readiness",
    "artifact_published",
]


class _TaskContextModel(BaseModel):
    """Strict, transport-neutral base for Desktop task facts."""

    model_config = ConfigDict(extra="forbid")


class TaskConstraint(_TaskContextModel):
    """One confirmed constraint copied into the Runtime-owned task record."""

    content: str = Field(min_length=1, max_length=2_000)
    source: Literal["runtime_intake", "legacy_session_adapter"]

    @field_validator("content")
    @classmethod
    def require_canonical_content(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("task constraint must not be blank")
        return normalized


class ModuleConstraints(_TaskContextModel):
    """Runtime-verifiable constraints for an explicitly modular coding task."""

    development_mode: Literal["standard", "modular"] = "standard"
    avoid_single_file: bool = False
    minimum_changed_files: int = Field(default=1, ge=1, le=32)
    preferred_directories: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def normalize_constraints(self) -> "ModuleConstraints":
        if self.development_mode == "modular":
            self.avoid_single_file = True
            if self.minimum_changed_files < 2:
                self.minimum_changed_files = 2
        return self


class ExecutionContext(_TaskContextModel):
    """The Runtime-owned execution decision attached to one snapshot.

    ``target`` identifies the execution host, while ``surface`` identifies the
    kind of work. Neither field grants permission: Host grants, approval and
    tool-level policy remain authoritative.
    """

    execution_context_id: str = Field(min_length=1, max_length=160)
    target: ExecutionTarget = "desktop"
    surface: ExecutionSurface = "chat"
    capability_status: CapabilityStatus = "available"
    capability_reason_code: str | None = Field(default=None, max_length=96)
    model_id: str | None = Field(default=None, max_length=320)
    workspace_uri: str | None = Field(default=None, max_length=2_048)
    catalog_fingerprint: str | None = Field(default=None, max_length=256)
    # 项目生命周期的服务数量由 Runtime 在 intake 时编译并持久化。
    # 单服务项目首个 readiness 即可收官；明确的前后端项目必须分别取得
    # 两个 Host receipt，避免把“后端已启动”误报成“全栈已完成”。
    project_service_count: int = Field(default=1, ge=1, le=8)
    verification_requirements: list[VerificationRequirement] = Field(default_factory=list, max_length=8)

    @field_validator(
        "execution_context_id",
        "model_id",
        "workspace_uri",
        "capability_reason_code",
        "catalog_fingerprint",
    )
    @classmethod
    def reject_surrounding_whitespace(cls, value: str | None) -> str | None:
        if value is not None and value != value.strip():
            raise ValueError("execution context values must not contain surrounding whitespace")
        return value

    @field_validator("verification_requirements")
    @classmethod
    def require_unique_verification_requirements(
        cls, value: list[VerificationRequirement]
    ) -> list[VerificationRequirement]:
        return list(dict.fromkeys(value))


class TaskEnvelope(_TaskContextModel):
    """One authoritative active goal for a local Conversation."""

    schema_version: Literal["cowork.task-envelope.v1"] = TASK_ENVELOPE_CONTRACT_VERSION
    task_id: str = Field(min_length=1, max_length=160)
    conversation_id: str = Field(min_length=1, max_length=160)
    active_goal: str = Field(min_length=1, max_length=12_000)
    confirmed_clarifications: list[str] = Field(default_factory=list, max_length=20)
    # Structured choices preserve semantic values while the text list above
    # remains for compatibility with older snapshots and projections.
    confirmed_clarification_values: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    pending_clarification: ClarificationRequest | None = None
    pinned_constraints: list[TaskConstraint] = Field(default_factory=list, max_length=40)
    pending_deliverables: list[str] = Field(default_factory=list, max_length=20)
    module_constraints: ModuleConstraints = Field(default_factory=ModuleConstraints)
    source: Literal["runtime_intake", "legacy_session_adapter"]
    created_at: datetime
    updated_at: datetime

    @field_validator("task_id", "conversation_id", "active_goal")
    @classmethod
    def require_canonical_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("task fields must not be blank")
        return normalized

    @field_validator("confirmed_clarifications", "pending_deliverables")
    @classmethod
    def require_bounded_text_items(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        for item in value:
            normalized = item.strip()
            if not normalized or len(normalized) > 2_000:
                raise ValueError("task text items must be non-empty and bounded")
            if normalized not in result:
                result.append(normalized)
        return result

    @field_validator("confirmed_clarification_values")
    @classmethod
    def require_bounded_structured_items(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict) or len(item) > 20:
                continue
            normalized = dict(item)
            if normalized not in result:
                result.append(normalized)
        return result

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("task timestamps must include timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def require_monotonic_timestamps(self) -> "TaskEnvelope":
        if self.updated_at < self.created_at:
            raise ValueError("task updated_at must not precede created_at")
        return self


class ContextSnapshot(_TaskContextModel):
    """A versioned Runtime-owned context selection for exactly one Turn."""

    schema_version: Literal["cowork.context-snapshot.v1"] = CONTEXT_SNAPSHOT_CONTRACT_VERSION
    snapshot_id: str = Field(min_length=1, max_length=160)
    conversation_id: str = Field(min_length=1, max_length=160)
    task_id: str = Field(min_length=1, max_length=160)
    run_id: str = Field(min_length=1, max_length=160)
    turn_id: str = Field(min_length=1, max_length=160)
    revision: int = Field(ge=1)
    source: Literal["runtime_intake", "legacy_session_adapter"]
    selection_reason: Literal[
        "new_conversation",
        "active_task_follow_up",
        "execution_context_changed",
        "legacy_session_fallback",
    ]
    execution_context: ExecutionContext
    task_envelope: TaskEnvelope
    # 资产在 intake 时冻结为稳定版本，避免下一轮从文件名、聊天文本或目录
    # 猜测“刚才的文件”。正文不在这里，仍须通过 Runtime 的受控读取边界取得。
    active_assets: list[AssetRef] = Field(default_factory=list, max_length=32)
    created_at: datetime

    @field_validator("snapshot_id", "conversation_id", "task_id", "run_id", "turn_id")
    @classmethod
    def require_canonical_identifiers(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("snapshot identifiers must not be blank")
        return normalized

    @field_validator("created_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("snapshot timestamp must include timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def require_task_lineage(self) -> "ContextSnapshot":
        if self.task_envelope.task_id != self.task_id:
            raise ValueError("snapshot task_id must match task_envelope")
        if self.task_envelope.conversation_id != self.conversation_id:
            raise ValueError("snapshot conversation_id must match task_envelope")
        return self


def parse_task_envelope(payload: object) -> TaskEnvelope:
    """Parse a task contract without normalizing unknown legacy fields."""

    return TaskEnvelope.model_validate(payload)


def parse_context_snapshot(payload: object) -> ContextSnapshot:
    """Parse a context snapshot and fail closed on revision/schema drift."""

    return ContextSnapshot.model_validate(payload)


__all__ = [
    "CONTEXT_SNAPSHOT_CONTRACT_VERSION",
    "TASK_ENVELOPE_CONTRACT_VERSION",
    "CapabilityStatus",
    "ContextSnapshot",
    "ExecutionContext",
    "ExecutionSurface",
    "ExecutionTarget",
    "TaskConstraint",
    "ModuleConstraints",
    "TaskEnvelope",
    "VerificationRequirement",
    "parse_context_snapshot",
    "parse_task_envelope",
]
