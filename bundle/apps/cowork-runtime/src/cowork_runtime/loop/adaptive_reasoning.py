"""异常触发的受限交付决策。

该模块只消费 Runtime 已验证的事实，不调用工具、不修改执行状态，也不保存
模型 Prompt 或推理正文。真正的状态迁移仍由 AgentLoop/Application/Orchestrator
负责；这里仅给出一个可审计的下一步选择，并提供 checkpoint 可持久化的去重记录。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal, MutableMapping


AdaptiveAction = Literal[
    "continue",
    "local_diagnose",
    "revise_unfinished_plan",
    "wait_for_user",
    "terminalize",
]

AdaptiveTrigger = Literal[
    "tool_failure",
    "verification_failed",
    "plan_step_failure",
    "context_compaction_failed",
    "no_progress",
    "plan_precondition_failed",
    "approval_blocked",
    "login_blocked",
    "missing_input",
    "runtime_restart",
    "recovery",
]

_SCHEMA_VERSION = "cowork.runtime.adaptive-reasoning.v1"
_MAX_REASON_CODE = 120
_MAX_TOOL_NAME = 160
_MAX_RECORDS = 32
_SAFE_SIDE_EFFECTS = frozenset({"none", "intrinsic", "keyed"})


@dataclass(frozen=True)
class AdaptiveReasoningInput:
    """供策略使用的脱敏事实。

    ``incident_fingerprint`` 必须由上游用稳定错误/调用摘要生成，不能携带
    工具参数本身。字段故意采用布尔值和 reason code，避免策略依赖自然语言。
    """

    trigger: AdaptiveTrigger | None = None
    reason_code: str = ""
    tool_name: str | None = None
    error_code: str | None = None
    recovery_stage: str | None = None
    recovery_action: str | None = None
    do_not_retry: bool = False
    recovery_exhausted: bool = False
    context_compaction_failed: bool = False
    no_progress: bool = False
    approval_required: bool = False
    login_required: bool = False
    user_input_required: bool = False
    runtime_restarted: bool = False
    cancelled: bool = False
    unknown_facts: bool = False
    conflicting_facts: bool = False
    verification_failed: bool = False
    artifact_satisfied: bool = False
    plan_present: bool = False
    plan_step_id: str | None = None
    plan_step_completed: bool = False
    plan_step_side_effect: str = "none"
    plan_step_idempotent: bool = False
    plan_revision: int = 0
    max_plan_revisions: int = 1
    budget_remaining: int = 0
    local_diagnose_used: bool = False
    safe_alternative_available: bool = False
    safe_local_diagnose_available: bool = False
    incident_fingerprint: str | None = None
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class AdaptiveDecision:
    """策略输出；它是建议和证据，不是执行命令。"""

    action: AdaptiveAction
    reason_code: str
    max_tool_calls: int
    max_model_calls: int
    allowed_capabilities: tuple[str, ...] = field(default_factory=tuple)
    preserve_evidence: tuple[str, ...] = field(default_factory=tuple)
    delivery_mapping: str = "continue"
    fact_fingerprint: str = ""
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)

    def to_record(self) -> dict[str, Any]:
        """返回可写入 checkpoint/event 的安全记录。"""

        return {
            "schema_version": _SCHEMA_VERSION,
            "fact_fingerprint": self.fact_fingerprint,
            "action": self.action,
            "reason_code": self.reason_code,
            "max_tool_calls": self.max_tool_calls,
            "max_model_calls": self.max_model_calls,
            "allowed_capabilities": list(self.allowed_capabilities),
            "preserve_evidence": list(self.preserve_evidence),
            "delivery_mapping": self.delivery_mapping,
            "evidence_refs": list(self.evidence_refs),
        }

    def system_reminder(self, *, tool_name: str | None = None) -> str:
        """为唯一一次局部诊断生成强行动导向、但不含参数的 Context 指令。"""

        name = (tool_name or "当前工具")[:_MAX_TOOL_NAME]
        reason = self.reason_code[:_MAX_REASON_CODE]
        return (
            "RUNTIME_ADAPTIVE_REASONING: 当前失败已进入一次性局部诊断窗口。"
            f"工具 '{name}' 的失败事实为 '{reason}'。"
            "禁止原样重试、猜测参数或重放副作用步骤；只能使用允许的只读诊断能力，"
            "先核对当前事实，再选择不同且有边界的恢复路径。该诊断窗口用尽后，"
            "必须等待用户补充信息或诚实结束任务。"
        )


class AdaptiveReasoningPolicy:
    """确定性决策表，不执行任何外部副作用。"""

    def decide(self, facts: AdaptiveReasoningInput) -> AdaptiveDecision | None:
        """未命中异常触发器时返回 ``None``，普通轮次不增加任何成本。"""

        fingerprint = facts_fingerprint(facts)
        if facts.trigger is None:
            return None

        if facts.cancelled:
            return self._decision(facts, fingerprint, "wait_for_user", "run.cancelled", "waiting")
        if facts.do_not_retry or facts.error_code in {"policy_denied", "approval_expired"}:
            return self._decision(facts, fingerprint, "wait_for_user", "tool.authorization_required", "waiting")
        if facts.approval_required or facts.login_required or facts.user_input_required:
            reason = (
                "run.approval_required"
                if facts.approval_required
                else "run.login_required"
                if facts.login_required
                else "run.user_input_required"
            )
            return self._decision(facts, fingerprint, "wait_for_user", reason, "waiting")
        if facts.runtime_restarted:
            return self._decision(facts, fingerprint, "wait_for_user", "runtime.restarted", "waiting")
        if facts.unknown_facts or facts.conflicting_facts:
            return self._decision(facts, fingerprint, "terminalize", "facts.unknown_or_conflicting", "terminal")
        if facts.context_compaction_failed or facts.recovery_exhausted:
            return self._decision(
                facts,
                fingerprint,
                "terminalize",
                "runtime.recovery_exhausted" if facts.recovery_exhausted else "context.compaction_failed",
                "terminal",
            )
        if facts.recovery_stage == "terminate" or facts.recovery_action == "stop":
            return self._decision(facts, fingerprint, "terminalize", "recovery.restriction_exhausted", "terminal")
        if facts.recovery_stage == "restrict" or facts.no_progress:
            return self._decision(facts, fingerprint, "continue", "recovery.restricted", "continue")

        if facts.trigger in {"verification_failed", "plan_step_failure", "plan_precondition_failed"}:
            if self._revision_is_safe(facts):
                return self._decision(
                    facts,
                    fingerprint,
                    "revise_unfinished_plan",
                    "plan.revision_eligible",
                    "continue",
                    max_model_calls=1,
                    allowed_capabilities=("planner.revision",),
                )
            return self._decision(
                facts,
                fingerprint,
                "terminalize" if facts.artifact_satisfied else "wait_for_user",
                "plan.revision_not_safe",
                "terminal" if facts.artifact_satisfied else "waiting",
            )

        if facts.reason_code.startswith(("policy.", "permission.", "script.security.")):
            return self._decision(facts, fingerprint, "wait_for_user", "tool.authorization_required", "waiting")
        if facts.tool_name and facts.trigger == "tool_failure":
            if facts.safe_local_diagnose_available and not facts.local_diagnose_used and facts.budget_remaining > 0:
                return self._decision(
                    facts,
                    fingerprint,
                    "local_diagnose",
                    "tool.failure_local_diagnosis",
                    "continue",
                    max_tool_calls=1,
                    max_model_calls=1,
                    allowed_capabilities=("read_only_diagnostic",),
                )
            if facts.safe_alternative_available:
                return self._decision(
                    facts,
                    fingerprint,
                    "continue",
                    "tool.safe_alternative_available",
                    "continue",
                    max_model_calls=1,
                )
            return self._decision(facts, fingerprint, "wait_for_user", "tool.retry_not_safe", "waiting")

        return self._decision(facts, fingerprint, "terminalize", "recovery.no_safe_action", "terminal")

    @staticmethod
    def _revision_is_safe(facts: AdaptiveReasoningInput) -> bool:
        return (
            facts.plan_present
            and bool(facts.plan_step_id)
            and not facts.plan_step_completed
            and facts.plan_step_side_effect in _SAFE_SIDE_EFFECTS
            and facts.plan_step_idempotent
            and facts.plan_revision < facts.max_plan_revisions
        )

    @staticmethod
    def _decision(
        facts: AdaptiveReasoningInput,
        fingerprint: str,
        action: AdaptiveAction,
        reason_code: str,
        delivery_mapping: str,
        *,
        max_tool_calls: int = 0,
        max_model_calls: int = 0,
        allowed_capabilities: tuple[str, ...] = (),
    ) -> AdaptiveDecision:
        return AdaptiveDecision(
            action=action,
            reason_code=reason_code,
            max_tool_calls=max_tool_calls,
            max_model_calls=max_model_calls,
            allowed_capabilities=allowed_capabilities,
            preserve_evidence=tuple(item for item in (facts.incident_fingerprint, facts.plan_step_id) if item),
            delivery_mapping=delivery_mapping,
            fact_fingerprint=fingerprint,
            evidence_refs=tuple(facts.evidence_refs[:8]),
        )


def facts_fingerprint(facts: AdaptiveReasoningInput) -> str:
    """仅对策略字段做 hash，防止参数/正文意外进入 decision record。"""

    payload = {
        key: value
        for key, value in vars(facts).items()
        if key not in {"evidence_refs"}
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def record_decision(
    metadata: MutableMapping[str, Any],
    decision: AdaptiveDecision,
) -> bool:
    """去重写入安全决策记录，返回本次是否新增。"""

    records = metadata.get("adaptive_reasoning_decisions")
    if not isinstance(records, list):
        records = []
        metadata["adaptive_reasoning_decisions"] = records
    if any(
        isinstance(item, dict) and item.get("fact_fingerprint") == decision.fact_fingerprint
        for item in records
    ):
        return False
    records.append(decision.to_record())
    del records[:-_MAX_RECORDS]
    if decision.action == "local_diagnose":
        used = metadata.get("adaptive_reasoning_local_diagnose_incidents")
        if not isinstance(used, list):
            used = []
            metadata["adaptive_reasoning_local_diagnose_incidents"] = used
        if decision.preserve_evidence and decision.preserve_evidence[0] not in used:
            used.append(decision.preserve_evidence[0])
            del used[:-_MAX_RECORDS]
    return True


def has_local_diagnose_decision(metadata: MutableMapping[str, Any], incident_fingerprint: str | None) -> bool:
    """检查同一脱敏故障事件是否已使用过局部诊断窗口。"""

    if not incident_fingerprint:
        return False
    used = metadata.get("adaptive_reasoning_local_diagnose_incidents")
    return isinstance(used, list) and incident_fingerprint in used


__all__ = [
    "AdaptiveAction",
    "AdaptiveDecision",
    "AdaptiveReasoningInput",
    "AdaptiveReasoningPolicy",
    "facts_fingerprint",
    "has_local_diagnose_decision",
    "record_decision",
]
