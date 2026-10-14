"""Versioned, redacted delivery outcome contract.

Execution hosts remain the source of truth for status and evidence. This
module only normalizes those facts into a transport-neutral user conclusion.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DELIVERY_OUTCOME_CONTRACT_VERSION = "cowork.delivery-outcome.v1"
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,119}$")
_SAFE_REFERENCE = re.compile(r"^(?:artifact|evidence|event|trace|run):[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_DISCLOSURE = re.compile(
    r"(?:https?://|\b(?:bearer|api[_ -]?key|password|secret|authorization|cookie|stderr|stdout|traceback)\b|```)",
    re.IGNORECASE,
)


class DeliveryOutcomeKind(str, Enum):
    DELIVERED = "delivered"
    PARTIAL = "partial"
    WAITING_FOR_USER = "waiting_for_user"
    RECOVERABLE_FAILURE = "recoverable_failure"
    UNRECOVERABLE_FAILURE = "unrecoverable_failure"
    CANCELLED = "cancelled"


class DeliveryNextAction(str, Enum):
    NONE = "none"
    REVIEW = "review"
    PROVIDE_INPUT = "provide_input"
    APPROVE = "approve"
    RETRY = "retry"
    START_NEW_RUN = "start_new_run"


class DeliveryRetryScope(str, Enum):
    NONE = "none"
    SAME_RUN = "same_run"
    NEW_RUN = "new_run"


class _DeliveryModel(BaseModel):
    """Strict base for data crossing Runtime/renderer boundaries."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _single_line(value: object, field: str, *, max_length: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip()
    if not allow_empty and not normalized:
        raise ValueError(f"{field} must not be empty")
    if normalized != value or len(normalized) > max_length or "\n" in normalized or "\r" in normalized:
        raise ValueError(f"{field} must be a bounded single-line value")
    if _DISCLOSURE.search(normalized):
        raise ValueError(f"{field} contains disallowed diagnostic content")
    return normalized


def _safe_reason_code(value: str) -> str:
    normalized = value.strip()
    if normalized != value or not _SAFE_CODE.fullmatch(normalized):
        raise ValueError("reason_code must be a stable lowercase code")
    return normalized


def _safe_reference(value: str) -> str:
    normalized = value.strip()
    if normalized != value or not _SAFE_REFERENCE.fullmatch(normalized):
        raise ValueError("evidence references must use a controlled reference")
    return normalized


