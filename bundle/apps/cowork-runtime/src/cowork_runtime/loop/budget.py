"""Loop budget guards."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from cowork_runtime.contracts import RuntimeErrorCode

from .types import LoopCheckpoint


class LoopBudgetExceeded(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = RuntimeErrorCode.BUDGET_EXCEEDED


@dataclass(frozen=True)
class LoopBudget:
    max_model_turns: int = 8
    # 模型请求的累计 prompt + completion token 上限。它与 model turns
    # 分开计量：复杂任务可以有更多短轮次，但不会因为上下文反复回放而
    # 无界增加成本。None 仅供内部兼容测试使用，生产 profile 必须有值。
    max_model_tokens: int | None = 80_000
    # Provider length stops normally need one continuation to finish an answer.
    # Repeated truncation without a tool call or a completed response is not
    # progress, however, and must not consume the whole Run time budget.
    max_output_continuations_without_progress: int = 1
    max_tool_calls: int = 16
    max_failures: int = 3
    max_model_repairs: int = 3
    max_transient_retries: int = 2
    # 模型网关已确认返回 5xx 时，在同一 Run 内做有界恢复；不重放已完成的工具调用。
    max_model_unavailable_retries: int = 2
    model_unavailable_retry_delays_seconds: tuple[float, ...] = (1.0, 2.0)
    tool_storm_threshold: int = 2
    # 相同参数、相同文件版本的只读调用只真实执行一次。文件被成功修改后，
    # 版本会推进，允许 Agent 重新读取并验证修改结果。
    successful_read_repeat_threshold: int = 1
    # 同一 URI 的多种 read/preview/range 组合也可能形成读取风暴。超过此
    # 上限后要求模型改用 search_text、脚本或已有证据推进任务。
    successful_read_uri_limit: int = 2
    # 精准搜索是读取风暴的逃生通道，但不同 query 也必须有界；超过上限后
    # 模型应使用已返回的片段直接 edit 或创建新版本。
    successful_search_uri_limit: int = 4
    # 远程市场调研一次会占用较长的网络与模型时间。达到上限后 Runtime
    # 从下一轮工具目录移除该能力，要求 Agent 用已经返回的来源完成交付物。
    successful_research_limit: int = 4
    # 在线搜索会自动补抓正文，但重复同一结果集仍然可能形成无进展风暴。
    # 达到上限后要求模型停止重复搜索，直接使用已返回的正文证据。
    successful_web_search_repeat_threshold: int = 1
    # 不同 query 也不能无限探索；达到上限后保留已有正文并进入回答/降级阶段。
    max_successful_web_searches: int = 3
    # 同一 URI 的整体重写风暴上限（run_272787f5：模型被历史空 content 占位符
    # 误导后连续 5 次重写同一文件直到预算耗尽）。合法的"写后修"迭代远不止
    # 一两次，因此 coding profile 给出更高额度；ordinary 保持宽松默认。
    successful_write_uri_limit: int = 8
    # 对必须生成 Artifact 的任务，允许的成功只读 Python 脚本数量。超过后
    # 只抑制 output_uris=[] 的检查脚本，仍允许声明新文件的写入脚本；None
    # 保持原有行为。
    successful_read_only_script_limit: Optional[int] = None
    model_timeout_seconds: Optional[float] = None
    # 单个 Run 的累计活跃执行总时间上限。审批等待不属于活跃执行，但恢复前
    # 已消耗的时间会保留，不能通过多次 approval resume 重置预算；None 保持
    # 调用方原有的无时间限制语义。
    max_execution_seconds: Optional[float] = None


# 预算模式是 Runtime 的安全策略，不由模型或 Renderer 自由扩大。数值沿用
# 当前桌面端的保守默认，并只对明确的 Coding/项目 Run 提供更多推进空间。
BUDGET_PROFILES: dict[str, dict[str, Any]] = {
    "ordinary": {},
    "single_file_coding": {
        "max_model_turns": 24,
        "max_model_tokens": 180_000,
        "max_tool_calls": 48,
        "max_execution_seconds": 360.0,
    },
    "multi_file_coding": {
        "max_model_turns": 48,
        "max_model_tokens": 400_000,
        "max_tool_calls": 128,
        "max_failures": 5,
        "max_execution_seconds": 900.0,
    },
    "project_development": {
        # Kun 的 maxSteps=100 是步骤兜底；这里给项目 Run 足够的修复和
        # lifecycle 空间，同时用 token/time/tool 三个独立维度控制成本。
        "max_model_turns": 80,
        "max_model_tokens": 800_000,
        "max_tool_calls": 240,
        "max_failures": 6,
        "max_execution_seconds": 1_800.0,
    },
}

# coding 类 profile 的同一 URI 写入上限：multi_file/project 的合法
# "写后验证再修"迭代比单文件任务更多，按 profile 放宽而不是全局放宽。
_WRITE_LIMIT_PROFILE_OVERRIDES: dict[str, int] = {
    "multi_file_coding": 12,
    "project_development": 16,
}

_BUDGET_SNAPSHOT_VERSION = 1


def resolve_loop_budget(
    metadata: Mapping[str, Any] | None,
    base: LoopBudget,
) -> LoopBudget:
    """Resolve one bounded budget profile for a Run.

    Renderer metadata is only a profile *name*; individual limits are always
    supplied by this Runtime-owned table. Unknown profiles fail closed to the
    base budget.
    """

    profile_name = str((metadata or {}).get("runtime_budget_profile") or "ordinary")
    overrides = BUDGET_PROFILES.get(profile_name)
    if overrides is None:
        profile_name = "ordinary"
        overrides = {}
    values = {field: getattr(base, field) for field in base.__dataclass_fields__}
    values.update(overrides)
    write_limit = _WRITE_LIMIT_PROFILE_OVERRIDES.get(profile_name)
    if write_limit is not None:
        values["successful_write_uri_limit"] = write_limit
    return LoopBudget(**values)


def budget_snapshot(budget: LoopBudget, *, profile: str = "ordinary") -> dict[str, Any]:
    """Return a secret-free, JSON-safe immutable Run budget record."""

    return {
        "schema_version": _BUDGET_SNAPSHOT_VERSION,
        "profile": profile if profile in BUDGET_PROFILES else "ordinary",
        "limits": {
            field: getattr(budget, field)
            for field in budget.__dataclass_fields__
        },
    }


def budget_from_snapshot(value: object, fallback: LoopBudget) -> LoopBudget:
    """Restore a previously resolved snapshot without widening its limits."""

    if not isinstance(value, Mapping) or value.get("schema_version") != _BUDGET_SNAPSHOT_VERSION:
        return fallback
    limits = value.get("limits")
    if not isinstance(limits, Mapping):
        return fallback
    profile = str(value.get("profile") or "ordinary")
    trusted = resolve_loop_budget({"runtime_budget_profile": profile}, fallback)
    values = {field: getattr(trusted, field) for field in trusted.__dataclass_fields__}
    for field in values:
        candidate = limits.get(field)
        if candidate is None or isinstance(candidate, bool):
            continue
        # Persisted snapshots are durable input, not authority. Accept a
        # tighter value for repair/testing, but never a value wider than the
        # Runtime-owned profile (including tuple retry delays).
        trusted_value = values[field]
        if isinstance(trusted_value, tuple):
            if isinstance(candidate, (list, tuple)):
                values[field] = tuple(
                    max(0.0, min(float(item), float(limit)))
                    for item, limit in zip(candidate, trusted_value)
                    if isinstance(item, (int, float)) and not isinstance(item, bool)
                ) or trusted_value
        elif isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            if isinstance(trusted_value, (int, float)):
                values[field] = min(candidate, trusted_value)
    try:
        return LoopBudget(**values)
    except (TypeError, ValueError):
        return fallback


def assert_model_turn_allowed(checkpoint: LoopCheckpoint, budget: LoopBudget) -> None:
    if checkpoint.model_turns >= budget.max_model_turns:
        raise LoopBudgetExceeded("model turn budget exceeded")


def assert_model_token_budget_allowed(checkpoint: LoopCheckpoint, budget: LoopBudget) -> None:
    """Stop before another provider request once cumulative usage is spent."""

    if budget.max_model_tokens is not None and checkpoint.model_tokens >= budget.max_model_tokens:
        raise LoopBudgetExceeded("model token budget exceeded")


def record_model_usage(checkpoint: LoopCheckpoint, usage: object) -> int:
    """Persist trusted cumulative provider usage on the resumable checkpoint."""

    if not isinstance(usage, Mapping):
        return 0
    total = usage.get("total_tokens")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        total = (
            (prompt if isinstance(prompt, int) and not isinstance(prompt, bool) and prompt >= 0 else 0)
            + (completion if isinstance(completion, int) and not isinstance(completion, bool) and completion >= 0 else 0)
        )
    checkpoint.model_tokens += total
    checkpoint.metadata["model_tokens_used"] = checkpoint.model_tokens
    return total


def assert_tool_call_allowed(checkpoint: LoopCheckpoint, budget: LoopBudget) -> None:
    if checkpoint.tool_calls >= budget.max_tool_calls:
        raise LoopBudgetExceeded("tool call budget exceeded")


def assert_failure_allowed(checkpoint: LoopCheckpoint, budget: LoopBudget) -> None:
    if checkpoint.failures >= budget.max_failures:
        raise LoopBudgetExceeded("failure budget exceeded")