class DeliveryOutcome(_DeliveryModel):
    """The sole normalized conclusion a renderer should use for a Run."""

    schema_version: Literal["cowork.delivery-outcome.v1"] = DELIVERY_OUTCOME_CONTRACT_VERSION
    run_id: str = Field(min_length=1, max_length=256)
    outcome_kind: DeliveryOutcomeKind
    reason_code: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=1000)
    completed_items: list[str] = Field(default_factory=list, max_length=32)
    outstanding_items: list[str] = Field(default_factory=list, max_length=32)
    next_action: DeliveryNextAction
    retry_scope: DeliveryRetryScope
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)
    contract_version: Literal["cowork.delivery-outcome.v1"] = DELIVERY_OUTCOME_CONTRACT_VERSION

    @field_validator("run_id")
    @classmethod
    def _run_id_is_safe(cls, value: str) -> str:
        return _single_line(value, "run_id", max_length=256)

    @field_validator("reason_code")
    @classmethod
    def _reason_is_safe(cls, value: str) -> str:
        return _safe_reason_code(value)

    @field_validator("summary")
    @classmethod
    def _summary_is_safe(cls, value: str) -> str:
        return _single_line(value, "summary", max_length=1000)

    @field_validator("completed_items", "outstanding_items")
    @classmethod
    def _items_are_safe(cls, values: list[str], info) -> list[str]:
        normalized = [_single_line(value, info.field_name, max_length=300) for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError(f"{info.field_name} must be unique")
        return normalized

    @field_validator("evidence_refs")
    @classmethod
    def _evidence_is_safe(cls, values: list[str]) -> list[str]:
        normalized = [_safe_reference(value) for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("evidence_refs must be unique")
        return normalized

    @model_validator(mode="after")
    def _kind_invariants(self) -> "DeliveryOutcome":
        if self.outcome_kind is DeliveryOutcomeKind.DELIVERED:
            if self.outstanding_items or self.next_action is not DeliveryNextAction.NONE:
                raise ValueError("delivered outcome cannot have outstanding work")
            if self.retry_scope is not DeliveryRetryScope.NONE:
                raise ValueError("delivered outcome cannot be retryable")
        if self.outcome_kind is DeliveryOutcomeKind.WAITING_FOR_USER:
            if self.next_action not in {DeliveryNextAction.PROVIDE_INPUT, DeliveryNextAction.APPROVE}:
                raise ValueError("waiting outcome must request user input or approval")
            if self.retry_scope is not DeliveryRetryScope.SAME_RUN:
                raise ValueError("waiting outcome must remain resumable in the same Run")
        if self.outcome_kind is DeliveryOutcomeKind.CANCELLED:
            if self.next_action is not DeliveryNextAction.NONE or self.retry_scope is not DeliveryRetryScope.NONE:
                raise ValueError("cancelled outcome cannot request an automatic follow-up")
        return self


class DeliveryFacts(_DeliveryModel):
    """Minimal verified facts shared by Local, Server, and Remote adapters."""

    run_id: str = Field(min_length=1, max_length=256)
    run_status: Literal[
        "created",
        "running",
        "waiting_approval",
        "waiting_input",
        "completed",
        "failed",
        "cancelled",
        "blocked",
    ]
    has_user_content: bool = False
    artifact_required: bool = False
    artifact_satisfied: bool = False
    artifact_count: int = Field(default=0, ge=0, le=10_000)
    completion_warning: bool = False
    waiting_for: Literal["input", "approval"] | None = None
    failure_retryable: bool = False
    reason_code: str | None = Field(default=None, max_length=120)
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("run_id")
    @classmethod
    def _facts_run_id_is_safe(cls, value: str) -> str:
        return _single_line(value, "run_id", max_length=256)

    @field_validator("reason_code")
    @classmethod
    def _facts_reason_is_safe(cls, value: str | None) -> str | None:
        return _safe_reason_code(value) if value is not None else None

    @field_validator("evidence_refs")
    @classmethod
    def _facts_evidence_is_safe(cls, values: list[str]) -> list[str]:
        normalized = [_safe_reference(value) for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("evidence_refs must be unique")
        return normalized


def map_delivery_outcome(facts: DeliveryFacts) -> DeliveryOutcome:
    """Map host facts without inspecting prompts, diagnostics, or artifact bodies."""

    if facts.run_status == "cancelled":
        return _outcome(
            facts,
            DeliveryOutcomeKind.CANCELLED,
            "delivery.cancelled",
            "任务已取消，未再执行后续操作。",
            next_action=DeliveryNextAction.NONE,
            retry_scope=DeliveryRetryScope.NONE,
        )
    waiting_for = facts.waiting_for or (
        "approval" if facts.run_status == "waiting_approval" else "input"
        if facts.run_status == "waiting_input"
        else None
    )
    if waiting_for is not None:
        approval = waiting_for == "approval"
        return _outcome(
            facts,
            DeliveryOutcomeKind.WAITING_FOR_USER,
            "delivery.waiting_approval" if approval else "delivery.waiting_input",
            "任务正在等待你的授权后继续执行。" if approval else "任务正在等待你补充必要信息后继续执行。",
            outstanding_items=["等待你的授权" if approval else "等待你补充必要信息"],
            next_action=DeliveryNextAction.APPROVE if approval else DeliveryNextAction.PROVIDE_INPUT,
            retry_scope=DeliveryRetryScope.SAME_RUN,
        )
    if facts.run_status == "completed":
        if facts.artifact_required and not facts.artifact_satisfied:
            return _partial_for_incomplete_artifact(facts)
        if facts.completion_warning:
            return _outcome(
                facts,
                DeliveryOutcomeKind.PARTIAL,
                "delivery.completed_with_warnings",
                "任务已完成主要步骤，但仍有需要你确认的限制。",
                completed_items=["已保留已完成的结果"] if facts.has_user_content or facts.artifact_count else [],
                outstanding_items=["确认并处理剩余限制"],
                next_action=DeliveryNextAction.REVIEW,
                retry_scope=DeliveryRetryScope.NEW_RUN,
            )
        if facts.artifact_satisfied:
            return _outcome(
                facts,
                DeliveryOutcomeKind.DELIVERED,
                "delivery.artifact_requirements_satisfied",
                "任务已完成，所需交付物已生成并通过验证。",
                completed_items=["已生成并验证所需交付物"],
                next_action=DeliveryNextAction.NONE,
                retry_scope=DeliveryRetryScope.NONE,
            )
        if facts.has_user_content:
            return _outcome(
                facts,
                DeliveryOutcomeKind.DELIVERED,
                "delivery.completed",
                "任务已完成，结果说明已准备就绪。",
                completed_items=["已生成结果说明"],
                next_action=DeliveryNextAction.NONE,
                retry_scope=DeliveryRetryScope.NONE,
            )
        return _outcome(
            facts,
            DeliveryOutcomeKind.PARTIAL,
            "delivery.missing_user_summary",
            "任务已结束，但没有可展示的结果说明。",
            outstanding_items=["生成可展示的结果说明"],
            next_action=DeliveryNextAction.START_NEW_RUN,
            retry_scope=DeliveryRetryScope.NEW_RUN,
        )
    if facts.artifact_count > 0:
        return _partial_for_incomplete_artifact(facts)
    if facts.reason_code == "delivery.platform_unavailable":
        return _outcome(
            facts,
            DeliveryOutcomeKind.RECOVERABLE_FAILURE,
            "delivery.platform_unavailable",
            "依赖服务暂时不可用，已保留当前进度，可在服务恢复后继续此任务。",
            outstanding_items=["在依赖服务恢复后继续未完成步骤"],
            next_action=DeliveryNextAction.RETRY,
            retry_scope=DeliveryRetryScope.SAME_RUN,
        )
    if facts.reason_code == "delivery.model_output_incomplete":
        return _outcome(
            facts,
            DeliveryOutcomeKind.RECOVERABLE_FAILURE,
            "delivery.model_output_incomplete",
            "模型输出多次达到长度限制，未能形成完整结果。请精简任务后重试。",
            outstanding_items=["精简任务范围后重新执行"],
            next_action=DeliveryNextAction.RETRY,
            retry_scope=DeliveryRetryScope.NEW_RUN,
        )
    if facts.reason_code == "delivery.execution_capability_unavailable":
        return _outcome(
            facts,
            DeliveryOutcomeKind.RECOVERABLE_FAILURE,
            facts.reason_code,
            "当前任务需要的桌面执行能力不可用，未调用模型，也未执行任何本地操作。",
            outstanding_items=["连接 Desktop 并授权工作区后重试"],
            next_action=DeliveryNextAction.RETRY,
            retry_scope=DeliveryRetryScope.NEW_RUN,
        )
    if facts.failure_retryable:
        return _outcome(
            facts,
            DeliveryOutcomeKind.RECOVERABLE_FAILURE,
            "delivery.retry_available",
            "任务未能完成，但保留了可安全重试的状态。",
            outstanding_items=["重新执行未完成步骤"],
            next_action=DeliveryNextAction.RETRY,
            retry_scope=DeliveryRetryScope.NEW_RUN,
        )
    return _outcome(
        facts,
        DeliveryOutcomeKind.UNRECOVERABLE_FAILURE,
        "delivery.unrecoverable_failure",
        "任务未能完成，当前运行已安全停止。",
        outstanding_items=["根据提示补充条件后重新发起任务"],
        next_action=DeliveryNextAction.REVIEW,
        retry_scope=DeliveryRetryScope.NONE,
    )


def _partial_for_incomplete_artifact(facts: DeliveryFacts) -> DeliveryOutcome:
    completed = [f"已保留 {facts.artifact_count} 项交付物"] if facts.artifact_count else []
    return _outcome(
        facts,
        DeliveryOutcomeKind.PARTIAL,
        "delivery.artifact_requirements_incomplete",
        "任务保留了部分成果，但尚未满足全部交付要求。",
        completed_items=completed,
        outstanding_items=["完成或验证缺失的交付物"],
        next_action=DeliveryNextAction.REVIEW,
        retry_scope=DeliveryRetryScope.NEW_RUN,
    )


def _outcome(
    facts: DeliveryFacts,
    outcome_kind: DeliveryOutcomeKind,
    default_reason_code: str,
    summary: str,
    *,
    completed_items: list[str] | None = None,
    outstanding_items: list[str] | None = None,
    next_action: DeliveryNextAction,
    retry_scope: DeliveryRetryScope,
) -> DeliveryOutcome:
    return DeliveryOutcome(
        run_id=facts.run_id,
        outcome_kind=outcome_kind,
        reason_code=facts.reason_code or default_reason_code,
        summary=summary,
        completed_items=completed_items or [],
        outstanding_items=outstanding_items or [],
        next_action=next_action,
        retry_scope=retry_scope,
        evidence_refs=facts.evidence_refs,
    )


__all__ = [
    "DELIVERY_OUTCOME_CONTRACT_VERSION",
    "DeliveryFacts",
    "DeliveryNextAction",
    "DeliveryOutcome",
    "DeliveryOutcomeKind",
    "DeliveryRetryScope",
    "map_delivery_outcome",
]
