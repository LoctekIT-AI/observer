"""Minimal testable agent loop.

This module coordinates model turns and tool turns only. Host transports,
databases, files, storage adapters, and real tool implementations stay behind
ports injected by the composition layer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from cowork_contracts.crawler import (
    BrowserSessionScope,
    CRAWLER_CONTRACT_VERSION,
    CRAWLER_CRAWL_SITE_TOOL,
    CRAWLER_REVIEW_EVIDENCE_TOOLS,
    CRAWLER_REVIEW_EXTRACT_CANDIDATES_TOOL,
    CRAWLER_REVIEW_FETCH_NEXT_PAGE_TOOL,
    CRAWLER_REVIEW_INSPECT_PAGE_TOOL,
    CRAWLER_REVIEW_RENDER_PAGE_TOOL,
    CRAWLER_REVIEW_SELECT_REGION_TOOL,
    CrawlRequest,
    CrawlResult,
    CrawlStatus,
    ExtractionSchema,
    ReviewEvidenceResult,
    build_extraction_schema,
)
from cowork_contracts import (
    ClarificationOutcome,
    DeliveryFacts,
    DeliveryOutcome,
    ExecutionGateOutcome,
    map_delivery_outcome,
)
from cowork_runtime.contracts import (
    ArtifactRef,
    ArtifactFormat,
    ArtifactIntent,
    ArtifactManifest,
    ArtifactValidationStatus,
    CognitiveEvidenceRef,
    CognitiveRecord,
    RuntimeError,
    RuntimeErrorCode,
    RunStatus,
    ToolCall,
    ToolResult,
    classify_runtime_error,
    error_signature,
    digest_value,
    public_token_usage,
    public_usage_cost,
)
from cowork_runtime.contracts.enums import (
    ApprovalStatus,
    EventVisibility,
    RuntimeEventType,
    ToolCallStatus,
)
from cowork_runtime.contracts.recovery import RecoveryGuidance
from cowork_runtime.contracts.ids import new_contract_id
from cowork_runtime.capabilities.argument_aliases import (
    ToolArgumentAliasError,
    normalize_argument_aliases,
)
from cowork_runtime.persistence.in_memory import InMemoryEventStore, InMemoryStateStore
from cowork_runtime.persistence.ports import EventStore, StateStore
from cowork_runtime.telemetry import EventStoreTraceEmitter, TraceEmitter
from cowork_runtime.context.errors import ContextCompactionError
from cowork_runtime.net.public_url import OutboundUrlError, validate_public_url_syntax
from cowork_runtime.intents import (
    ArtifactVerdictAction,
    artifact_completion_instruction,
    artifact_repair_instruction,
    evaluate_artifact_terminal,
    unrequested_artifact_write_instruction,
)


def _clarification_outcome(*, turn_id: str, task_id: str, reason: str) -> dict[str, object]:
    reason_code = {
        "policy_gate": "clarification.policy_gate",
        "artifact_intent_needs_clarification": "clarification.artifact_intent",
        "asset.continuation_ambiguous": "clarification.asset_selection",
    }.get(reason, "clarification.user_question")
    # 先通过跨边界 Contract 校验，再写入 checkpoint，避免 Loop 在不同
    # 路径上手工拼出不一致的澄清状态。
    return ClarificationOutcome(
        turn_id=turn_id,
        task_id=task_id,
        reason_code=reason_code,
    ).model_dump(mode="json")


def _execution_gate_outcome(*, run_id: str, kind: str, gate_id: str, reason_code: str) -> dict[str, object]:
    normalized_kind = kind if kind in {"approval", "user_input", "desktop_takeover"} else "user_input"
    # 闸门必须是 Runtime 生成的可恢复事实；模型校验保证 kind、reason 和
    # 时间字段在 approval、用户输入和 Desktop 接管路径中保持同一合同。
    return ExecutionGateOutcome(
        run_id=run_id,
        gate_id=gate_id,
        kind=normalized_kind,
        reason_code=reason_code if reason_code.startswith("gate.") else f"gate.{normalized_kind}",
        created_at=datetime.now(timezone.utc),
    ).model_dump(mode="json")


def _failure_label_value(value: object) -> str | None:
    """有界、可打印的标识才允许进入用户可见文案。"""

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 128:
        return None
    return text if all(character.isprintable() for character in text) else None


def model_failure_subject(provider: object, model: object) -> str:
    """合成模型失败文案中的供应商/模型片段。

    Desktop 用户报障时需要知道是哪家供应商、哪个模型超时。信息未知时返回
    空串，保证历史文案逐字不变；已知时返回形如 " DeepSeek（模型 kimi-k2）"
    的片段，直接拼接在“模型供应商”之后。application/service.py 的用户
    消息映射与 Loop 内部终态文案共用这一份合成逻辑。
    """

    provider_label = _failure_label_value(provider)
    model_label = _failure_label_value(model)
    if provider_label and model_label:
        return f" {provider_label}（模型 {model_label}）"
    if provider_label:
        return f" {provider_label}"
    if model_label:
        return f"（模型 {model_label}）"
    return ""


def _loop_model_ref(checkpoint: LoopCheckpoint) -> str | None:
    """Loop 级失败时从 checkpoint 取最近可知的模型标识。

    优先取上一次成功模型轮记录的 resolved_model；auto 路由首轮失败时退回
    Desktop 提交的 requested_model。
    """

    for key in ("resolved_model", "requested_model", "model"):
        label = _failure_label_value((checkpoint.metadata or {}).get(key))
        if label:
            return label
    return None

from cowork_runtime.local_dev import (
    LOCAL_DEV_PROJECT_ACTION_SUCCEEDED_METADATA_KEY,
    LOCAL_DEV_PROJECT_EXECUTION_REPAIR_ATTEMPTS_METADATA_KEY,
    LOCAL_DEV_PROJECT_LIFECYCLE_EVIDENCE_METADATA_KEY,
    LOCAL_DEV_PROJECT_LIFECYCLE_SESSION_EVIDENCE_METADATA_KEY,
    is_code_execution,
    is_local_dev_project_source_uri,
    is_local_dev_project_action,
    local_dev_project_execution_repair_instruction,
    local_dev_project_lifecycle_missing,
    local_dev_project_lifecycle_missing_with_session,
    local_dev_project_lifecycle_progress,
    local_dev_project_lifecycle_required,
    local_dev_project_lifecycle_signal_note,
)

from .budget import (
    LoopBudget,
    LoopBudgetExceeded,
    assert_failure_allowed,
    assert_model_token_budget_allowed,
    assert_model_turn_allowed,
    assert_tool_call_allowed,
    budget_from_snapshot,
    budget_snapshot,
    record_model_usage,
    resolve_loop_budget,
)
from .artifact_delivery import append_artifact_delivery_summary, build_artifact_delivery_summary
from .cancellation import CancellationRequested, CancellationToken
from .context_builder import ContextBuilder, tool_result_payload
from .continuation import model_requested_tools, tool_failed
from .model_turn import run_model_turn
from .progress_guards import (
    grant_tool_failure_reprieve,
    excessive_uri_read_instruction,
    no_progress_instruction,
    read_only_call_signature,
    record_successful_file_mutations,
    record_successful_read_only_script,
    record_successful_research,
    record_successful_read,
    record_successful_web_search,
    record_successful_web_search_call,
    record_successful_web_search_total,
    record_successful_write,
    crawler_call_signature,
    record_successful_crawler_call,
    successful_read_only_script_count,
    successful_research_count,
    successful_read_count,
    successful_uri_read_count,
    successful_web_search_count,
    successful_web_search_call_count,
    successful_web_search_total,
    successful_crawler_call_count,
    successful_write_count,
    suppressed_duplicate_result,
    suppressed_excessive_read_only_script_result,
    suppressed_excessive_research_result,
    suppressed_excessive_uri_read_result,
    suppressed_excessive_web_search_result,
    suppressed_excessive_write_result,
    terminal_tool_failure_explanation,
    web_search_call_signature,
)
from .recovery import RecoveryManager
from .file_workspace_recovery import workspace_uri_recovery_guidance
from .modular_delivery import evaluate_modular_delivery, modular_delivery_instruction
from .adaptive_reasoning import (
    AdaptiveDecision,
    AdaptiveReasoningInput,
    AdaptiveReasoningPolicy,
    has_local_diagnose_decision,
    record_decision,
)
from .crawler_progress import CRAWLER_PROGRESS_METADATA_KEY, evaluate_crawler_progress
from .execution_trace import append_trace
from .lifecycle import TurnPhase, transition_checkpoint_lifecycle
from .public_progress import tool_completed_progress, tool_started_progress
from .tool_turn import ToolTurnRunner
from .crawler_reuse import (
    active_dataset,
    crawler_dataset_matches_call_profile,
    dataset_from_crawl_result,
    has_complete_coverage,
    is_explicit_crawler_refresh,
    is_export_request,
    is_same_source,
    merge_session_crawler_dataset,
    review_continuation_target,
    review_continuation_target_for_dataset,
)
from .review_scope import (
    REVIEW_AGENT_MAX_REPLAN_ATTEMPTS,
    REVIEW_AGENT_PHASE_DELIVERY,
    REVIEW_AGENT_PHASE_REPLAN,
    REVIEW_EVIDENCE_CRAWLER_TOOLS,
    REVIEW_EVIDENCE_ALLOWED_TOOLS_KEY,
    REVIEW_EVIDENCE_MAX_TOOL_CALLS,
    REVIEW_EVIDENCE_TOOL_CALLS_KEY,
    REVIEW_LEGACY_CRAWLER_TOOLS,
    review_agent_enabled,
    review_evidence_enabled,
)
from cowork_runtime.review_collection import (
    MAX_REVIEW_COLLECTION_TARGET,
    apply_review_collection_metadata,
    continuation_review_collection,
    initial_review_collection,
    review_collection_from_metadata,
)
from cowork_runtime.crawler.dataset_commit import (
    ReviewEvidenceCommitError,
    ReviewEvidenceDatasetCommitter,
)
from cowork_runtime.crawler.dataset_delivery import (
    crawler_dataset_csv_artifact,
    crawler_dataset_delivery_content,
)
from .types import (
    LoopCheckpoint,
    CrawlerInteractionResume,
    LoopInput,
    LoopResult,
    LoopResume,
    ModelClient,
    Policy,
    ToolCallIntent,
    ToolExecutionContext,
    ToolHost,
)

from cowork_runtime.context.projection import PROVIDER_REASONING_CONTENT_METADATA_KEY

# 循环观测日志：记录模型轮次/工具执行/终态的耗时与规模，用于定位
# "长时间只有 loading" 时到底是模型调用、工具执行还是编排等待。
_LOOP_LOGGER = logging.getLogger("cowork.runtime.loop")


def _is_client_action_proposal(result: ToolResult) -> bool:
    """识别只等待外部客户端执行的结构化动作提案。

    Client-action Skill 在服务端只返回 payload，不代表命令已经执行。
    将其作为普通成功工具继续下一轮会让模型重复派发动作并耗尽预算；
    这里把它明确建模为一次可恢复的等待点，由上层负责创建 durable action。
    """
    output = getattr(result, "output", None)
    return (
        result.status is ToolCallStatus.SUCCEEDED
        and isinstance(output, Mapping)
        and str(output.get("client_action") or "").strip() == "developer_command"
    )


_EXECUTION_ELAPSED_SECONDS_KEY = "execution_elapsed_seconds"
# Providers do not use one universal label for an output cap.  Treat only
# documented length-limit values as incomplete; an arbitrary stop reason must
# not be guessed into a continuation request.
_LENGTH_LIMIT_STOP_REASONS = frozenset({"length", "max_tokens", "max_output_tokens", "token_limit"})
_MARKET_RESEARCH_TOOL = "cowork.research.market"
_MARKET_RESEARCH_FALLBACK_TOOL = "cowork.web.search"
_WEB_SEARCH_TOOL = "cowork.web.search"
_WEB_FETCH_PAGE_TOOL = "cowork.web.fetch_page"
_WEB_CRAWL_SITE_TOOL = CRAWLER_CRAWL_SITE_TOOL
_WEB_SEARCH_FOLLOWUP_LIMIT = 3
_LOCAL_PYTHON_SCRIPT_TOOL = "cowork.script.run_python"
_LOCAL_SCRIPT_TOOLS = frozenset(
    {
        "cowork.script.run_python",
        "cowork.script.run_node",
        "cowork.script.run_bash",
    }
)
_FILE_WRITE_TARGET_URI_ALIASES = {"uri": "target_uri"}
_FILE_WRITE_TOOLS = frozenset({"cowork.file.write_text", "cowork.file.write_base64"})
_LEGACY_CRAWLER_TOOLS = frozenset({_WEB_FETCH_PAGE_TOOL, _WEB_CRAWL_SITE_TOOL})
_REVIEW_EVIDENCE_TOOLS = frozenset(CRAWLER_REVIEW_EVIDENCE_TOOLS)
_CRAWLER_TOOLS = _LEGACY_CRAWLER_TOOLS | _REVIEW_EVIDENCE_TOOLS
_CRAWLER_ARTIFACT_SCHEME = "sqlite"
_CRAWLER_QUALITY_DIAGNOSTICS = frozenset(
    {
        "crawl.extraction_empty",
        "crawl.extraction_failed",
        "crawl.extraction_partial",
        "crawl.interaction_no_change",
        "crawl.pagination_incomplete",
        "crawl.dynamic_evidence_unusable",
    }
)
_REVIEW_INTENT_MARKERS = (
    "review",
    "reviews",
    "comment",
    "comments",
    "feedback",
    "评论",
    "评价",
    "口碑",
    "kundenmeinung",
    "kundenbewertungen",
    "bewertung",
    "bewertungen",
)
_PRODUCT_INTENT_MARKERS = (
    "product",
    "商品",
    "产品",
    "item",
    "sku",
    "产品属性",
    "商品属性",
    "商品信息",
    "产品信息",
    "produkt",
    "artikel",
)
_PRODUCT_FIELD_MARKERS = (
    "brand", "品牌", "sku", "货号", "型号", "材质", "material", "颜色", "color",
    "尺寸", "规格", "size", "重量", "weight", "承重", "保修", "warranty",
    "dimensions", "长宽高", "库存", "availability", "价格", "price",
)
_RESEARCH_REPORT_INTENT_MARKERS = (
    "研报", "券商", "研究报告", "分析师报告", "broker report", "analyst report",
    "research report", "目标价", "投资评级", "评级报告",
)
_RESEARCH_REPORT_FIELD_MARKERS = (
    "评级", "rating", "目标价", "target price", "发布日期", "published", "核心观点",
    "投资逻辑", "风险", "risks", "盈利预测", "earnings forecast", "券商", "机构",
)

TERMINAL_LOOP_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
    RunStatus.BLOCKED,
}


def _execution_elapsed_seconds(checkpoint: LoopCheckpoint) -> float:
    """读取持久化的活跃执行时间；损坏的旧元数据不应阻断恢复。"""

    raw = checkpoint.metadata.get(_EXECUTION_ELAPSED_SECONDS_KEY)
    try:
        return max(0.0, float(raw or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _record_execution_elapsed(
    checkpoint: LoopCheckpoint,
    elapsed_before: float,
    started_at: float,
) -> None:
    checkpoint.metadata[_EXECUTION_ELAPSED_SECONDS_KEY] = elapsed_before + max(
        0.0,
        time.monotonic() - started_at,
    )


def _is_read_only_python_script(call: ToolCall) -> bool:
    outputs = call.arguments.get("output_uris")
    return call.tool_name == "cowork.script.run_python" and isinstance(outputs, list) and not outputs


def _crawler_extraction_target(checkpoint: LoopCheckpoint, arguments: dict[str, object]) -> str | None:
    """Infer only the small set of explicit, deterministic crawler profiles."""

    candidates: list[str] = []
    for message in checkpoint.messages:
        if str(message.get("role") or "") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            candidates.append(content.lower())
    for key in ("query", "source_type"):
        value = arguments.get(key)
        if isinstance(value, str):
            candidates.append(value.lower())
    text = "\n".join(candidates)
    if any(marker in text for marker in _REVIEW_INTENT_MARKERS):
        return "评论字段：作者、评分、内容、发布时间"
    if any(marker in text for marker in _RESEARCH_REPORT_INTENT_MARKERS) or any(
        marker in text for marker in _RESEARCH_REPORT_FIELD_MARKERS
    ):
        return next(
            (candidate for candidate in candidates if any(marker in candidate for marker in _RESEARCH_REPORT_FIELD_MARKERS)),
            "券商研报",
        )
    if any(marker in text for marker in _PRODUCT_INTENT_MARKERS) or any(marker in text for marker in _PRODUCT_FIELD_MARKERS):
        return next((candidate for candidate in candidates if any(marker in candidate for marker in _PRODUCT_FIELD_MARKERS)), "商品属性")
    return None


def _crawler_profile_from_target(target: str | None) -> str | None:
    """Resolve a recognized profile without changing a model-provided schema."""

    return build_extraction_schema(target).profile if target is not None else None


def _initial_review_target(text: str) -> int | None:
    """Read an explicit first-batch size only from a review collection request.

    Continuation parsing stays deliberately narrow because it can initiate a
    new network operation from existing session evidence. A fresh review crawl
    already has a model-selected crawler call, so allow natural phrases such
    as ``获取这个商品的 40 条评论`` while still requiring both a collection verb
    and a review subject around the numeric count.
    """

    explicit = review_continuation_target(text)
    if explicit is not None:
        return explicit
    lowered = str(text or "").strip().lower()
    if not lowered:
        return None
    match = re.search(
        r"(?:抓取|爬取|采集|获取)[^0-9]{0,32}?(?P<count>\d{1,4})\s*条?\s*(?:评论|评价|reviews?)",
        lowered,
        re.IGNORECASE,
    )
    if match is None:
        return None
    try:
        target = int(match.group("count"))
    except (TypeError, ValueError):
        return None
    return min(target, MAX_REVIEW_COLLECTION_TARGET) if target >= 1 else None


def _bare_extraction_json_schema(value: dict[str, object]) -> dict[str, object] | None:
    """Convert the model's field-map dialect into a JSON Schema object."""

    wrapper_keys = {"version", "profile", "description", "json_schema", "completion_mode", "metadata"}
    if wrapper_keys.intersection(value):
        return None
    if value.get("type") == "object":
        return dict(value)
    properties = value.get("properties")
    if isinstance(properties, dict):
        return {"type": "object", **value}
    if not value:
        return None

    def scalar_schema(raw: object) -> dict[str, object]:
        declared = str(raw or "string").strip().lower()
        if declared in {"number", "integer", "boolean", "string", "object", "array"}:
            return {"type": declared}
        return {"type": "string"}

    normalized_properties: dict[str, object] = {}
    for field, raw in value.items():
        if isinstance(raw, list):
            item_properties = {
                str(item_field): scalar_schema(item_type)
                for item in raw
                if isinstance(item, dict)
                for item_field, item_type in item.items()
            }
            item_schema: dict[str, object] = {
                "type": "object",
                "properties": item_properties,
                "additionalProperties": False,
            }
            if item_properties:
                item_schema["required"] = list(item_properties)
            normalized_properties[str(field)] = {"type": "array", "items": item_schema}
        elif isinstance(raw, dict) and raw.get("type"):
            normalized_properties[str(field)] = dict(raw)
        else:
            normalized_properties[str(field)] = scalar_schema(raw)
    return {
        "type": "object",
        "properties": normalized_properties,
        "additionalProperties": False,
    }


def _crawler_review_terminal_answer(result: ToolResult) -> dict[str, str] | None:
    """Classify review evidence that needs a bounded final response."""

    if result.status is not ToolCallStatus.SUCCEEDED or not isinstance(result.output, dict):
        return None
    output = result.output
    coverage = output.get("extraction_coverage")
    summary = output.get("summary_text")
    records = output.get("extraction_records")
    observed_count = int(coverage.get("observed_count") or 0) if isinstance(coverage, dict) else 0
    expected_count = coverage.get("expected_count") if isinstance(coverage, dict) else None
    review_records = [
        item for item in records
        if isinstance(item, dict) and str(item.get("profile") or "").lower() == "reviews"
    ] if isinstance(records, list) else []
    if (
        output.get("status") not in {"partial", "completed"}
        or not isinstance(summary, str)
        or not summary.strip()
        or not isinstance(coverage, dict)
        or not isinstance(expected_count, int)
        or expected_count <= 0
        or (output.get("status") == "partial" and observed_count >= expected_count)
        or (review_records == [] and observed_count > 0)
        or (output.get("status") == "completed" and observed_count < expected_count)
    ):
        return None
    return {
        "summary_text": summary.strip(),
        "coverage_label": str(coverage.get("label") or f"{observed_count}/{expected_count}"),
        "observed_count": str(observed_count),
        "expected_count": str(expected_count),
        "is_complete": str(observed_count >= expected_count),
    }


def _crawler_review_summary_instruction(summary: dict[str, str]) -> str:
    """Ask for one evidence-bound conclusion after a fully covered review crawl.

    完整评论记录已经在本轮工具结果中。这里允许模型做一次总结，而不是把
    覆盖率提示误当成最终用户答案；同时明确禁止再次调用工具，避免重入抓取。
    """

    return (
        "RUNTIME_CRAWLER_REVIEW_SUMMARY: The crawler has already collected complete, verified "
        f"review evidence ({summary['coverage_label']}). Answer the user's original request now "
        "with a concise summary grounded only in those records. Do not call any tools, fetch more "
        "pages, search, or claim that unobserved facts are true. State the verified coverage."
    )


def _crawler_partial_answer_instruction(summary: dict[str, str]) -> str:
    """Request one useful conclusion even when review coverage is partial.

    部分评论仍是用户已经取得的真实证据。旧逻辑直接输出覆盖率，虽然没有
    伪造内容，却让用户得不到对已提取评论的总结。这里严格提供一次无工具
    总结机会；模型试图再次抓取时由下一轮回退到确定性覆盖率说明。
    """

    return (
        "RUNTIME_CRAWLER_PARTIAL_REVIEW_SUMMARY: The crawler collected verified but incomplete "
        f"review evidence ({summary['coverage_label']}). Answer the user's original request now "
        "with a concise summary grounded only in the collected records. Do not call tools, fetch "
        "more pages, search, or imply the uncollected reviews were examined. State the verified "
        "coverage and clearly distinguish the summary from the missing records."
    )


def _crawler_partial_terminal_text(summary: dict[str, str]) -> str:
    observed = int(summary.get("observed_count") or 0)
    expected = int(summary.get("expected_count") or 0)
    if observed >= expected > 0:
        detail = f"已获取 {observed}/{expected} 条候选评论记录，字段质量仍需校验。"
    elif observed > 0:
        detail = f"已获取 {observed}/{expected} 条候选评论记录，剩余评论明细未获取。"
    else:
        detail = "当前页面未暴露可验证的单条评论内容。"
    return (
        f"{summary['summary_text']}（评论明细覆盖率：{summary['coverage_label']}）。"
        f"{detail}不会补造评论明细。"
    )


def _crawler_review_terminal_content(checkpoint: LoopCheckpoint) -> str | None:
    """Ensure the final answer still states the verified review count."""

    partial_answer = checkpoint.metadata.get("crawler_partial_answer")
    if not isinstance(partial_answer, dict):
        return None
    observed = int(partial_answer.get("observed_count") or 0)
    expected = int(partial_answer.get("expected_count") or 0)
    if observed < 0 or expected <= 0:
        return None
    coverage_label = str(partial_answer.get("coverage_label") or f"{observed}/{expected}")
    content = checkpoint.final_content.strip()
    if not content:
        return _crawler_partial_terminal_text(partial_answer)
    if coverage_label in content:
        return None
    return (
        f"{content}（评论明细覆盖率：{coverage_label}，已获取 {observed}/{expected} 条候选评论记录）"
    )


def _review_evidence_commit_denial(
    checkpoint: LoopCheckpoint,
) -> tuple[str, RuntimeError] | None:
    """Convert a Runtime-owned Dataset refusal into one truthful terminal fact."""

    commit = checkpoint.metadata.get("review_evidence_commit")
    if not isinstance(commit, Mapping) or commit.get("status") != "denied":
        return None
    reason_code = str(commit.get("reason_code") or "review.commit_denied")
    return (
        "本次评论候选未通过数据质量校验，未写入可下载的数据集。",
        RuntimeError(
            code=RuntimeErrorCode.TOOL_FAILED,
            message="review evidence dataset commit was denied",
            retryable=False,
            category="fatal",
            details={"reason_code": reason_code, "do_not_retry": True},
        ),
    )


def _pending_crawler_interaction(call: ToolCall, result: ToolResult) -> dict[str, str] | None:
    """Project the minimal safe contract Desktop needs to open a takeover window."""

    if call.tool_name not in _LEGACY_CRAWLER_TOOLS or result.status != ToolCallStatus.SUCCEEDED:
        return None
    if not isinstance(result.output, dict):
        return None
    try:
        crawl_result = CrawlResult.model_validate(result.output)
    except (TypeError, ValueError):
        return None
    pending = crawl_result.pending_interaction
    if crawl_result.status != CrawlStatus.WAITING_INPUT.value or pending is None:
        return None
    if pending.crawl_job_id != crawl_result.crawl_job_id:
        return None
    target = urlsplit(str(pending.metadata.get("url") or ""))
    allowed_origin = f"{target.scheme}://{target.netloc}" if target.scheme in {"http", "https"} and target.netloc else ""
    return {
        "tool_name": call.tool_name,
        "crawl_job_id": pending.crawl_job_id,
        "interaction_id": pending.interaction_id,
        "kind": pending.kind,
        "message": pending.message,
        "required_action": pending.required_action or "",
        "requested_url": target.geturl(),
        "session_ref": pending.session_ref or "",
        "allowed_origin": allowed_origin,
    }


@dataclass(frozen=True)
class _CrawlerRecoveryProjection:
    """Stable crawler outcome classified without reading page content or URLs."""

    error: RuntimeError
    convert_to_failed: bool = False
    block_call: bool = False


def _crawler_failure_output(value: object) -> dict[str, object]:
    """Keep only crawler control facts once a result becomes a failed ToolResult."""

    if not isinstance(value, dict):
        return {}
    diagnostics = value.get("diagnostics")
    safe_diagnostics = []
    if isinstance(diagnostics, list):
        for item in diagnostics[:16]:
            if isinstance(item, dict) and isinstance(item.get("code"), str):
                safe_diagnostics.append({"code": item["code"]})
    return {
        "status": str(value.get("status") or "failed"),
        "diagnostics": safe_diagnostics,
    }


def _crawler_recovery_projection(
    call: ToolCall,
    result: ToolResult,
) -> _CrawlerRecoveryProjection | None:
    """Map crawler contract status/diagnostic codes to bounded recovery facts.

    Crawler owns its browser strategy and yields partial evidence as successful
    output. This bridge does not inspect diagnostics text or page content; it
    only turns stable status/code facts into the generic recovery vocabulary.
    """

    if call.tool_name not in _LEGACY_CRAWLER_TOOLS or result.status is not ToolCallStatus.SUCCEEDED:
        return None
    if not isinstance(result.output, dict):
        return None
    try:
        crawl_result = CrawlResult.model_validate(result.output)
    except (TypeError, ValueError):
        return None

    diagnostic_codes = {item.code for item in crawl_result.diagnostics}
    if crawl_result.status == CrawlStatus.WAITING_INPUT.value and crawl_result.pending_interaction is not None:
        kind = crawl_result.pending_interaction.kind
        reason_code = (
            "crawler.interaction.captcha_required"
            if kind == "captcha"
            else "crawler.interaction.login_required"
            if kind in {"login", "permission", "mfa"}
            else "crawler.interaction.user_action_required"
        )
        return _CrawlerRecoveryProjection(
            error=RuntimeError(
                code=RuntimeErrorCode.TOOL_FAILED,
                message="crawler is waiting for user input",
                category="fatal",
                details={"reason_code": reason_code, "do_not_retry": True},
            ),
        )
    # Runtime-generated search follow-ups deliberately return a completed
    # ``public_source_skipped`` page when robots denies one candidate. That is
    # a successful omission, not a fatal crawler failure; the loop must move
    # on to the remaining candidates instead of blocking the whole research.
    if "crawl.robots_denied" in diagnostic_codes and not any(
        isinstance(page.metadata, dict) and page.metadata.get("public_source_skipped") is True
        for page in crawl_result.pages
    ):
        return _CrawlerRecoveryProjection(
            error=RuntimeError(
                code=RuntimeErrorCode.TOOL_FAILED,
                message="crawler source disallows automated access",
                category="fatal",
                details={"reason_code": "crawler.policy.robots_denied"},
            ),
            convert_to_failed=True,
            block_call=True,
        )
    if "crawl.session_scope_denied" in diagnostic_codes:
        return _CrawlerRecoveryProjection(
            error=RuntimeError(
                code=RuntimeErrorCode.TOOL_FAILED,
                message="crawler session scope requires user action",
                category="fatal",
                details={"reason_code": "crawler.policy.session_scope_denied", "do_not_retry": True},
            ),
        )
    if "crawl.rate_limited" in diagnostic_codes:
        return _CrawlerRecoveryProjection(
            error=RuntimeError(
                code=RuntimeErrorCode.TOOL_FAILED,
                message="crawler source is rate limited",
                category="model_repairable",
                details={"reason_code": "crawler.provider.rate_limited"},
            ),
            convert_to_failed=True,
            block_call=True,
        )
    source_failures = {
        "crawl.timeout": (
            "crawler.source.timeout",
            "crawler source response timed out",
        ),
        "crawl.network_error": (
            "crawler.source.network_error",
            "crawler source network request failed",
        ),
        "crawl.http_error": (
            "crawler.source.http_error",
            "crawler source returned an HTTP error",
        ),
    }
    for diagnostic_code, (reason_code, message) in source_failures.items():
        if diagnostic_code in diagnostic_codes:
            # A source failure must block only this canonical resource.  The
            # next model turn (and search-result follow-up) may still use
            # independent candidates instead of treating one slow page as a
            # Run-wide crawler outage.
            return _CrawlerRecoveryProjection(
                error=RuntimeError(
                    code=RuntimeErrorCode.TOOL_FAILED,
                    message=message,
                    category="model_repairable",
                    details={"reason_code": reason_code},
                ),
                convert_to_failed=True,
                block_call=True,
            )
    if crawl_result.status == CrawlStatus.FAILED.value:
        return _CrawlerRecoveryProjection(
            error=RuntimeError(
                code=RuntimeErrorCode.TOOL_FAILED,
                message="crawler did not return usable source evidence",
                # 失败状态表示 Crawler 已耗尽自己的浏览器/提取策略；把它
                # 再交给模型只会重复同一 URL，最后变成 execution budget
                # exceeded，掩盖真正原因。
                category="fatal",
                details={"reason_code": "crawler.provider.unavailable", "do_not_retry": True},
            ),
            convert_to_failed=True,
            block_call=True,
        )
    recovery = crawl_result.metadata.get("recovery") if isinstance(crawl_result.metadata, dict) else None
    stop_reason = recovery.get("stop_reason") if isinstance(recovery, dict) else None
    if stop_reason == "recovery_not_better" and diagnostic_codes.intersection(_CRAWLER_QUALITY_DIAGNOSTICS):
        return _CrawlerRecoveryProjection(
            error=RuntimeError(
                code=RuntimeErrorCode.TOOL_FAILED,
                message="crawler quality recovery did not improve the verified evidence",
                category="model_repairable",
                details={"reason_code": "crawler.quality.insufficient"},
            ),
            block_call=True,
        )
    return None


def _is_crawler_artifact_file_read(call: ToolCall) -> bool:
    if call.tool_name not in {
        "cowork.file.read_file",
        "cowork.file.preview",
        "cowork.file.read_range",
        "cowork.file.stat",
    }:
        return False
    uri = call.arguments.get("uri")
    if not isinstance(uri, str):
        return False
    parsed = urlsplit(uri)
    return parsed.scheme.lower() == _CRAWLER_ARTIFACT_SCHEME and parsed.netloc.lower() == "artifact"


def _requires_artifact(checkpoint: LoopCheckpoint) -> bool:
    try:
        return ArtifactIntent.model_validate(checkpoint.metadata.get("artifact_intent")).require_artifact
    except (TypeError, ValueError):
        return False


def _tool_progress_message(tool_name: str) -> str:
    """Return a safe, user-facing stage label without exposing tool arguments."""

    if tool_name == "cowork.research.market":
        return "正在收集与核对公开资料。"
    if tool_name == _WEB_SEARCH_TOOL:
        return "正在搜索并准备打开候选页面。"
    if tool_name == _WEB_FETCH_PAGE_TOOL:
        return "正在打开候选网页并读取正文。"
    if tool_name.startswith("cowork.file."):
        return "正在处理已授权的本地文件。"
    if tool_name == "cowork.script.run_python":
        return "正在准备生成请求的文件。"
    return "正在准备必要的执行步骤。"


def _model_turn_completion_message(tool_calls: Sequence[ToolCallIntent]) -> str:
    """用已确认的下一步描述模型回合，不公开模型自由文本。

    Provider 内容可能包含私有推理或尚未执行的猜测，不能进入 Think。工具意图
    是已通过策略检查的结构化事实；这里只复用无参数的受控标签，让模型完成
    阶段具备信息量，同时保持公开过程契约安全。
    """

    labels: list[str] = []
    for intent in tool_calls:
        # 该函数只读取工具身份并返回受控公开标签；ToolCallIntent 与 ToolCall
        # 都具有 tool_name/arguments，因此不需要构造一次未执行的 ToolCall。
        label = str(tool_started_progress(intent)["title"]).strip()
        if label and label not in labels:
            labels.append(label)
    if not labels:
        return "本轮分析已完成，正在整理可交付结果。"
    next_actions = "、".join(labels[:3])
    suffix = "等步骤" if len(labels) > 3 else ""
    return f"本轮分析已完成，接下来将{next_actions}{suffix}。"


def _local_script_security_reason(call: ToolCall, error: RuntimeError) -> str | None:
    """Return a trusted script-boundary code without reading raw script output."""

    if call.tool_name not in _LOCAL_SCRIPT_TOOLS:
        return None
    if not isinstance(error.details, dict):
        return None
    if error.details.get("do_not_retry") is True:
        reason_code = error.details.get("reason_code")
        return str(reason_code) if isinstance(reason_code, str) else "script.security.denied"
    # ISSUE-057 already persisted this safe exception classifier. Preserve
    # compatibility for checkpoints and third-party hosts produced before
    # ISSUE-058 added script.* reason codes; raw stderr is never consulted.
    legacy_reason_codes = {
        "PermissionError": "script.security.permission_denied",
        "ERR_ACCESS_DENIED": "script.security.permission_denied",
        "MODULE_NOT_ALLOWED": "script.security.permission_denied",
        "ModuleNotAllowedError": "script.security.permission_denied",
    }
    return legacy_reason_codes.get(error.details.get("script_failure_kind"))


def _is_local_script_security_failure(call: ToolCall, error: RuntimeError) -> bool:
    """Security stops use only source-classified codes, never raw script output."""

    return _local_script_security_reason(call, error) is not None


def _normalize_local_script_security_error(call: ToolCall, error: RuntimeError) -> RuntimeError:
    """Upgrade legacy safe failure kinds before recovery guidance is generated."""

    reason_code = _local_script_security_reason(call, error)
    if reason_code is None or error.details.get("do_not_retry") is True:
        return error
    return error.model_copy(
        update={
            "details": {
                **error.details,
                "reason_code": reason_code,
                "do_not_retry": True,
            }
        },
        deep=True,
    )


def _output_chunks(content: str, chunk_size: int = 48) -> List[str]:
    """Split completed provider output into bounded, lossless display deltas."""

    text = str(content or "")
    return [text[index:index + chunk_size] for index in range(0, len(text), chunk_size) if text[index:index + chunk_size]]


class AgentLoop:
    """Drive one checkpoint through model/tool continuations to a turn result.

    Plan graphs and cross-step scheduling belong to Orchestrator. In-memory
    stores are test defaults; production composition injects SQLite-backed
    ports.
    """

    def __init__(
        self,
        *,
        model_client: ModelClient,
        tool_host: ToolHost,
        policy: Policy,
        state_store: Optional[StateStore] = None,
        event_store: Optional[EventStore] = None,
        budget: Optional[LoopBudget] = None,
        context_builder: Optional[ContextBuilder] = None,
        trace_emitter: Optional[TraceEmitter] = None,
        heartbeat_interval_seconds: float = 5.0,
        adaptive_reasoning_policy: Optional[AdaptiveReasoningPolicy] = None,
        review_evidence_committer: ReviewEvidenceDatasetCommitter | None = None,
        resolve_authorized_directory_uri: Callable[[str], str | None] | None = None,
    ) -> None:
        self.model_client = model_client
        self.state_store = state_store or InMemoryStateStore()
        self.event_store = event_store or InMemoryEventStore()
        self.trace_emitter = trace_emitter or EventStoreTraceEmitter(self.event_store)
        self._base_budget = budget or LoopBudget()
        # ContextVar 让并行 Run 各自持有解析后的预算，避免把一个 Run 的
        # profile 临时写回共享 AgentLoop，造成另一 Run 被意外放宽或收紧。
        self._run_budget: ContextVar[LoopBudget | None] = ContextVar(
            "cowork_runtime_run_budget", default=None
        )
        self.context_builder = context_builder or ContextBuilder()
        self.heartbeat_interval_seconds = max(0.01, heartbeat_interval_seconds)
        self.tool_runner = ToolTurnRunner(tool_host, policy)
        self.recovery_manager = RecoveryManager()
        self.adaptive_reasoning_policy = adaptive_reasoning_policy or AdaptiveReasoningPolicy()
        # This is deliberately an internal Runtime service, not a ToolHost
        # capability.  The model may select an evidence action but it can
        # never pass records or an evidence ref to a Dataset write tool.
        self.review_evidence_committer = review_evidence_committer
        # The callback is backed by the Desktop Host grant registry in the
        # local composition. It returns no value when a Run's persisted URI is
        # stale, a file-only grant, or otherwise no longer authorized.
        self.resolve_authorized_directory_uri = resolve_authorized_directory_uri
        self._active_tokens: Dict[str, CancellationToken] = {}
        self._active_tokens_lock = RLock()
        self._terminal_lock = RLock()

    @property
    def budget(self) -> LoopBudget:
        return self._run_budget.get() or self._base_budget

    @budget.setter
    def budget(self, value: LoopBudget) -> None:
        self._base_budget = value

    def _transition_turn(
        self,
        checkpoint: LoopCheckpoint,
        phase: TurnPhase,
        *,
        tool_call_id: str | None = None,
        increment_attempt: bool = False,
        payload: dict[str, Any] | None = None,
        status: str | None = None,
    ) -> None:
        """Persist one lifecycle transition and its bounded execution fact."""

        transition_checkpoint_lifecycle(
            checkpoint.metadata,
            turn_id=checkpoint.run_id,
            phase=phase,
            tool_call_id=tool_call_id,
            increment_attempt=increment_attempt,
        )
        append_trace(
            checkpoint.metadata,
            kind=phase.value,
            run_id=checkpoint.run_id,
            turn_id=checkpoint.run_id,
            payload=payload,
            status=status,
        )

    def _record_crawler_progress(self, checkpoint: LoopCheckpoint, call: ToolCall, result: ToolResult) -> None:
        progress = evaluate_crawler_progress(
            call,
            result,
            checkpoint.metadata.get(CRAWLER_PROGRESS_METADATA_KEY),
        )
        if progress is not None:
            checkpoint.metadata[CRAWLER_PROGRESS_METADATA_KEY] = progress
            append_trace(
                checkpoint.metadata,
                kind="crawler_progress",
                run_id=checkpoint.run_id,
                turn_id=checkpoint.run_id,
                payload={
                    "tool_name": call.tool_name,
                    "status": progress["status"],
                    "new_items": progress["new_items"],
                    "coverage": progress["coverage"],
                    "fingerprint": progress["fingerprint"],
                },
                status=progress["status"],
            )

    def _restore_policy_state(self, checkpoint: LoopCheckpoint) -> None:
        restore = getattr(self.tool_runner.policy, "restore_grants", None)
        if callable(restore):
            restore(checkpoint.metadata)

    def _persist_policy_state(self, checkpoint: LoopCheckpoint) -> None:
        export = getattr(self.tool_runner.policy, "export_grants", None)
        if callable(export):
            checkpoint.metadata["approval_grants"] = export()

    async def run(
        self,
        loop_input: LoopInput,
        *,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> LoopResult:
        token = cancellation_token or CancellationToken()
        checkpoint_key = loop_input.checkpoint_key or loop_input.run_id
        self._register_active_token(checkpoint_key, token)
        checkpoint = self.context_builder.new_checkpoint(loop_input)
        # 首次进入 Run 时固化 Runtime 预算快照；恢复/审批续接优先使用
        # 已持久化快照，避免用户通过后续消息改变原 Run 的硬上限。
        persisted_snapshot = checkpoint.metadata.get("budget_snapshot")
        if isinstance(persisted_snapshot, dict):
            effective_budget = budget_from_snapshot(persisted_snapshot, self._base_budget)
        else:
            profile = str(checkpoint.metadata.get("runtime_budget_profile") or "ordinary")
            effective_budget = resolve_loop_budget(checkpoint.metadata, self._base_budget)
            checkpoint.metadata["budget_snapshot"] = budget_snapshot(
                effective_budget,
                profile=profile,
            )
            self.state_store.save(checkpoint)
        budget_token = self._run_budget.set(effective_budget)
        checkpoint.status = RunStatus.RUNNING
        self._clear_nonterminal_delivery_outcome(checkpoint)
        # ``LoopInput.metadata`` may contain renderer/runtime request data and
        # is not a grant authority.  Grant state is only restored from a
        # durable checkpoint during ``resume``; never accept a fresh Run's
        # user-supplied ``approval_grants`` field.
        checkpoint.metadata.pop("approval_grants", None)
        self._transition_turn(checkpoint, TurnPhase.CREATED, status=RunStatus.RUNNING.value)
        self.state_store.save(checkpoint)
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(checkpoint, token))
        try:
            token.raise_if_cancelled()
            if _owns_run_lifecycle(checkpoint):
                self.trace_emitter.emit(
                    checkpoint.run_id,
                    RuntimeEventType.RUN_STARTED,
                    {"session_id": checkpoint.session_id},
                    trace_id=_trace_id(checkpoint),
                )
            diagnostics = checkpoint.metadata.get("context_diagnostics")
            if diagnostics:
                self.trace_emitter.emit(
                    checkpoint.run_id,
                    RuntimeEventType.ERROR_RAISED,
                    {
                        "error": {
                            "code": "context_repair",
                            "message": "Invalid historical tool messages were excluded from model context.",
                            "diagnostics": diagnostics,
                        }
                    },
                    visibility=EventVisibility.DEBUG,
                    trace_id=_trace_id(checkpoint),
                )
            continuation = self._session_review_continuation(checkpoint)
            if continuation is not None:
                return await self._run_with_execution_budget(
                    checkpoint,
                    lambda: self._run_session_review_continuation(checkpoint, continuation, token),
                )
            return await self._run_with_execution_budget(
                checkpoint,
                lambda: self._drive(checkpoint, token),
            )
        except CancellationRequested as error:
            return self._finish(
                checkpoint,
                RunStatus.CANCELLED,
                error=_cancellation_error(error.reason),
            )
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
            self._unregister_active_token(checkpoint_key, token)
            self._run_budget.reset(budget_token)

    async def resume(
        self,
        resume: LoopResume,
        *,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> LoopResult:
        token = cancellation_token or CancellationToken()
        checkpoint_key = resume.checkpoint_key or resume.run_id
        checkpoint = self._require_checkpoint(checkpoint_key)
        persisted_snapshot = checkpoint.metadata.get("budget_snapshot")
        budget_token = self._run_budget.set(budget_from_snapshot(persisted_snapshot, self._base_budget))
        self._restore_policy_state(checkpoint)
        self._transition_turn(checkpoint, TurnPhase.RESUMED, status=RunStatus.RUNNING.value)
        self._register_active_token(checkpoint_key, token)
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(checkpoint, token))
        try:
            token.raise_if_cancelled()
            if resume.decision == ApprovalStatus.DENIED:
                error = self._reject_pending_approval(
                    checkpoint,
                    resume,
                    status=ApprovalStatus.DENIED,
                    error_code=RuntimeErrorCode.POLICY_DENIED,
                    default_message="approval denied",
                )
                return self._finish(checkpoint, RunStatus.BLOCKED, error=error)
            if resume.decision == ApprovalStatus.EXPIRED:
                error = self._reject_pending_approval(
                    checkpoint,
                    resume,
                    status=ApprovalStatus.EXPIRED,
                    error_code=RuntimeErrorCode.APPROVAL_EXPIRED,
                    default_message="approval expired",
                )
                return self._finish(checkpoint, RunStatus.BLOCKED, error=error)
            if resume.decision not in {
                ApprovalStatus.ALLOWED_ONCE,
                ApprovalStatus.ALLOWED_SESSION,
                ApprovalStatus.ALLOWED_PROJECT,
            }:
                error = RuntimeError(code=RuntimeErrorCode.CANCELLED, message="run cancelled")
                return self._finish(checkpoint, RunStatus.CANCELLED, error=error)

            pending_call = checkpoint.pending_tool_call
            pending_approval = checkpoint.pending_approval
            if pending_call is None or pending_approval is None:
                error = RuntimeError(
                    code=RuntimeErrorCode.INVALID_REQUEST,
                    message="no pending approval to resume",
                )
                return self._finish(checkpoint, RunStatus.BLOCKED, error=error)

            pending_approval.status = resume.decision
            approval_context = ToolExecutionContext(
                run_id=checkpoint.run_id,
                session_id=checkpoint.session_id,
                metadata=dict(checkpoint.metadata),
                cancellation_token=token,
            )
            record_approval = getattr(self.tool_runner.policy, "record_approval", None)
            if callable(record_approval):
                record_approval(resume.decision, pending_call, approval_context)
                self._persist_policy_state(checkpoint)
            checkpoint.pending_tool_call = None
            checkpoint.pending_approval = None
            checkpoint.metadata.pop("execution_gate", None)
            checkpoint.status = RunStatus.RUNNING
            self._clear_nonterminal_delivery_outcome(checkpoint)
            self.state_store.save(checkpoint)
            self.trace_emitter.emit(
                checkpoint.run_id,
                RuntimeEventType.APPROVAL_RESOLVED,
                {
                    "approval_id": pending_approval.approval_id,
                    "status": resume.decision.value,
                },
                trace_id=_trace_id(checkpoint),
            )
            self.trace_emitter.emit(
                checkpoint.run_id,
                RuntimeEventType.PIPELINE_STAGE,
                {
                    "stage": "execution_gate",
                    "status": "resumed",
                    "gate_id": pending_approval.approval_id,
                },
                visibility=EventVisibility.DEBUG,
                trace_id=_trace_id(checkpoint),
            )
            async def continue_after_approval() -> LoopResult:
                tool_results: List[ToolResult] = []
                outcome = await self._execute_tool(
                    checkpoint,
                    pending_call,
                    token,
                    approval_already_granted=True,
                )
                if outcome.tool_result is not None:
                    tool_results.append(outcome.tool_result)
                tool_results.extend(outcome.followup_results)
                if outcome.terminal_result is not None:
                    return _with_tool_results(outcome.terminal_result, tool_results)
                batch_outcome = await self._continue_pending_tool_batch(checkpoint, token)
                tool_results.extend(batch_outcome.followup_results)
                if batch_outcome.terminal_result is not None:
                    return _with_tool_results(batch_outcome.terminal_result, tool_results)
                return await self._drive(checkpoint, token)

            return await self._run_with_execution_budget(
                checkpoint,
                continue_after_approval,
            )
        except CancellationRequested as error:
            return self._finish(
                checkpoint,
                RunStatus.CANCELLED,
                error=_cancellation_error(error.reason),
            )
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
            self._unregister_active_token(checkpoint_key, token)
            self._run_budget.reset(budget_token)

    async def resume_crawler_interaction(
        self,
        resume: CrawlerInteractionResume,
        *,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> LoopResult:
        """Continue the original crawler tool call after Desktop user takeover.

        A crawler login barrier is not an approval: the tool call is retained
        in the checkpoint and replayed with the same crawl job ID.  This keeps
        tool and time budgets attached to the original Run and never accepts
        browser secrets over the Runtime HTTP boundary.
        """

        token = cancellation_token or CancellationToken()
        checkpoint = self._require_checkpoint(resume.run_id)
        self._restore_policy_state(checkpoint)
        self._register_active_token(resume.run_id, token)
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(checkpoint, token))
        try:
            token.raise_if_cancelled()
            pending_call = checkpoint.pending_tool_call
            pending = checkpoint.metadata.get("crawler_pending_interaction")
            if checkpoint.status != RunStatus.WAITING_INPUT or pending_call is None or not isinstance(pending, dict):
                return self._finish(
                    checkpoint,
                    RunStatus.BLOCKED,
                    error=RuntimeError(
                        code=RuntimeErrorCode.INVALID_REQUEST,
                        message="no pending crawler interaction to resume",
                    ),
                )
            if pending_call.tool_name not in _CRAWLER_TOOLS:
                return self._finish(
                    checkpoint,
                    RunStatus.BLOCKED,
                    error=RuntimeError(
                        code=RuntimeErrorCode.INVALID_REQUEST,
                        message="pending input is not a crawler interaction",
                    ),
                )
            if (
                str(pending.get("crawl_job_id") or "") != resume.crawl_job_id
                or str(pending.get("interaction_id") or "") != resume.interaction_id
            ):
                return self._finish(
                    checkpoint,
                    RunStatus.BLOCKED,
                    error=RuntimeError(
                        code=RuntimeErrorCode.INVALID_REQUEST,
                        message="crawler interaction does not match the pending crawl job",
                    ),
                )

            arguments = dict(pending_call.arguments)
            request = CrawlRequest.model_validate(arguments)
            allowed_origin = str(pending.get("allowed_origin") or "").strip()
            if not allowed_origin:
                return self._finish(
                    checkpoint,
                    RunStatus.BLOCKED,
                    error=RuntimeError(
                        code=RuntimeErrorCode.INVALID_REQUEST,
                        message="pending crawler interaction has no allowed origin",
                    ),
                )
            resumed_metadata = dict(arguments.get("metadata") if isinstance(arguments.get("metadata"), dict) else {})
            # The visible Desktop profile remains Main-owned. The Runtime gets
            # only a sanitized same-origin page snapshot as evidence.
            resumed_metadata["desktop_interaction_resumed"] = True
            snapshot = resume.metadata.get("desktop_interaction_snapshot")
            if isinstance(snapshot, dict):
                resumed_metadata["desktop_interaction_snapshot"] = dict(snapshot)
            # ``CrawlRequest`` is the shared internal contract and carries
            # defaults such as allowed_domains. A tool's public schema is more
            # narrow (fetch_page does not accept those fields), so replay the
            # original tool arguments and update only its supported fields.
            resumed_arguments = {
                **arguments,
                "session_ref": resume.session_ref,
                "metadata": resumed_metadata,
            }
            resumed_call = pending_call.model_copy(
                update={"arguments": resumed_arguments},
                deep=True,
            )
            checkpoint.pending_tool_call = None
            checkpoint.metadata.pop("crawler_pending_interaction", None)
            checkpoint.metadata.pop("execution_gate", None)
            checkpoint.status = RunStatus.RUNNING
            self._clear_nonterminal_delivery_outcome(checkpoint)
            self.state_store.save(checkpoint)
            self.trace_emitter.emit(
                checkpoint.run_id,
                RuntimeEventType.CRAWLER_INTERACTION_RESUMED,
                {
                    "crawl_job_id": resume.crawl_job_id,
                    "interaction_id": resume.interaction_id,
                    "session_ref_present": True,
                },
                trace_id=_trace_id(checkpoint),
            )
            self.trace_emitter.emit(
                checkpoint.run_id,
                RuntimeEventType.PIPELINE_STAGE,
                {
                    "stage": "execution_gate",
                    "status": "resumed",
                    "gate_id": resume.interaction_id,
                },
                visibility=EventVisibility.DEBUG,
                trace_id=_trace_id(checkpoint),
            )

            async def continue_after_interaction() -> LoopResult:
                outcome = await self._execute_tool(
                    checkpoint,
                    resumed_call,
                    token,
                    approval_already_granted=True,
                )
                if outcome.terminal_result is not None:
                    return outcome.terminal_result
                return await self._drive(checkpoint, token)

            return await self._run_with_execution_budget(checkpoint, continue_after_interaction)
        except CancellationRequested as error:
            return self._finish(
                checkpoint,
                RunStatus.CANCELLED,
                error=_cancellation_error(error.reason),
            )
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
            self._unregister_active_token(resume.run_id, token)

    def cancel(self, run_id: str, reason: Optional[str] = None, *, checkpoint_key: Optional[str] = None) -> LoopResult:
        key = checkpoint_key or run_id
        with self._active_tokens_lock:
            token = self._active_tokens.get(key)
        if token is not None:
            token.cancel(reason)
        checkpoint = self._require_checkpoint(key)
        error = _cancellation_error(reason)
        return self._finish(checkpoint, RunStatus.CANCELLED, error=error)

    async def _run_with_execution_budget(
        self,
        checkpoint: LoopCheckpoint,
        operation_factory: Callable[[], Awaitable[LoopResult]],
    ) -> LoopResult:
        """在 Loop 所属边界强制每个 Run 的累计活跃执行时限。

        审批等待不占用时间，但审批前已经花掉的模型、工具执行时间不能在
        ``resume`` 时归零；否则每次审批恢复都能绕过同一 Run 的总预算。
        """

        total_budget = self.budget.max_execution_seconds
        if total_budget is None:
            return await operation_factory()
        total_budget = max(0.001, total_budget)
        elapsed_before = _execution_elapsed_seconds(checkpoint)
        timeout = total_budget - elapsed_before
        if timeout <= 0:
            return self._finish_execution_budget(checkpoint, total_budget)

        started_at = time.monotonic()
        elapsed_recorded = False
        try:
            return await asyncio.wait_for(operation_factory(), timeout=timeout)
        except asyncio.TimeoutError:
            _record_execution_elapsed(checkpoint, elapsed_before, started_at)
            elapsed_recorded = True
            return self._finish_execution_budget(checkpoint, total_budget)
        finally:
            # 每次 run/resume 返回时立即落盘，确保下一次审批恢复或 sidecar
            # 重启不会重新获得已消耗的执行时间。
            if not elapsed_recorded:
                _record_execution_elapsed(checkpoint, elapsed_before, started_at)
            self.state_store.save(checkpoint)

    def _finish_execution_budget(
        self,
        checkpoint: LoopCheckpoint,
        total_budget: float,
    ) -> LoopResult:
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            {
                "stage": "execution_budget",
                "status": "exhausted",
                "timeout_seconds": total_budget,
                "elapsed_seconds": _execution_elapsed_seconds(checkpoint),
            },
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
        )
        return self._finish_for_budget(
            checkpoint,
            LoopBudgetExceeded("run execution time budget exceeded"),
        )

    async def _drive(
        self,
        checkpoint: LoopCheckpoint,
        cancellation_token: CancellationToken,
    ) -> LoopResult:
        tool_results: List[ToolResult] = []

        def finish(
            status: RunStatus,
            *,
            error: Optional[RuntimeError] = None,
        ) -> LoopResult:
            """保留本次循环已完成的工具结果，供上层继续处理客户端动作。"""
            return self._finish(
                checkpoint,
                status,
                error=error,
                tool_results=list(tool_results),
            )

        while True:
            cancellation_token.raise_if_cancelled()
            try:
                assert_model_turn_allowed(checkpoint, self.budget)
                # 模型轮次和 token 是两个独立预算：复杂 Coding 可以拥有更多
                # 短轮次，但不能通过不断回放历史上下文无限扩大单个 Run 成本。
                assert_model_token_budget_allowed(checkpoint, self.budget)
            except LoopBudgetExceeded as exc:
                return self._finish_for_budget(checkpoint, exc)

            self._emit_user_progress(
                checkpoint,
                phase="planning",
                title="分析任务",
                content=(
                    "正在分析你的需求并规划下一步。"
                    if checkpoint.model_turns == 0
                    else "正在结合已完成的步骤继续处理。"
                ),
                # 规划是已经发生的阶段事实；将唯一的 running 状态留给
                # 当前模型轮次，避免首屏同时出现两个 loading 指示器。
                status="completed",
                # 规划是 Run 级阶段事实；后续模型轮次只更新同一条阶段行，
                # 不把“正在分析/继续处理”不断追加成重复列表。
                step_id="run-planning",
                message_source="runtime",
            )

            self._transition_turn(
                checkpoint,
                TurnPhase.MODEL_ATTEMPT,
                increment_attempt=True,
                status=RunStatus.RUNNING.value,
            )
            self.state_store.save(checkpoint)

            reasoning_event = self.trace_emitter.emit(
                checkpoint.run_id,
                RuntimeEventType.AGENT_REASONING,
                {
                    "status": RunStatus.RUNNING.value,
                    "phase": "model_turn",
                    "model_turn": checkpoint.model_turns + 1,
                    "tool_calls": checkpoint.tool_calls,
                    "message": "Model turn is selecting the next response or tool call.",
                },
                visibility=EventVisibility.DEBUG,
                trace_id=_trace_id(checkpoint),
            )
            model_started_event = self.trace_emitter.emit(
                checkpoint.run_id,
                RuntimeEventType.PIPELINE_STAGE,
                {"stage": "model", "status": "started"},
                visibility=EventVisibility.DEBUG,
                trace_id=_trace_id(checkpoint),
            )
            model_step_id = f"model-turn-{checkpoint.model_turns + 1}"
            self._emit_user_progress(
                checkpoint,
                phase="model",
                title="等待模型",
                content="正在等待模型分析并返回下一步结果。",
                step_id=model_step_id,
                message_source="runtime",
            )
            try:
                model_turn_started_at = time.monotonic()
                model_context = self.context_builder.build(checkpoint, cancellation_token)
                self._emit_context_compaction(checkpoint)
                model_turn = run_model_turn(
                    self.model_client,
                    model_context,
                )
                if self.budget.model_timeout_seconds is not None:
                    output = await cancellation_token.run(
                        asyncio.wait_for(model_turn, timeout=self.budget.model_timeout_seconds)
                    )
                else:
                    output = await cancellation_token.run(model_turn)
                # Provider models naturally reuse ``uri`` from read/edit file
                # tools when calling a write tool.  Normalize this declared
                # compatibility alias before artifact policy is evaluated, so
                # it cannot become a way around the source-write guard.
                output = replace(
                    output,
                    tool_calls=_normalize_file_write_tool_intents(output.tool_calls),
                )
            except ContextCompactionError:
                self._emit_user_progress(
                    checkpoint,
                    phase="model",
                    title="等待模型",
                    content="模型准备阶段未完成，正在收敛任务状态。",
                    status="failed",
                    step_id=model_step_id,
                    message_source="runtime",
                )
                adaptive_decision = self.adaptive_reasoning_policy.decide(
                    AdaptiveReasoningInput(
                        trigger="context_compaction_failed",
                        reason_code="context.compaction_failed",
                        context_compaction_failed=True,
                        budget_remaining=max(0, self.budget.max_model_turns - checkpoint.model_turns),
                    )
                )
                if adaptive_decision is not None:
                    self._record_adaptive_decision(checkpoint, adaptive_decision)
                artifact_verdict = evaluate_artifact_terminal(
                    checkpoint.metadata.get("artifact_intent"),
                    self._run_artifacts(checkpoint),
                    repair_attempts=1,
                )
                if self._can_complete_after_artifact_finalization(checkpoint, artifact_verdict):
                    # 压缩只服务于下一次 provider 请求；若当前 Run 已持有全部
                    # 有效产物，可选的最终说明轮失败不能降级已完成的文件操作。
                    self.trace_emitter.emit(
                        checkpoint.run_id,
                        RuntimeEventType.PIPELINE_STAGE,
                        {
                            "stage": "artifact_verdict",
                            "status": "completed_after_context_compaction_failure",
                            "reason": artifact_verdict.reason,
                            "matching_artifact_ids": list(artifact_verdict.matching_artifact_ids),
                        },
                        visibility=EventVisibility.DEBUG,
                        trace_id=_trace_id(checkpoint),
                    )
                    return finish(RunStatus.COMPLETED)
                compaction_state = checkpoint.metadata.get("context_compaction")
                compaction_state = compaction_state if isinstance(compaction_state, dict) else {}
                compaction_budget = compaction_state.get("budget")
                compaction_budget = compaction_budget if isinstance(compaction_budget, dict) else {}
                error = RuntimeError(
                    code=RuntimeErrorCode.CONTEXT_COMPACTION_FAILED,
                    # fail-closed 契约（ISSUE-014）保持不变，但用户侧必须能
                    # 自救：提示新开会话或切换更大窗口的模型，并带出预算
                    # 诊断字段，避免裸 fatal 文案无法定位（ISSUE-164）。
                    message=(
                        "上下文压缩后仍超出模型上下文预算，本次任务已停止。"
                        "可新开会话后重试，或切换上下文窗口更大的模型。"
                    ),
                    retryable=False,
                    category="fatal",
                    details={
                        "stage": "context_compaction",
                        "failure": compaction_state.get("failure") or "context_compaction_failed",
                        "tokens_before": compaction_state.get("tokens_before"),
                        "tokens_after": compaction_state.get("tokens_after"),
                        "available_history_tokens": compaction_budget.get("available_history_tokens"),
                        "usage_ratio": compaction_budget.get("usage_ratio"),
                    },
                )
                self.trace_emitter.emit(
                    checkpoint.run_id,
                    RuntimeEventType.ERROR_RAISED,
                    {"error": error.model_dump(mode="json")},
                    visibility=EventVisibility.USER,
                    trace_id=_trace_id(checkpoint),
                )
                return finish(RunStatus.FAILED, error=error)
            except asyncio.TimeoutError:
                self._emit_user_progress(
                    checkpoint,
                    phase="model",
                    title="等待模型",
                    content="模型响应超时，正在尝试恢复。",
                    status="failed",
                    step_id=model_step_id,
                    message_source="runtime",
                )
                completed_result = self._finish_after_artifact_finalization_failure(
                    checkpoint,
                    completion_status="completed_after_model_timeout",
                    notice="文件已生成。模型响应超时，因此未能补充文字说明。",
                    exception_type="TimeoutError",
                    retryable=True,
                )
                if completed_result is not None:
                    return completed_result
                if self._should_retry_model_timeout(checkpoint):
                    checkpoint.model_unavailable_retries += 1
                    retry_number = checkpoint.model_unavailable_retries
                    delay = self._model_unavailable_retry_delay(retry_number)
                    self.trace_emitter.emit(
                        checkpoint.run_id,
                        RuntimeEventType.PIPELINE_STAGE,
                        {
                            "stage": "model",
                            "status": "retrying_timeout",
                            "retry": retry_number,
                            "max_retries": self.budget.max_model_unavailable_retries,
                            "delay_seconds": delay,
                        },
                        visibility=EventVisibility.USER,
                        trace_id=_trace_id(checkpoint),
                    )
                    checkpoint.metadata["model_unavailable_retries"] = retry_number
                    self.state_store.save(checkpoint)
                    await cancellation_token.run(asyncio.sleep(delay))
                    continue
                error = RuntimeError(
                    code=RuntimeErrorCode.MODEL_FAILED,
                    message=(
                        f"模型供应商{model_failure_subject(None, _loop_model_ref(checkpoint))}"
                        "响应超时，请稍后重试；如持续发生，请切换模型或检查供应商状态。"
                    ),
                    retryable=True,
                    category="transient",
                    details={
                        "stage": "model",
                        "timeout_seconds": self.budget.model_timeout_seconds,
                        "reason_code": "model.provider_timeout",
                        "failure_source": "provider",
                        # Loop 级超时拿不到云端解析的供应商，只带上本地可知的
                        # 模型标识，供交付兜底文案与诊断复用。
                        **({"model": _loop_model_ref(checkpoint)} if _loop_model_ref(checkpoint) else {}),
                    },
                )
                self.trace_emitter.emit(
                    checkpoint.run_id,
                    RuntimeEventType.ERROR_RAISED,
                    {"error": error.model_dump(mode="json")},
                    visibility=EventVisibility.USER,
                    trace_id=_trace_id(checkpoint),
                )
                return finish(RunStatus.FAILED, error=error)
            except Exception as exc:
                self._emit_user_progress(
                    checkpoint,
                    phase="model",
                    title="等待模型",
                    content="模型请求未完成，正在处理失败原因。",
                    status="failed",
                    step_id=model_step_id,
                    message_source="runtime",
                )
                # Preserve the provider-attempt fact even when the transport
                # fails before a normal ``model/completed`` event.  The
                # application-level usage projection uses this event to
                # distinguish an upstream 429/503 from a local preflight gate.
                request_attempts = getattr(exc, "request_attempts", None)
                # A 503 returned before the Cowork route entered its provider
                # call (for example OS identity projection failed) must not be
                # counted as a provider/token attempt.
                provider_request_started = (
                    bool(getattr(exc, "model_unavailable", False))
                    and isinstance(request_attempts, int)
                    and request_attempts > 0
                    and getattr(exc, "failure_source", None) == "provider"
                )
                self.trace_emitter.emit(
                    checkpoint.run_id,
                    RuntimeEventType.PIPELINE_STAGE,
                    {
                        "stage": "model",
                        "status": "failed",
                        "provider_request_started": provider_request_started,
                        **(
                            {"request_attempts": request_attempts}
                            if provider_request_started
                            else {}
                        ),
                        **(
                            {"reason_code": str(getattr(exc, "reason_code"))}
                            if isinstance(getattr(exc, "reason_code", None), str)
                            else {}
                        ),
                    },
                    visibility=EventVisibility.DEBUG,
                    trace_id=_trace_id(checkpoint),
                )
                if self._should_retry_model_unavailable(checkpoint, exc):
                    checkpoint.model_unavailable_retries += 1
                    retry_number = checkpoint.model_unavailable_retries
                    delay = self._model_unavailable_retry_delay(retry_number)
                    self.trace_emitter.emit(
                        checkpoint.run_id,
                        RuntimeEventType.PIPELINE_STAGE,
                        {
                            "stage": "model",
                            "status": "retrying_unavailable",
                            "retry": retry_number,
                            "max_retries": self.budget.max_model_unavailable_retries,
                            "delay_seconds": delay,
                        },
                        visibility=EventVisibility.USER,
                        trace_id=_trace_id(checkpoint),
                    )
                    checkpoint.metadata["model_unavailable_retries"] = retry_number
                    self.state_store.save(checkpoint)
                    await cancellation_token.run(asyncio.sleep(delay))
                    continue
                completed_result = (
                    self._finish_after_artifact_finalization_failure(
                        checkpoint,
                        completion_status="completed_after_model_unavailable",
                        notice="文件已生成。模型服务暂时不可用，因此未能补充文字说明。",
                        exception_type=type(exc).__name__,
                        retryable=bool(getattr(exc, "retryable", False)),
                    )
                    if getattr(exc, "model_unavailable", False)
                    else None
                )
                if completed_result is not None:
                    return completed_result
                completed_result = (
                    self._finish_after_crawler_summary_failure(
                        checkpoint,
                        completion_status="completed_after_crawler_summary_model_unavailable",
                        exception_type=type(exc).__name__,
                        retryable=bool(getattr(exc, "retryable", False)),
                    )
                    if getattr(exc, "model_unavailable", False)
                    else None
                )
                if completed_result is not None:
                    return completed_result
                completed_result = (
                    self._finish_after_crawler_evidence_model_unavailable(
                        checkpoint,
                        completion_status="completed_after_crawler_evidence_model_unavailable",
                        exception_type=type(exc).__name__,
                        retryable=bool(getattr(exc, "retryable", False)),
                    )
                    if getattr(exc, "model_unavailable", False)
                    else None
                )
                if completed_result is not None:
                    return completed_result
                raise
            _LOOP_LOGGER.info(
                "model_turn.completed run_id=%s model_turn=%s duration_ms=%.0f tool_calls=%s tools=%s content_chars=%s reasoning_chars=%s stop_reason=%s resolved_model=%s selection_mode=%s routing_reason=%s",
                checkpoint.run_id,
                checkpoint.model_turns + 1,
                (time.monotonic() - model_turn_started_at) * 1000,
                len(output.tool_calls),
                [intent.tool_name for intent in output.tool_calls][:8],
                len(output.content or ""),
                len(str(output.metadata.get(PROVIDER_REASONING_CONTENT_METADATA_KEY) or "")),
                output.stop_reason,
                output.metadata.get("model"),
                output.metadata.get("selection_mode"),
                output.metadata.get("routing_reason"),
            )
            self._emit_user_progress(
                checkpoint,
                phase="model",
                title="等待模型",
                content=_model_turn_completion_message(output.tool_calls),
                status="completed",
                step_id=model_step_id,
                message_source="runtime",
            )
            model_completed_event = self.trace_emitter.emit(
                checkpoint.run_id,
                RuntimeEventType.PIPELINE_STAGE,
                {
                    "stage": "model",
                    "status": "completed",
                    # Deterministic input gates share the loop lifecycle but
                    # return before Platform HTTP. Keep that distinction in
                    # the durable stage fact for application-level metering.
                    "provider_request_started": output.metadata.get("provider_request_started") is not False,
                    # Cost is safe aggregate telemetry and must remain linked
                    # to this model span even when the turn also contains a
                    # tool call (whose intermediate prose is draft-only).
                    "usage": (
                        public_usage_cost(output.metadata.get("usage"))
                        if output.tool_calls
                        else {}
                    ),
                },
                visibility=EventVisibility.DEBUG,
                trace_id=_trace_id(checkpoint),
            )
            self._emit_model_usage(checkpoint, output.metadata)
            # 只统计真实 provider 请求的 usage。Runtime 在确定性门禁或本地
            # 短路时也会构造 ModelTurnOutput，但那类结果没有消耗模型预算。
            if output.metadata.get("provider_request_started") is not False:
                record_model_usage(checkpoint, output.metadata.get("usage"))
            # Provider prompt_tokens includes framing/tool schema overhead that
            # the local character estimator cannot see. Feed the trusted usage
            # report into the persisted checkpoint so later turns compact
            # before the same provider rejects an oversized request.
            self.context_builder.record_provider_usage(checkpoint, output.metadata)
            self.state_store.save(checkpoint)
            if output.metadata.get("provider_request_started") is not False:
                checkpoint.model_turns += 1
            if not model_requested_tools(output) and _is_length_limited_model_output(output):
                # A provider-enforced output cap is not a completed answer.
                # Preserve the fragment only in the private provider context,
                # then request a continuation.  Publishing each fragment as a
                # final message would make history and callers treat the first
                # incomplete turn as the answer.
                self.context_builder.append_model_output(checkpoint, output)
                fragments = checkpoint.metadata.setdefault("truncated_output_fragments", [])
                if isinstance(fragments, list) and output.content:
                    fragments.append(output.content)
                continuation_count = _output_continuation_count(checkpoint)
                if (
                    continuation_count >= self.budget.max_output_continuations_without_progress
                    or checkpoint.model_turns >= self.budget.max_model_turns
                ):
                    partial = _combined_truncated_output(checkpoint, "")
                    if partial:
                        await self._emit_assistant_output(
                            checkpoint,
                            partial,
                            output.metadata,
                            cancellation_token,
                            authoritative=True,
                        )
                        checkpoint.final_content = partial
                    error = _incomplete_output_error(output.stop_reason)
                    self.trace_emitter.emit(
                        checkpoint.run_id,
                        RuntimeEventType.ERROR_RAISED,
                        {"error": error.model_dump(mode="json")},
                        visibility=EventVisibility.USER,
                        trace_id=_trace_id(checkpoint),
                    )
                    return self._finish(checkpoint, RunStatus.BLOCKED, error=error)
                self.context_builder.append_system(
                    checkpoint,
                    _output_continuation_instruction(),
                    metadata={"_runtime_output_continuation": True},
                )
                checkpoint.metadata["output_continuation_count"] = continuation_count + 1
                self.state_store.save(checkpoint)
                self.trace_emitter.emit(
                    checkpoint.run_id,
                    RuntimeEventType.PIPELINE_STAGE,
                    {
                        "stage": "model",
                        "status": "continuation_requested",
                        "reason": output.stop_reason,
                        "continuation": continuation_count + 1,
                    },
                    visibility=EventVisibility.DEBUG,
                    trace_id=_trace_id(checkpoint),
                )
                continue
            partial_answer = checkpoint.metadata.get("crawler_partial_answer")
            if isinstance(partial_answer, dict) and output.tool_calls:
                summary = {
                    "summary_text": str(partial_answer.get("summary_text") or ""),
                    "coverage_label": str(partial_answer.get("coverage_label") or ""),
                    "observed_count": str(partial_answer.get("observed_count") or "0"),
                    "expected_count": str(partial_answer.get("expected_count") or "0"),
                }
                if checkpoint.metadata.get("crawler_partial_answer_replan") is True:
                    content = _crawler_partial_terminal_text(summary)
                    await self._emit_assistant_output(
                        checkpoint,
                        content,
                        {"source": "runtime", "reason": "crawler_partial_answer"},
                        cancellation_token,
                        authoritative=True,
                    )
                    # 只有完整正文已经公开写入后才持久化。取消发生在流式
                    # 节流期间时，不能让历史里留下未完成的整段回答。
                    self.context_builder.append_assistant(checkpoint, content)
                    self.trace_emitter.emit(
                        checkpoint.run_id,
                        RuntimeEventType.PIPELINE_STAGE,
                        {"stage": "crawler_partial_answer", "status": "forced_completion"},
                        visibility=EventVisibility.DEBUG,
                        trace_id=_trace_id(checkpoint),
                    )
                    return self._finish(checkpoint, RunStatus.COMPLETED)
                checkpoint.metadata["crawler_partial_answer_replan"] = True
                checkpoint.messages.append(
                    {
                        "role": "system",
                        "content": _crawler_partial_answer_instruction(summary),
                        "metadata": {"_runtime_crawler_partial_answer": True},
                    }
                )
                self.state_store.save(checkpoint)
                self.trace_emitter.emit(
                    checkpoint.run_id,
                    RuntimeEventType.PIPELINE_STAGE,
                    {"stage": "crawler_partial_answer", "status": "answer_requested"},
                    visibility=EventVisibility.DEBUG,
                    trace_id=_trace_id(checkpoint),
                )
                continue
            if self._should_suppress_redundant_artifact_write(checkpoint, output.tool_calls):
                # Runtime 主动抑制的 provider 工具调用不得持久化，否则完整
                # Session 历史会留下无法闭合的 assistant/tool 对。
                if output.content:
                    await self._emit_assistant_output(
                        checkpoint,
                        output.content,
                        output.metadata,
                        cancellation_token,
                    )
                self.context_builder.append_assistant(
                    checkpoint,
                    output.content,
                    metadata=output.metadata,
                )
                artifact_verdict = evaluate_artifact_terminal(
                    checkpoint.metadata.get("artifact_intent"),
                    self._run_artifacts(checkpoint),
                    repair_attempts=1,
                )
                self.trace_emitter.emit(
                    checkpoint.run_id,
                    RuntimeEventType.PIPELINE_STAGE,
                    {
                        "stage": "artifact_verdict",
                        "status": "redundant_artifact_write_suppressed",
                        "reason": artifact_verdict.reason,
                        "present_formats": [item.value for item in artifact_verdict.present_formats],
                        "suppressed_tools": [intent.tool_name for intent in output.tool_calls],
                    },
                    visibility=EventVisibility.DEBUG,
                    trace_id=_trace_id(checkpoint),
                )
                self._emit_decision(
                    checkpoint,
                    choice="respond",
                    alternatives=[intent.tool_name for intent in output.tool_calls],
                    reason_code="required_artifacts_already_satisfied",
                    evidence_events=[reasoning_event, model_completed_event],
                    confidence=_confidence(output.metadata),
                )
                cancellation_token.raise_if_cancelled()
                return finish(RunStatus.COMPLETED)
            if self._should_suppress_unrequested_artifact_write(checkpoint, output.tool_calls):
                # Provider 工具调用尚未进入 Session 历史时直接拦截，避免留下
                # 没有对应 tool result 的孤儿消息，也确保未授权写入不触达 Host。
                if output.content:
                    await self._emit_assistant_output(
                        checkpoint,
                        output.content,
                        output.metadata,
                        cancellation_token,
                    )
                self.context_builder.append_assistant(
                    checkpoint,
                    output.content,
                    metadata=output.metadata,
                )
                suppressions = int(
                    checkpoint.metadata.get("unrequested_artifact_write_suppressions") or 0
                )
                event_status = (
                    "repeated_unrequested_artifact_write_blocked"
                    if suppressions >= 1
                    else "unrequested_artifact_write_suppressed"
                )
                self.trace_emitter.emit(
                    checkpoint.run_id,
                    RuntimeEventType.PIPELINE_STAGE,
                    {
                        "stage": "artifact_verdict",
                        "status": event_status,
                        "reason": "artifact_not_required",
                        "suppressed_tools": [intent.tool_name for intent in output.tool_calls],
                    },
                    visibility=EventVisibility.DEBUG,
                    trace_id=_trace_id(checkpoint),
                )
                if suppressions >= 1:
                    # 重复未授权写入不应把模型的错误工具选择升级成用户可见的
                    # 致命失败。保留写入门禁，并以明确的澄清收口，让用户决定
                    # 是否确实需要文件或代码工程。
                    clarification = (
                        "当前请求未授权该文件写入，Runtime 已阻止重复写入。"
                        "请明确所需交付物后再继续。"
                    )
                    checkpoint.final_content = clarification
                    self.context_builder.append_assistant(
                        checkpoint,
                        clarification,
                        metadata={"_runtime_unrequested_artifact_write_clarification": True},
                    )
                    self.trace_emitter.emit(
                        checkpoint.run_id,
                        RuntimeEventType.PIPELINE_STAGE,
                        {
                            "stage": "artifact_verdict",
                            "status": "repeated_unrequested_artifact_write_clarified",
                            "reason": "artifact_not_required",
                        },
                        visibility=EventVisibility.DEBUG,
                        trace_id=_trace_id(checkpoint),
                    )
                    return finish(
                        RunStatus.BLOCKED,
                        error=RuntimeError(
                            code=RuntimeErrorCode.POLICY_DENIED,
                            message="重复的未请求文件写入已被阻止。",
                            details={"reason_code": "artifact_not_required"},
                        ),
                    )
                checkpoint.metadata["unrequested_artifact_write_suppressions"] = 1
                checkpoint.messages.append(
                    {
                        "role": "system",
                        "content": unrequested_artifact_write_instruction(
                            checkpoint.metadata.get("artifact_intent")
                        ),
                        "metadata": {"_runtime_unrequested_artifact_write": True},
                    }
                )
                self.state_store.save(checkpoint)
                self._emit_decision(
                    checkpoint,
                    choice="respond",
                    alternatives=[intent.tool_name for intent in output.tool_calls],
                    reason_code="artifact_not_required",
                    evidence_events=[reasoning_event, model_completed_event],
                    confidence=_confidence(output.metadata),
                )
                continue
            if not model_requested_tools(output):
                if self._requires_local_dev_project_action(checkpoint) and not (
                    # run_45a1fc5c：模型第一次回复是向用户确认"是否在授权目录
                    # 初始化？"。这不是拒绝执行，而是正常的澄清轮次；把澄清
                    # 当"未发起项目执行"注入 repair 指令会把模型逼进死角
                    # （下一轮它输出纯文本伪 tool call，仍无真实调用）。澄清
                    # 按 completed 会话回复收口，用户的回答以同 Session 的新
                    # Run 续接并携带该确认上下文。
                    str(output.content or "").strip()
                    and _is_clarification_request(output.content)
                ):
                    # The model has received a Runtime-frozen local_dev catalog
                    # and a workspace grant. A prose refusal is not evidence of
                    # project progress, so keep it private and demand one
                    # concrete tool action before a normal direct answer may
                    # complete this execution Run.
                    self.context_builder.append_model_output(
                        checkpoint,
                        output,
                        record_final_content=False,
                    )
                    # run_45a1fc5c：repair 后模型曾把工具调用以正文文本输出
                    # （``cowork.local_dev.command(...)`` 不是真实的 function
                    # call）。检测到这种伪调用形态时换用更明确的纠正指令，
                    # 告诉它上一条回复根本没有触发工具执行。
                    textual_tool_call = _looks_like_textual_tool_call(output.content)
                    repair_attempts = int(
                        checkpoint.metadata.get(
                            LOCAL_DEV_PROJECT_EXECUTION_REPAIR_ATTEMPTS_METADATA_KEY
                        ) or 0)
                    if repair_attempts >= 1:
                        error = RuntimeError(
                            code=RuntimeErrorCode.TOOL_FAILED,
                            message=(
                                "本地开发工具已授权，但模型未发起项目执行；"
                                "未创建或启动本地项目，请重试。"
                            ),
                            retryable=True,
                            category="model_repairable",
                            details={
                                "stage": "local_dev_project_execution",
                                "reason_code": "local_dev.project_action_missing",
                            },
                        )
                        self.state_store.save(checkpoint)
                        self.trace_emitter.emit(
                            checkpoint.run_id,
                            RuntimeEventType.PIPELINE_STAGE,
                            {
                                "stage": "local_dev_project_execution",
                                "status": "blocked_project_action_missing",
                                "repair_attempts": repair_attempts,
                            },
                            visibility=EventVisibility.DEBUG,
                            trace_id=_trace_id(checkpoint),
                        )
                        self.trace_emitter.emit(
                            checkpoint.run_id,
                            RuntimeEventType.ERROR_RAISED,
                            {"error": error.model_dump(mode="json")},
                            visibility=EventVisibility.USER,
                            trace_id=_trace_id(checkpoint),
                        )
                        return finish(RunStatus.BLOCKED, error=error)
                    checkpoint.metadata[
                        LOCAL_DEV_PROJECT_EXECUTION_REPAIR_ATTEMPTS_METADATA_KEY
                    ] = repair_attempts + 1
                    self.context_builder.append_system(
                        checkpoint,
                        local_dev_project_execution_repair_instruction(
                            textual_tool_call=textual_tool_call,
                        ),
                        metadata={"_runtime_local_dev_project_execution_repair": True},
                    )
                    self.state_store.save(checkpoint)
                    self.trace_emitter.emit(
                        checkpoint.run_id,
                        RuntimeEventType.PIPELINE_STAGE,
                        {
                            "stage": "local_dev_project_execution",
                            "status": "tool_repair_requested",
                            "repair_attempt": repair_attempts + 1,
                        },
                        visibility=EventVisibility.DEBUG,
                        trace_id=_trace_id(checkpoint),
                    )
                    self._emit_decision(
                        checkpoint,
                        choice="call_tool",
                        alternatives=["respond"],
                        reason_code="local_dev_project_action_required",
                        evidence_events=[reasoning_event, model_completed_event],
                        confidence=_confidence(output.metadata),
                    )
                    continue
                modular_verdict = evaluate_modular_delivery(checkpoint.metadata)
                if not modular_verdict.satisfied:
                    # A prose-only response cannot satisfy an explicit
                    # modular-development contract. Keep it provider-private
                    # and give the model one bounded repair/replan attempt.
                    self.context_builder.append_model_output(
                        checkpoint,
                        output,
                        record_final_content=False,
                    )
                    attempts = int(checkpoint.metadata.get("modular_delivery_repair_attempts") or 0)
                    if attempts >= 1:
                        error = RuntimeError(
                            code=RuntimeErrorCode.TOOL_FAILED,
                            message="modular development structure was not satisfied",
                            retryable=True,
                            category="model_repairable",
                            details={
                                "stage": "modular_delivery",
                                "reason_code": "delivery.modular_structure_unsatisfied",
                                "changed_files": list(modular_verdict.changed_files),
                                "minimum_changed_files": modular_verdict.minimum_changed_files,
                            },
                        )
                        checkpoint.final_content = modular_delivery_instruction(modular_verdict)
                        self.context_builder.append_assistant(checkpoint, checkpoint.final_content)
                        self.state_store.save(checkpoint)
                        self.trace_emitter.emit(
                            checkpoint.run_id,
                            RuntimeEventType.PIPELINE_STAGE,
                            {
                                "stage": "modular_delivery",
                                "status": "blocked_unsatisfied",
                                "changed_files": list(modular_verdict.changed_files),
                                "minimum_changed_files": modular_verdict.minimum_changed_files,
                            },
                            visibility=EventVisibility.DEBUG,
                            trace_id=_trace_id(checkpoint),
                        )
                        return finish(RunStatus.BLOCKED, error=error)
                    checkpoint.metadata["modular_delivery_repair_attempts"] = attempts + 1
                    checkpoint.messages.append(
                        {
                            "role": "system",
                            "content": modular_delivery_instruction(modular_verdict),
                            "metadata": {"_runtime_modular_delivery_repair": True},
                        }
                    )
                    self.state_store.save(checkpoint)
                    self.trace_emitter.emit(
                        checkpoint.run_id,
                        RuntimeEventType.PIPELINE_STAGE,
                        {
                            "stage": "modular_delivery",
                            "status": "repair_requested",
                            "changed_files": list(modular_verdict.changed_files),
                            "minimum_changed_files": modular_verdict.minimum_changed_files,
                        },
                        visibility=EventVisibility.DEBUG,
                        trace_id=_trace_id(checkpoint),
                    )
                    continue
                # 分层信号（ISSUE-185）：缺失的 Host 生命周期回执不再 BLOCKED。
                # 功能落盘且模型正常交付时 Run 以模型回复为准收口；未验证的
                # 构建/服务事实降级为 DEBUG 事件 + 追加在最终回复后的中性
                # 提示。会话内历史 Run 已验证的回执由应用层注入，编辑类后续
                # 轮不再被要求重交当前轮收据。模型幻觉"已启动"由用户刷新
                # 自查与下一轮修正兜底，不再由硬门禁防。
                session_evidence = checkpoint.metadata.get(
                    LOCAL_DEV_PROJECT_LIFECYCLE_SESSION_EVIDENCE_METADATA_KEY
                )
                lifecycle_missing = local_dev_project_lifecycle_missing_with_session(
                    checkpoint.metadata,
                    (
                        item
                        for item in (session_evidence if isinstance(session_evidence, list) else [])
                        if isinstance(item, Mapping)
                    ),
                )
                if lifecycle_missing:
                    verified_service_count, required_service_count = (
                        local_dev_project_lifecycle_progress(checkpoint.metadata)
                    )
                    self.trace_emitter.emit(
                        checkpoint.run_id,
                        RuntimeEventType.PIPELINE_STAGE,
                        {
                            "stage": "local_dev_project_lifecycle",
                            "status": "unverified_signal",
                            "missing": list(lifecycle_missing),
                            "verified_service_count": verified_service_count,
                            "required_service_count": required_service_count,
                        },
                        visibility=EventVisibility.DEBUG,
                        trace_id=_trace_id(checkpoint),
                    )
                    signal_note = local_dev_project_lifecycle_signal_note(lifecycle_missing)
                    if signal_note not in str(output.content or ""):
                        output = replace(
                            output,
                            content=(
                                f"{str(output.content or '').rstrip()}\n\n{signal_note}"
                            ).strip(),
                        )

            if (
                not model_requested_tools(output)
                and _owns_run_lifecycle(checkpoint)
                and not str(output.content or "").strip()
                and not str(
                    output.metadata.get(PROVIDER_REASONING_CONTENT_METADATA_KEY) or ""
                ).strip()
                and output.stop_reason != "policy_gate"
            ):
                # 供应商偶发返回空正文且无工具调用（run_f478cdeb：gemini 经
                # 网关 finish_reason=stop、completion_tokens=123、content=""）。
                # 空输出不是用户可接受的终态：先给模型一次带修复指令的重试
                # 机会，重试仍为空才落失败终态；绝不能静默 completed 后靠
                # delivery 兜底文案收尾。守卫必须放在 local_dev action /
                # modular / lifecycle 等领域终态策略之后，避免抢先截断它们
                # 的 BLOCKED 语义；编排步骤（orchestrated）不拥有 Run 生命
                # 周期，其空输出由编排器的 plan step 状态机自行裁决。
                empty_retries = int(
                    checkpoint.metadata.get("model_empty_response_retries") or 0
                )
                if empty_retries < 1 and output.metadata.get("provider_request_started") is not False:
                    checkpoint.metadata["model_empty_response_retries"] = empty_retries + 1
                    # 空响应是本次 auto/fixed 表现差异的直接触发点：把当时的
                    # 模型解析结果与 usage 一起落日志，才能区分“网关换模型”
                    # 还是“同模型偶发空正文”，并确认重试是否解析到了别的模型。
                    _LOOP_LOGGER.warning(
                        "model_empty_response_retry run_id=%s model_turn=%s resolved_model=%s selection_mode=%s routing_reason=%s finish_reason=%s content_chars=%s reasoning_chars=%s usage=%s",
                        checkpoint.run_id,
                        checkpoint.model_turns + 1,
                        output.metadata.get("model"),
                        output.metadata.get("selection_mode"),
                        output.metadata.get("routing_reason"),
                        output.stop_reason,
                        len(output.content or ""),
                        len(str(output.metadata.get(PROVIDER_REASONING_CONTENT_METADATA_KEY) or "")),
                        json.dumps(output.metadata.get("usage") or {}, ensure_ascii=False, sort_keys=True),
                    )
                    self.context_builder.append_model_output(
                        checkpoint,
                        output,
                        record_final_content=False,
                    )
                    self.context_builder.append_system(
                        checkpoint,
                        (
                            "上一次响应正文为空。请直接给出面向用户的完整中文回答；"
                            "如需执行操作，请明确发起工具调用。不要只输出思考内容。"
                        ),
                        metadata={"_runtime_empty_response_repair": True},
                    )
                    self.state_store.save(checkpoint)
                    self.trace_emitter.emit(
                        checkpoint.run_id,
                        RuntimeEventType.PIPELINE_STAGE,
                        {
                            "stage": "model_output",
                            "status": "empty_response_retry",
                            "retry": empty_retries + 1,
                        },
                        visibility=EventVisibility.DEBUG,
                        trace_id=_trace_id(checkpoint),
                    )
                    self._emit_decision(
                        checkpoint,
                        choice="call_tool",
                        alternatives=["respond"],
                        reason_code="model.empty_response_retry",
                        evidence_events=[reasoning_event, model_completed_event],
                        confidence=_confidence(output.metadata),
                    )
                    continue
                error = RuntimeError(
                    code=RuntimeErrorCode.MODEL_FAILED,
                    message="模型未返回可用内容，请稍后重试。",
                    retryable=True,
                    category="transient",
                    details={
                        "stage": "model_output",
                        "reason_code": "model.empty_response",
                    },
                )
                _LOOP_LOGGER.warning(
                    "model_empty_response_terminal run_id=%s resolved_model=%s selection_mode=%s finish_reason=%s usage=%s",
                    checkpoint.run_id,
                    output.metadata.get("model"),
                    output.metadata.get("selection_mode"),
                    output.stop_reason,
                    json.dumps(output.metadata.get("usage") or {}, ensure_ascii=False, sort_keys=True),
                )
                self.trace_emitter.emit(
                    checkpoint.run_id,
                    RuntimeEventType.ERROR_RAISED,
                    {"error": error.model_dump(mode="json")},
                    visibility=EventVisibility.USER,
                    trace_id=_trace_id(checkpoint),
                )
                return finish(RunStatus.FAILED, error=error)

            visible_content = _combined_truncated_output(checkpoint, output.content)
            if visible_content:
                await self._emit_assistant_output(
                    checkpoint,
                    visible_content,
                    output.metadata,
                    cancellation_token,
                    # Artifact-producing turns may include a useful user-facing
                    # progress sentence alongside the write call. Preserve it
                    # as a real assistant event; ordinary tool preambles remain
                    # draft-only to prevent private planning text leaking.
                    authoritative=(
                        not model_requested_tools(output)
                        or (_requires_artifact(checkpoint) and bool(output.content))
                    ),
                )
            # 该调用没有 await；输出的最后一次取消检查与历史写入之间不留
            # 可调度窗口，避免取消后把 provider 的完整回答写进续接上下文。
            cancellation_token.raise_if_cancelled()
            self.context_builder.append_model_output(checkpoint, output)
            if checkpoint.metadata.pop("_runtime_suppressed_low_information_output", False):
                # Keep the provider turn in private context, but do not let its
                # hollow prose become the Run's public result.
                preserved = _latest_substantial_assistant_content(self.event_store, checkpoint.run_id)
                if preserved:
                    checkpoint.final_content = preserved
            if visible_content and visible_content != output.content:
                # ``append_model_output`` intentionally stores the provider's
                # latest turn for a possible later tool call. The public Run
                # result, however, must contain every length-limited fragment.
                checkpoint.final_content = visible_content
                checkpoint.metadata.pop("truncated_output_fragments", None)

            if not model_requested_tools(output):
                if output.stop_reason == "policy_gate":
                    # 澄清是一次已完成的对话回复，不是同一执行 Run 的暂停。
                    # 用户的补充会以相同 Session 的新 Run 继续，从而保留上下文
                    # 又不会锁住输入框。真正需要原 Run 恢复的登录/审批/桌面
                    # 接管仍由 WAITING_INPUT / WAITING_APPROVAL 路径处理。
                    self._record_completed_clarification(
                        checkpoint,
                        stage="input_gate",
                        reason=str(output.metadata.get("policy_gate") or "policy_gate"),
                    )
                    return finish(RunStatus.COMPLETED)
                if (
                    str(output.content or "").strip()
                    and _session_artifact_intent_needs_clarification(checkpoint)
                    and _is_clarification_request(output.content)
                ):
                    # 文件意图分类器已经以受限结构化契约判定当前意图不明确，
                    # 但只有主模型实际向用户索取输入时才能结束本轮。工具失败
                    # 的陈述不是澄清；把它标成成功会绕过 artifact repair 和
                    # 编排重规划，导致任务看似完成却没有产物。
                    self._record_completed_clarification(
                        checkpoint,
                        stage="artifact_intent",
                        reason="artifact_intent_needs_clarification",
                    )
                    return finish(RunStatus.COMPLETED)
                if (
                    checkpoint.metadata.get("local_skill_action_completed") is True
                    and self._run_artifacts(checkpoint)
                ):
                    # The Skill action already produced its declared local
                    # artifact; do not trigger a second generic script repair.
                    self.trace_emitter.emit(
                        checkpoint.run_id,
                        RuntimeEventType.PIPELINE_STAGE,
                        {
                            "stage": "artifact_verdict",
                            "status": "completed_after_local_skill_action",
                            "reason": "local_skill_action_artifact_satisfied",
                        },
                        visibility=EventVisibility.DEBUG,
                        trace_id=_trace_id(checkpoint),
                    )
                    return finish(RunStatus.COMPLETED)
                if (
                    _requires_artifact(checkpoint)
                    and not self._run_artifacts(checkpoint)
                    and int(checkpoint.metadata.get("artifact_repair_attempts") or 0) >= 1
                ):
                    # 模型在 REPAIR 轮仍没有请求任何工具、也没有产出有效产物：
                    # 它要么在向用户澄清，要么没有足够的输入来创建文件。继续塞
                    # “创建文件”指令只会让它再澄清一轮后落入 fatal BLOCK。这里把
                    # 它收敛为等待输入终态，让用户补充信息后重新进入产出流程，而
                    # 不向用户抛出不可自救的致命错误。
                    checkpoint.metadata["waiting_input"] = {
                        "reason": "missing_artifact_input",
                    }
                    self.state_store.save(checkpoint)
                    self.trace_emitter.emit(
                        checkpoint.run_id,
                        RuntimeEventType.PIPELINE_STAGE,
                        {
                            "stage": "input_gate",
                            "status": "waiting_input",
                            "reason": "missing_artifact_input",
                        },
                        visibility=EventVisibility.DEBUG,
                        trace_id=_trace_id(checkpoint),
                    )
                    return finish(RunStatus.BLOCKED)
                artifact_verdict = evaluate_artifact_terminal(
                    checkpoint.metadata.get("artifact_intent"),
                    self._run_artifacts(checkpoint),
                    repair_attempts=int(checkpoint.metadata.get("artifact_repair_attempts") or 0),
                )
                if artifact_verdict.action is ArtifactVerdictAction.REPAIR:
                    checkpoint.metadata["artifact_repair_attempts"] = 1
                    checkpoint.messages.append(
                        {
                            "role": "system",
                            "content": artifact_repair_instruction(artifact_verdict),
                            "metadata": {"_runtime_artifact_repair": True},
                        }
                    )
                    self.state_store.save(checkpoint)
                    self.trace_emitter.emit(
                        checkpoint.run_id,
                        RuntimeEventType.PIPELINE_STAGE,
                        {
                            "stage": "artifact_verdict",
                            "status": "repair_requested",
                            "reason": artifact_verdict.reason,
                            "expected_formats": [item.value for item in artifact_verdict.expected_formats],
                            "missing_formats": [item.value for item in artifact_verdict.missing_formats],
                            "present_formats": [item.value for item in artifact_verdict.present_formats],
                        },
                        visibility=EventVisibility.DEBUG,
                        trace_id=_trace_id(checkpoint),
                    )
                    continue
                if artifact_verdict.action is ArtifactVerdictAction.BLOCK:
                    error = RuntimeError(
                        code=RuntimeErrorCode.TOOL_FAILED,
                        message="required artifact was not produced",
                        retryable=False,
                        category="fatal",
                        details={
                            "stage": "artifact_verdict",
                            "expected_formats": [item.value for item in artifact_verdict.expected_formats],
                            "missing_formats": [item.value for item in artifact_verdict.missing_formats],
                            "present_formats": [item.value for item in artifact_verdict.present_formats],
                        },
                    )
                    self.trace_emitter.emit(
                        checkpoint.run_id,
                        RuntimeEventType.ERROR_RAISED,
                        {"error": error.model_dump(mode="json")},
                        visibility=EventVisibility.USER,
                        trace_id=_trace_id(checkpoint),
                    )
                    return finish(RunStatus.BLOCKED, error=error)
                self._emit_decision(
                    checkpoint,
                    choice="respond",
                    alternatives=["call_tool"],
                    reason_code="answer_direct",
                    evidence_events=[reasoning_event, model_completed_event],
                    confidence=_confidence(output.metadata),
                )
                cancellation_token.raise_if_cancelled()
                return finish(RunStatus.COMPLETED)

            for index, intent in enumerate(output.tool_calls):
                self._emit_decision(
                    checkpoint,
                    choice=intent.tool_name,
                    alternatives=["respond"],
                    reason_code="tool_required",
                    evidence_events=[reasoning_event, model_started_event, model_completed_event],
                    confidence=_confidence(output.metadata),
                    action_ref=str(intent.metadata.get("provider_tool_call_id") or "") or None,
                    attributes={
                        "tool_name": intent.tool_name,
                        "provider_tool_call_id": intent.metadata.get("provider_tool_call_id"),
                    },
                )
                outcome = await self._run_tool_intent(
                    checkpoint,
                    intent,
                    cancellation_token,
                )
                if outcome.result is not None:
                    tool_results.append(outcome.result)
                if outcome.followup_results:
                    tool_results.extend(outcome.followup_results)
                if outcome.terminal_result is not None:
                    if checkpoint.status is RunStatus.WAITING_APPROVAL:
                        # The provider emitted these calls as one atomic tool
                        # batch. Finish the remaining intents after approval;
                        # requesting another completion now would send an
                        # assistant tool_calls message with missing receipts.
                        checkpoint.pending_tool_intents = list(output.tool_calls[index + 1 :])
                    else:
                        checkpoint.pending_tool_intents.clear()
                    self.state_store.save(checkpoint)
                    terminal = outcome.terminal_result
                    if tool_results:
                        terminal = _with_tool_results(terminal, tool_results)
                    return terminal
                if (
                    outcome.result is not None
                    and outcome.result.status is ToolCallStatus.SUCCEEDED
                    and self._should_finalize_local_dev_project(checkpoint)
                    and index == len(output.tool_calls) - 1
                ):
                    # 项目生命周期证据已经由 Electron Host 完整确认。
                    # 允许一次额外动作覆盖前后端分别启动等合理情况，之后
                    # 不再把收官交回模型，避免模型继续读取/重启导致预算耗尽。
                    checkpoint.final_content = (
                        "本地项目已完成源码写入、依赖处理、构建并通过开发服务器启动验证。"
                    )
                    self.trace_emitter.emit(
                        checkpoint.run_id,
                        RuntimeEventType.PIPELINE_STAGE,
                        {
                            "stage": "local_dev_project_lifecycle",
                            "status": "runtime_finalized",
                            "reason_code": "local_dev.project_lifecycle_verified",
                        },
                        visibility=EventVisibility.DEBUG,
                        trace_id=_trace_id(checkpoint),
                    )
                    return finish(RunStatus.COMPLETED)
                # 当前模型响应中的其余工具调用是在看到本次结果之前生成的。
                # 失败后继续执行旧批次会重复命中同一不可用能力，且无法让模型
                # 使用刚写入上下文的错误事实进行降级规划。
                if outcome.result is not None and tool_failed(outcome.result):
                    checkpoint.pending_tool_intents.clear()
                    break
            self.state_store.save(checkpoint)

    async def _continue_pending_tool_batch(
        self,
        checkpoint: LoopCheckpoint,
        cancellation_token: CancellationToken,
    ) -> "_ToolIntentOutcome":
        """Finish calls remaining from an approved provider tool batch.

        A Chat Completions tool-call message cannot be resumed halfway through:
        every call in that message must receive a tool result before the next
        provider request. The pending list is persisted separately from the
        approval itself so a Desktop restart cannot re-enter the model loop
        with an incomplete protocol block.
        """

        results: List[ToolResult] = []
        while checkpoint.pending_tool_intents:
            intent = checkpoint.pending_tool_intents.pop(0)
            self.state_store.save(checkpoint)
            self._emit_decision(
                checkpoint,
                choice=intent.tool_name,
                alternatives=["respond"],
                reason_code="tool_required",
                evidence_events=[],
                confidence=None,
                action_ref=str(intent.metadata.get("provider_tool_call_id") or "") or None,
                attributes={
                    "tool_name": intent.tool_name,
                    "provider_tool_call_id": intent.metadata.get("provider_tool_call_id"),
                },
            )
            outcome = await self._run_tool_intent(checkpoint, intent, cancellation_token)
            if outcome.result is not None:
                results.append(outcome.result)
            results.extend(outcome.followup_results)
            if outcome.terminal_result is not None:
                return _ToolIntentOutcome(
                    followup_results=results,
                    terminal_result=outcome.terminal_result,
                )
            if outcome.result is not None and tool_failed(outcome.result):
                # A failed call must never cause the unexecuted tail of a
                # batch to run on stale model reasoning. Projection will turn
                # the completed receipts into observations for a clean replan.
                checkpoint.pending_tool_intents.clear()
                self.state_store.save(checkpoint)
                break
        return _ToolIntentOutcome(followup_results=results)

    def _should_retry_model_unavailable(self, checkpoint: LoopCheckpoint, error: Exception) -> bool:
        # PlatformModelClient has already spent its complete shared HTTP
        # deadline before raising this error. Retrying it as another Loop turn
        # would make a single slow upstream call keep the Desktop task alive
        # for several additional minutes. It remains retryable for a fresh
        # user-triggered Run.
        if getattr(error, "reason_code", None) in {
            "model.provider_timeout",
            "model.provider_rate_limited",
        }:
            return False
        return (
            bool(getattr(error, "model_unavailable", False))
            and bool(getattr(error, "retryable", False))
            and checkpoint.model_unavailable_retries < self.budget.max_model_unavailable_retries
        )

    def _should_retry_model_timeout(self, checkpoint: LoopCheckpoint) -> bool:
        """Use the same bounded recovery budget for a timed-out provider turn."""

        return checkpoint.model_unavailable_retries < self.budget.max_model_unavailable_retries

    def _model_unavailable_retry_delay(self, retry_number: int) -> float:
        delays = self.budget.model_unavailable_retry_delays_seconds
        if not delays:
            return 0.0
        index = min(max(0, retry_number - 1), len(delays) - 1)
        return max(0.0, float(delays[index]))

    def _emit_context_compaction(self, checkpoint: LoopCheckpoint) -> None:
        """Expose applied compaction as a structured debug gate signal."""

        raw = checkpoint.metadata.get("context_compaction")
        if not isinstance(raw, dict):
            return
        compaction_id = raw.get("compaction_id")
        if not compaction_id or raw.get("last_emitted_id") == compaction_id:
            return
        payload = {
            "stage": "context_compaction",
            "status": "completed",
            "compaction_id": compaction_id,
            "tokens_before": int(raw.get("tokens_before") or 0),
            "tokens_after": int(raw.get("tokens_after") or 0),
            "source_turn_count": int(raw.get("source_turn_count") or 0),
            "recent_turn_count": int(raw.get("recent_turn_count") or 0),
            "removed_tool_results": int(raw.get("tool_results_removed") or 0),
            "passes": int(raw.get("passes") or 0),
            "budget": dict(raw.get("budget") or {}),
        }
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            payload,
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
        )
        # 上下文压缩是实际发生且与任务连续性直接相关的事实。公开摘要只说明
        # 结果，不包含被压缩的原始对话、工具回执或 token 明细。
        self._emit_user_progress(
            checkpoint,
            phase="context",
            title="上下文已整理",
            content="已精简较早的对话上下文，保留当前任务所需信息。",
            status="completed",
            message_source="runtime",
        )
        raw["last_emitted_id"] = compaction_id

    async def _heartbeat_loop(self, checkpoint: LoopCheckpoint, cancellation_token: CancellationToken) -> None:
        # 心跳本身保持 DEBUG，不直接刷屏；长时间等待模型时每 15 秒更新
        # 同一个公开模型步骤，确保 Think 区持续有事实反馈且不新增重复行。
        user_progress_interval = 15.0
        elapsed = 0.0
        while not cancellation_token.is_cancelled:
            await asyncio.sleep(self.heartbeat_interval_seconds)
            if cancellation_token.is_cancelled:
                return
            elapsed += self.heartbeat_interval_seconds
            self.trace_emitter.emit(
                checkpoint.run_id,
                RuntimeEventType.RUN_HEARTBEAT,
                {"status": RunStatus.RUNNING.value, "interval_seconds": self.heartbeat_interval_seconds},
                visibility=EventVisibility.DEBUG,
            )
            if elapsed >= user_progress_interval and checkpoint.status is RunStatus.RUNNING:
                elapsed = 0.0
                model_turn = max(1, checkpoint.model_turns + 1)
                self._emit_user_progress(
                    checkpoint,
                    phase="model",
                    title="等待模型",
                    content="模型仍在处理当前任务，暂未产生新的工具步骤。",
                    step_id=f"model-turn-{model_turn}",
                    message_source="runtime",
                )

    def _emit_user_progress(
        self,
        checkpoint: LoopCheckpoint,
        *,
        phase: str,
        title: str,
        content: str,
        status: str = "running",
        step_id: str | None = None,
        message_source: str = "runtime",
        display: Mapping[str, Any] | None = None,
    ) -> None:
        """Persist the single public process contract used by live and history UI.

        This deliberately describes observable work only. Provider reasoning,
        tool arguments, local paths and model traces remain debug-only events.

        ``step_id``/``message_source``/``display`` are optional: 公开执行叙事
        需要它们把工具步骤聚合到 step 卡片并脱敏渲染，普通阶段叙事不传即保持
        精简事件。
        """

        payload: dict[str, Any] = {
            "phase": phase,
            "status": status,
            "title": title,
            "content": content,
            "source": "cowork_python_sidecar",
            "message_source": message_source,
        }
        if step_id:
            payload["step_id"] = step_id
            payload["tool_call_id"] = step_id
        if display:
            payload["display"] = dict(display)
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.STEP_PROGRESS,
            payload,
            visibility=EventVisibility.USER,
            trace_id=_trace_id(checkpoint),
        )

    def _emit_tool_completion_progress(
        self,
        checkpoint: LoopCheckpoint,
        call: ToolCall,
        result: ToolResult,
    ) -> None:
        """用真实 ToolResult 结束同一个公开步骤，避免 UI 留下永久运行态。

        合并自远端后保留：远端把工具完成精简为不可见的 execution 通知
        （无 step_id/display），但前端 step 卡需要 step-bound 的 completed
        事件才能从 running 收敛到完成态，因此这里用同一 step_id 关闭卡片。
        """

        progress = tool_completed_progress(call, result)
        self._emit_user_progress(
            checkpoint,
            phase="tool",
            title=str(progress["title"]),
            content=str(progress["content"]),
            status=str(progress["status"]),
            step_id=call.tool_call_id,
            message_source="tool",
            display=progress["display"],
        )

    async def _emit_assistant_output(
        self,
        checkpoint: LoopCheckpoint,
        content: str,
        metadata: Dict[str, object],
        cancellation_token: CancellationToken,
        *,
        authoritative: bool = False,
    ) -> None:
        """Write either a safe draft fact or the authoritative final text.

        The provider often returns explanatory prose together with tool calls.
        That prose is not a user answer yet. Persist only a fixed, safe narrative
        for those turns; the original content remains private Runtime context.
        """

        if _defer_user_response(checkpoint):
            # An intermediate orchestration step may create durable evidence;
            # only the plan's delivery step may publish the user response.
            return
        # Orchestrated fallback plans share one event ledger while each step
        # owns an isolated checkpoint. A later step can emit a hollow pointer
        # and replace a rich answer from an earlier step; keep the substantive
        # answer authoritative for the whole Run.
        if _is_low_information_assistant_text(content):
            preserved = _latest_substantial_assistant_content(self.event_store, checkpoint.run_id)
            if preserved:
                checkpoint.final_content = preserved
                checkpoint.metadata["_runtime_suppressed_low_information_output"] = True
                return
        if not authoritative:
            cancellation_token.raise_if_cancelled()
            self.trace_emitter.emit(
                checkpoint.run_id,
                RuntimeEventType.TURN_ASSISTANT_DRAFT,
                {
                    "content": "正在整理执行结果。",
                    "draft_chars": len(content or ""),
                    "metadata": {"source": "runtime", "reason": "provider_intermediate_output"},
                },
                visibility=EventVisibility.USER,
                trace_id=_trace_id(checkpoint),
            )
            return
        for chunk in _output_chunks(content):
            cancellation_token.raise_if_cancelled()
            self.trace_emitter.emit(
                checkpoint.run_id,
                RuntimeEventType.TEXT_DELTA,
                {"content": chunk},
                visibility=EventVisibility.USER,
                trace_id=_trace_id(checkpoint),
            )
            # Let the Desktop polling bridge observe each persisted chunk. This
            # is presentation pacing for a non-streaming provider response,
            # not model reasoning and not a second output source.
            await cancellation_token.run(asyncio.sleep(0.03))
        # ``turn.assistant_text`` is the authoritative full response. It must
        # never be appended after the user has already stopped the Run.
        cancellation_token.raise_if_cancelled()
        # Provider-only metadata（例如 Think 模型的 reasoning_content）必须只在
        # 下一轮模型请求的私有上下文中流转，不能进入用户事件或调试导出。
        safe_metadata = {
            key: value
            for key, value in metadata.items()
            if key != "usage" and not key.startswith("_provider_")
        }
        usage_cost = public_usage_cost(metadata.get("usage"))
        if usage_cost:
            # Cost telemetry remains available to governed traces, while all
            # token fields live exclusively in usage.updated for idempotency.
            safe_metadata["usage"] = usage_cost
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.TURN_ASSISTANT_TEXT,
            {
                "content": content,
                # Token usage has its own event so a model turn with tool calls
                # is observable and assistant text cannot be counted twice.
                "metadata": safe_metadata,
            },
            trace_id=_trace_id(checkpoint),
        )

    def _emit_model_usage(
        self,
        checkpoint: LoopCheckpoint,
        metadata: Dict[str, object],
    ) -> None:
        """Emit one public usage fact after each completed provider call."""

        usage = public_token_usage(metadata.get("usage"))
        if not usage:
            return
        checkpoint.metadata["model_usage_reported"] = True
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.USAGE_UPDATED,
            {"usage": usage},
            visibility=EventVisibility.USER,
            trace_id=_trace_id(checkpoint),
        )

    async def _run_tool_intent(
        self,
        checkpoint: LoopCheckpoint,
        intent: ToolCallIntent,
        cancellation_token: CancellationToken,
    ) -> "_ToolIntentOutcome":
        cancellation_token.raise_if_cancelled()
        try:
            assert_tool_call_allowed(checkpoint, self.budget)
        except LoopBudgetExceeded as exc:
            return _ToolIntentOutcome(terminal_result=self._finish_for_budget(checkpoint, exc))

        call = ToolCall(
            tool_call_id=str(intent.metadata.get("provider_tool_call_id") or new_contract_id("tool_call")),
            run_id=checkpoint.run_id,
            tool_name=intent.tool_name,
            arguments=dict(intent.arguments),
            status=ToolCallStatus.RUNNING,
            metadata=dict(intent.metadata),
        )
        call = self._enrich_crawler_call(checkpoint, call)
        self._transition_turn(
            checkpoint,
            TurnPhase.TOOL_DISPATCH,
            tool_call_id=call.tool_call_id,
            status=ToolCallStatus.RUNNING.value,
            payload={"tool_name": call.tool_name, "tool_call_id": call.tool_call_id},
        )
        evidence_budget_result = self._review_evidence_budget_exhausted(checkpoint, call)
        if evidence_budget_result is not None:
            self._emit_tool_completion_progress(checkpoint, call, evidence_budget_result)
            self._append_terminal_tool_failure_explanation(
                checkpoint,
                call=call,
                runtime_error=evidence_budget_result.error,
            )
            return _ToolIntentOutcome(
                result=evidence_budget_result,
                terminal_result=self._finish(
                    checkpoint,
                    RunStatus.BLOCKED,
                    error=evidence_budget_result.error,
                    tool_results=[evidence_budget_result],
                ),
            )
        checkpoint.tool_calls += 1
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.TOOL_CALL_CREATED,
            {
                "tool_call_id": call.tool_call_id,
                "tool_name": call.tool_name,
                "arguments": call.arguments,
            },
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
            redact=True,
        )
        review_duplicate = self._review_duplicate_crawler_failure(checkpoint, call)
        if review_duplicate is not None:
            self._emit_tool_completion_progress(checkpoint, call, review_duplicate)
            self._append_terminal_tool_failure_explanation(
                checkpoint,
                call=call,
                runtime_error=review_duplicate.error,
            )
            return _ToolIntentOutcome(
                result=review_duplicate,
                terminal_result=self._finish(
                    checkpoint,
                    RunStatus.BLOCKED,
                    error=review_duplicate.error,
                    tool_results=[review_duplicate],
                ),
            )
        recovery_guard = self.recovery_manager.guard(checkpoint.recovery_state, call)
        if recovery_guard is not None:
            result = self._blocked_by_recovery_guard(checkpoint, call, recovery_guard.guidance)
            self._emit_tool_completion_progress(checkpoint, call, result)
            if recovery_guard.terminal:
                self._append_terminal_tool_failure_explanation(
                    checkpoint,
                    call=call,
                    runtime_error=self._recovery_terminal_error(checkpoint, result.error),
                )
                return _ToolIntentOutcome(
                    result=result,
                    terminal_result=self._finish(
                        checkpoint,
                        RunStatus.BLOCKED,
                        error=result.error,
                        tool_results=[result],
                    ),
                )
            return _ToolIntentOutcome(result=result)
        duplicate_signature = read_only_call_signature(call, checkpoint.metadata)
        if (
            self.budget.successful_read_repeat_threshold > 0
            and duplicate_signature is not None
            and successful_read_count(checkpoint.metadata, duplicate_signature)
            >= self.budget.successful_read_repeat_threshold
        ):
            # 重复读取没有进入 Host，因此也不生成第二条用户可见执行步骤；
            # Debug ledger 仍保留抑制事实，便于审计模型的重复请求。
            result = self._suppress_duplicate_read(
                checkpoint,
                call,
                duplicate_signature,
            )
            return _ToolIntentOutcome(result=result)
        web_search_signature = web_search_call_signature(call)
        if (
            web_search_signature is not None
            and self.budget.successful_web_search_repeat_threshold > 0
            and successful_web_search_call_count(checkpoint.metadata, web_search_signature)
            >= self.budget.successful_web_search_repeat_threshold
        ):
            # 同一搜索已经成功过，不再让它进入 Host 或生成第二张用户可见卡；
            # 受控结果和 Debug ledger 仍保留，模型可继续使用第一次的证据交付。
            result = self._suppress_duplicate_web_search(checkpoint, call, web_search_signature)
            return _ToolIntentOutcome(result=result)
        progress = tool_started_progress(call)
        self._emit_user_progress(
            checkpoint,
            phase="tool",
            title=str(progress["title"]),
            content=str(progress["content"]),
            step_id=call.tool_call_id,
            message_source="tool",
            display=progress["display"],
        )
        if (
            call.tool_name == "cowork.research.market"
            and successful_research_count(checkpoint.metadata)
            >= self.budget.successful_research_limit
        ):
            result = self._suppress_excessive_research(checkpoint, call)
            self._emit_tool_completion_progress(checkpoint, call, result)
            return _ToolIntentOutcome(result=result)
        if (
            call.tool_name == _WEB_SEARCH_TOOL
            and successful_web_search_total(checkpoint.metadata)
            >= self.budget.max_successful_web_searches
        ):
            result = self._suppress_excessive_web_search(checkpoint, call)
            self._emit_tool_completion_progress(checkpoint, call, result)
            return _ToolIntentOutcome(result=result)
        if _is_crawler_artifact_file_read(call):
            result = self._redirect_crawler_artifact_file_read(checkpoint, call)
            self._emit_tool_completion_progress(checkpoint, call, result)
            return _ToolIntentOutcome(result=result)
        reusable = self._reusable_crawler_dataset(checkpoint, call)
        if reusable is not None:
            result = self._suppress_redundant_crawler_fetch(checkpoint, call, reusable)
            self._emit_tool_completion_progress(checkpoint, call, result)
            return _ToolIntentOutcome(result=result)
        crawler_signature = crawler_call_signature(call)
        if (
            crawler_signature is not None
            and successful_crawler_call_count(checkpoint.metadata, crawler_signature) > 0
            and not is_explicit_crawler_refresh(_latest_user_message(checkpoint))
        ):
            result = self._suppress_redundant_crawler_request(checkpoint, call, crawler_signature)
            self._emit_tool_completion_progress(checkpoint, call, result)
            return _ToolIntentOutcome(result=result)
        if (
            _is_read_only_python_script(call)
            and _requires_artifact(checkpoint)
            and self.budget.successful_read_only_script_limit is not None
            and successful_read_only_script_count(checkpoint.metadata)
            >= self.budget.successful_read_only_script_limit
        ):
            result = self._suppress_excessive_read_only_script(checkpoint, call)
            self._emit_tool_completion_progress(checkpoint, call, result)
            return _ToolIntentOutcome(result=result)
        if (
            successful_uri_read_count(checkpoint.metadata, call)
            >= self._successful_uri_read_limit(call)
        ):
            result = self._suppress_excessive_uri_read(checkpoint, call)
            self._emit_tool_completion_progress(checkpoint, call, result)
            return _ToolIntentOutcome(result=result)
        if (
            self.budget.successful_write_uri_limit > 0
            and successful_write_count(checkpoint.metadata, call)
            >= self.budget.successful_write_uri_limit
        ):
            # 同一 URI 的整体重写风暴：写入已多次真实成功（回执带 sha256），
            # 模型却被历史空参数投影误导认为"什么都没写进去"。拦截在 Host
            # 之前，把模型指回磁盘现状（run_272787f5 根因的兜底防线）。
            result = self._suppress_excessive_write(checkpoint, call)
            self._emit_tool_completion_progress(checkpoint, call, result)
            return _ToolIntentOutcome(result=result)
        terminal = await self._execute_tool(checkpoint, call, cancellation_token)
        return _ToolIntentOutcome(
            result=terminal.tool_result,
            followup_results=terminal.followup_results,
            terminal_result=terminal.terminal_result,
        )

    def _session_review_continuation(self, checkpoint: LoopCheckpoint):
        """Resolve an explicit numeric continuation against session-owned evidence.

        This is intentionally before the general model loop.  A partial dataset
        plus an explicit target is sufficient to safely continue the same
        public review collection; waiting for a model here previously made a
        deterministic data operation fail whenever the gateway was rate-limited.
        """

        store = self.context_builder.session_store
        if store is None:
            return None
        dataset = active_dataset(store.list_crawler_datasets(checkpoint.session_id), profile="reviews")
        if dataset is None or has_complete_coverage(dataset):
            return None
        coverage = dataset.ref.coverage
        target = review_continuation_target_for_dataset(
            _latest_user_message(checkpoint),
            expected_count=coverage.expected_count if coverage is not None else None,
            record_count=dataset.ref.record_count,
        )
        if target is None:
            return None
        if target <= dataset.ref.record_count:
            return None
        return dataset, target

    async def _run_session_review_continuation(
        self,
        checkpoint: LoopCheckpoint,
        continuation,
        cancellation_token: CancellationToken,
    ) -> LoopResult:
        dataset, target = continuation
        try:
            assert_tool_call_allowed(checkpoint, self.budget)
        except LoopBudgetExceeded as exc:
            return self._finish_for_budget(checkpoint, exc)
        call = ToolCall(
            tool_call_id=new_contract_id("tool_call"),
            run_id=checkpoint.run_id,
            tool_name=_WEB_CRAWL_SITE_TOOL,
            arguments={
                "version": CRAWLER_CONTRACT_VERSION,
                "request_id": new_contract_id("crawl_request"),
                "tool_name": _WEB_CRAWL_SITE_TOOL,
                "url": dataset.ref.source_url,
                "max_pages": 1,
                "max_depth": 0,
                "per_domain_limit": 1,
                "same_origin": True,
                "extraction_schema": build_extraction_schema(
                    "评论字段：作者、评分、内容、发布时间",
                    metadata={"source": "runtime_session_review_continuation"},
                ).model_dump(mode="json"),
                "metadata": apply_review_collection_metadata({
                    "continuation_dataset_id": dataset.ref.dataset_id,
                    "continuation_from_record_count": dataset.ref.record_count,
                    "source": "runtime_session_review_continuation",
                }, continuation_review_collection(target_count=target)),
            },
            status=ToolCallStatus.RUNNING,
            metadata={"runtime_session_review_continuation": True},
        )
        checkpoint.tool_calls += 1
        checkpoint.metadata["crawler_continuation"] = {
            "dataset_id": dataset.ref.dataset_id,
            "target_count": target,
            "source": "deterministic_session_asset",
        }
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.TOOL_CALL_CREATED,
            {"tool_call_id": call.tool_call_id, "tool_name": call.tool_name, "arguments": call.arguments},
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
            redact=True,
        )
        self._emit_user_progress(
            checkpoint,
            phase="tool",
            title="继续抓取评论",
            content=f"正在将当前会话的评论数据补齐到 {target} 条。",
            step_id=call.tool_call_id,
            message_source="runtime",
            display=tool_started_progress(call)["display"],
        )
        outcome = await self._execute_tool(checkpoint, call, cancellation_token)
        if outcome.terminal_result is not None:
            return outcome.terminal_result
        result = outcome.tool_result
        if result is not None:
            summary = _crawler_review_terminal_answer(result)
            if summary is not None:
                observed = int(summary["observed_count"])
                target_prefix = (
                    f"已完成本次续抓目标：{target} 条评论。"
                    if observed >= target
                    else f"本次续抓目标为 {target} 条，实际获得 {observed} 条。"
                )
                content = f"{target_prefix}{_crawler_partial_terminal_text(summary)}"
            else:
                content = f"已完成续抓。请以本次返回的评论覆盖率为准，目标为 {target} 条。"
            await self._emit_assistant_output(
                checkpoint,
                content,
                {"source": "runtime", "reason": "crawler_session_continuation"},
                cancellation_token,
                authoritative=True,
            )
            self.context_builder.append_assistant(checkpoint, content)
        return self._finish(
            checkpoint,
            RunStatus.COMPLETED,
            tool_results=[result] if result is not None else None,
        )

    def _enrich_crawler_call(self, checkpoint: LoopCheckpoint, call: ToolCall) -> ToolCall:
        """Add a constrained extraction profile when the user explicitly asks for it.

        The model still owns the tool call.  This only prevents a common loss
        of intent where a valid fetch request omits the optional schema, which
        otherwise leaves review extraction to a later and fragile artifact-read
        turn.
        """

        if call.tool_name not in _LEGACY_CRAWLER_TOOLS:
            return call
        raw_schema = call.arguments.get("extraction_schema")
        arguments = dict(call.arguments)
        metadata = dict(arguments.get("metadata") or {})

        # request_id 只用于 Runtime 侧的爬虫追踪与幂等关联，模型没有必要
        # 生成它。缺失时由 Runtime 在进入严格契约校验前补齐，避免一次
        # 可修复的参数遗漏触发额外模型轮，尤其避免在模型服务抖动时把
        # 爬虫任务放大成 503 失败。
        if not isinstance(arguments.get("request_id"), str) or not arguments["request_id"].strip():
            arguments["request_id"] = new_contract_id("crawl_request")
            metadata["runtime_generated_request_id"] = True
            arguments["metadata"] = metadata

        review_agent_source_url = checkpoint.metadata.get("review_agent_source_url")
        if isinstance(review_agent_source_url, str) and review_agent_source_url.strip():
            # The crawler-only review catalog is a policy scope, not merely a
            # recommendation in the system prompt. Normalize every selected
            # crawler capability to one fixed public product URL and the
            # reviews profile before the strict tool contract is validated.
            source_url = review_agent_source_url.strip()
            source_host = urlsplit(source_url).hostname
            if source_host:
                # 该 metadata 进入严格的 CrawlRequest 后会由 Provider 消费；
                # 不能保留模型自带的 session、浏览器操作或 provider hint。只
                # 留 Runtime 自身生成的请求关联事实与受控评论集合约束。
                review_metadata = {
                    "runtime_generated_request_id": True,
                } if metadata.get("runtime_generated_request_id") is True else {}
                arguments.update({
                    "tool_name": _WEB_CRAWL_SITE_TOOL,
                    "url": source_url,
                    "seed_urls": [],
                    "extraction_schema": build_extraction_schema("reviews").model_dump(mode="json"),
                    "max_pages": 1,
                    "max_depth": 0,
                    "per_domain_limit": 1,
                    "allowed_domains": [source_host],
                    "same_origin": True,
                    "respect_robots": True,
                    "allow_external_sources": False,
                    "source_type": "user_url",
                    "metadata": apply_review_collection_metadata(
                        review_metadata,
                        initial_review_collection(
                            requested_target=_initial_review_target(_latest_user_message(checkpoint)),
                        ),
                    ),
                })
                return call.model_copy(
                    update={"tool_name": _WEB_CRAWL_SITE_TOOL, "arguments": arguments},
                    deep=True,
                )

        # Some model/tool adapters emit a bare JSON Schema here. The public
        # crawler contract intentionally wraps it in ExtractionSchema, so
        # normalize this additive compatibility shape at the Runtime boundary
        # instead of weakening CrawlRequest's strict validation.
        if isinstance(raw_schema, dict):
            bare_json_schema = _bare_extraction_json_schema(raw_schema)
        else:
            bare_json_schema = None
        if bare_json_schema is not None:
            target = _crawler_extraction_target(checkpoint, arguments)
            description = target or "模型请求的结构化网页字段"
            normalized_schema = ExtractionSchema(
                version=CRAWLER_CONTRACT_VERSION,
                profile=_crawler_profile_from_target(target),
                description=description,
                json_schema=bare_json_schema,
                completion_mode="partial_ok",
                metadata={"source": "runtime_bare_json_schema_normalization"},
            )
            metadata["runtime_normalized_extraction_schema"] = True
            arguments["metadata"] = metadata
            arguments["extraction_schema"] = normalized_schema.model_dump(mode="json")
        elif raw_schema is None:
            target = _crawler_extraction_target(checkpoint, arguments)
            if target is not None:
                inferred_schema = build_extraction_schema(
                    target,
                    metadata={"source": "runtime_user_intent_inference"},
                )
                metadata["runtime_inferred_extraction_profile"] = inferred_schema.profile
                arguments["metadata"] = metadata
                arguments["extraction_schema"] = inferred_schema.model_dump(mode="json")

        schema = arguments.get("extraction_schema")
        profile = schema.get("profile") if isinstance(schema, dict) else None
        if profile == "reviews":
            # 评论组件的分页通常不产生新的 URL。强制落入单 URL crawl_site，
            # 使 Provider 能够执行受控的浏览器/API 分页，而非停在 fetch_page
            # 的一次通用恢复。模型不能改变这一集合采集策略。
            existing_spec = review_collection_from_metadata(metadata)
            if existing_spec is not None and existing_spec.collection_mode == "continuation":
                spec = existing_spec
            else:
                spec = initial_review_collection(
                    requested_target=_initial_review_target(_latest_user_message(checkpoint)),
                )
            arguments["metadata"] = apply_review_collection_metadata(metadata, spec)
            arguments["tool_name"] = _WEB_CRAWL_SITE_TOOL
            arguments["max_pages"] = 1
            arguments["max_depth"] = 0
            arguments["per_domain_limit"] = 1
            arguments["same_origin"] = True
            return call.model_copy(
                update={"tool_name": _WEB_CRAWL_SITE_TOOL, "arguments": arguments},
                deep=True,
            )
        return call.model_copy(update={"arguments": arguments}, deep=True)

    def _redirect_crawler_artifact_file_read(
        self,
        checkpoint: LoopCheckpoint,
        call: ToolCall,
    ) -> ToolResult:
        """Keep crawler handles out of the local-file adapter and error budget."""

        result = ToolResult(
            tool_call_id=call.tool_call_id,
            run_id=call.run_id,
            status=ToolCallStatus.SUCCEEDED,
            output={
                "status": "completed",
                "kind": "crawler_artifact_reference",
                "message": (
                    "This is a crawler artifact handle, not a local workspace file. "
                    "Do not retry cowork.file.read_file for it; use the completed crawler "
                    "result already in context, including summary_text, extraction_records, "
                    "evidence_refs, and diagnostics."
                ),
                "next_action": "answer_from_crawler_result",
            },
        )
        self.context_builder.append_tool_result(checkpoint, result)
        checkpoint.messages.append(
            {
                "role": "system",
                "content": (
                    "RUNTIME_CRAWLER_ARTIFACT_ROUTE: sqlite crawler artifacts are not file:// URIs. "
                    "Do not call cowork.file.read_file, preview, read_range, or stat for them. "
                    "Answer from the fetch result already in context; if extraction is empty, "
                    "state that clearly and use crawler diagnostics to choose one bounded retry strategy."
                ),
                "metadata": {"_runtime_crawler_artifact_route": True},
            }
        )
        self.state_store.save(checkpoint)
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.TOOL_RESULT_CREATED,
            tool_result_payload(result),
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
            redact=True,
        )
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            {
                "stage": "crawler_artifact_route",
                "status": "local_file_read_suppressed",
                "tool_name": call.tool_name,
            },
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
        )
        return result

    def _reusable_crawler_dataset(self, checkpoint: LoopCheckpoint, call: ToolCall):
        """只用完整覆盖的数据集抑制同一任务的重复网络抓取。

        会话历史数据仍仅在导出场景中复用；但当前 Run 已完成相同 URL、
        相同 extraction profile 的抓取时，模型再次请求同一个 crawler 只会
        产生重复网络读取。用户明确要求刷新时始终保留重新采集的能力。
        """

        if call.tool_name not in _CRAWLER_TOOLS:
            return None
        if is_explicit_crawler_refresh(_latest_user_message(checkpoint)):
            return None
        store = self.context_builder.session_store
        if store is None:
            return None
        raw = checkpoint.metadata.get("crawler_dataset")
        dataset_id = raw.get("dataset_id") if isinstance(raw, dict) else None
        dataset = (
            store.get_crawler_dataset(checkpoint.session_id, dataset_id)
            if isinstance(dataset_id, str) and dataset_id
            else active_dataset(store.list_crawler_datasets(checkpoint.session_id), profile="reviews")
        )
        if dataset is None or active_dataset([dataset]) is None:
            return None
        if not is_same_source(call.arguments.get("url"), dataset):
            return None
        asset_decision = checkpoint.metadata.get("session_asset_decision")
        if (
            isinstance(asset_decision, dict)
            and asset_decision.get("action") == "reuse_dataset"
            and asset_decision.get("dataset_id") == dataset.ref.dataset_id
        ):
            # The constrained asset selector has already established that this
            # turn consumes session evidence. Partial coverage remains useful
            # for analysis, but must not silently trigger a new crawl.
            return dataset
        if is_export_request(_latest_user_message(checkpoint)):
            return dataset
        # partial dataset 是可导出/可查看的真实证据，但绝不是“已完成”
        # 证据。阻断它会让“继续抓取剩余评论”永远无法进入 Browser Worker。
        if not has_complete_coverage(dataset):
            return None
        if dataset.ref.source_run_id != checkpoint.run_id:
            return None
        if not crawler_dataset_matches_call_profile(call.arguments, dataset):
            return None
        return dataset

    def _suppress_redundant_crawler_fetch(self, checkpoint: LoopCheckpoint, call: ToolCall, dataset) -> ToolResult:
        """将模型的重复抓取请求闭合为已有证据的成功观察。"""

        coverage = dataset.ref.coverage.model_dump(mode="json") if dataset.ref.coverage else None
        complete_coverage = has_complete_coverage(dataset)
        evidence_state = "complete" if complete_coverage else "partial"
        instruction = (
            "Do not fetch the page again; answer or create the requested export from this dataset."
            if complete_coverage
            else "Use these verified partial records only for the requested export; do not imply that missing records exist."
        )
        result = ToolResult(
            tool_call_id=call.tool_call_id,
            run_id=call.run_id,
            status=ToolCallStatus.SUCCEEDED,
            output={
                "status": "completed",
                "kind": "crawler_dataset_reuse",
                "dataset_id": dataset.ref.dataset_id,
                "record_count": dataset.ref.record_count,
                "coverage": coverage,
                "message": (
                    f"A {evidence_state} crawler dataset already exists for this source. "
                    f"{instruction}"
                ),
            },
        )
        self.context_builder.append_tool_result(checkpoint, result)
        checkpoint.messages.append(
            {
                "role": "system",
                "content": (
                    f"RUNTIME_CRAWLER_DATASET_REUSE: A reusable {evidence_state} crawler dataset already exists "
                    f"({dataset.ref.record_count} records, dataset_id={dataset.ref.dataset_id}). "
                    f"{instruction}"
                ),
                "metadata": {"_runtime_crawler_dataset_reuse": dataset.ref.dataset_id},
            }
        )
        self.state_store.save(checkpoint)
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.TOOL_RESULT_CREATED,
            tool_result_payload(result),
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
            redact=True,
        )
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            {
                "stage": "crawler_dataset_reuse",
                "status": "redundant_fetch_suppressed",
                "dataset_id": dataset.ref.dataset_id,
                "record_count": dataset.ref.record_count,
                "tool_name": call.tool_name,
            },
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
        )
        return result

    async def _execute_tool(
        self,
        checkpoint: LoopCheckpoint,
        call: ToolCall,
        cancellation_token: CancellationToken,
        *,
        approval_already_granted: bool = False,
    ) -> "_ToolExecutionOutcome":
        context = ToolExecutionContext(
            run_id=checkpoint.run_id,
            session_id=checkpoint.session_id,
            metadata=dict(checkpoint.metadata),
            cancellation_token=cancellation_token,
        )
        if approval_already_granted:
            resumed_progress = tool_started_progress(call)
            self._emit_user_progress(
                checkpoint,
                phase="tool",
                title="继续执行",
                content="已收到你的确认，正在执行已批准的操作。",
                step_id=call.tool_call_id,
                message_source="tool",
                display=resumed_progress["display"],
            )
        tool_started_at = time.monotonic()
        outcome = await self.tool_runner.run(
            call,
            context,
            approval_already_granted=approval_already_granted,
        )
        # 工具耗时日志：工具执行期间通常没有其它用户可见事件，这行日志
        # 用于把"loading 空窗"归因到具体工具。
        _LOOP_LOGGER.info(
            "tool.executed run_id=%s tool=%s tool_call_id=%s duration_ms=%.0f approval_requested=%s result_status=%s",
            checkpoint.run_id,
            call.tool_name,
            call.tool_call_id,
            (time.monotonic() - tool_started_at) * 1000,
            outcome.approval is not None,
            getattr(getattr(outcome, "result", None), "status", None),
        )
        if outcome.approval is not None:
            self._transition_turn(
                checkpoint,
                TurnPhase.WAITING_APPROVAL,
                tool_call_id=call.tool_call_id,
                status=RunStatus.WAITING_APPROVAL.value,
            )
            checkpoint.status = RunStatus.WAITING_APPROVAL
            checkpoint.pending_tool_call = call
            checkpoint.pending_approval = outcome.approval
            checkpoint.metadata["execution_gate"] = _execution_gate_outcome(
                run_id=checkpoint.run_id,
                kind="approval",
                gate_id=outcome.approval.approval_id,
                reason_code="gate.approval_requested",
            )
            self.state_store.save(checkpoint)
            self._emit_user_progress(
                checkpoint,
                phase="approval",
                title="等待授权",
                content="该操作需要你的确认，确认后将继续执行。",
                status=RunStatus.WAITING_APPROVAL.value,
                step_id=call.tool_call_id,
                message_source="tool",
                display=tool_started_progress(call)["display"],
            )
            self.trace_emitter.emit(
                checkpoint.run_id,
                RuntimeEventType.APPROVAL_REQUESTED,
                outcome.approval.model_dump(mode="json"),
                trace_id=_trace_id(checkpoint),
                redact=True,
            )
            self.trace_emitter.emit(
                checkpoint.run_id,
                RuntimeEventType.PIPELINE_STAGE,
                {"stage": "execution_gate", "status": "pending", "execution_gate": checkpoint.metadata["execution_gate"]},
                visibility=EventVisibility.DEBUG,
                trace_id=_trace_id(checkpoint),
            )
            return _ToolExecutionOutcome(
                terminal_result=self._result(checkpoint, pending_approval=outcome.approval, persist_delivery=True)
            )

        result = outcome.result
        if result is None:
            return _ToolExecutionOutcome()

        # A declared local Skill action owns its output contract. Once the
        # action succeeds and produces artifacts, later fallback planning must
        # not reinterpret the request as a generic script task.
        if (
            call.tool_name == "cowork.skill.run_action"
            and result.status is ToolCallStatus.SUCCEEDED
            and isinstance(result.output, dict)
            and isinstance(result.output.get("artifacts"), list)
            and result.output.get("artifacts")
        ):
            checkpoint.metadata["local_skill_action_completed"] = True
            self.state_store.save(checkpoint)

        # Local Skill activation is a Runtime-owned audit fact. Keep only
        # bounded metadata in the checkpoint; the instruction body remains in
        # the model context/tool result and is never copied into audit fields.
        skill_output = result.output if isinstance(result.output, dict) else None
        activation = skill_output.get("activation") if isinstance(skill_output, dict) else None
        if call.tool_name == "cowork.skill.load" and isinstance(activation, dict):
            applied = checkpoint.metadata.setdefault("applied_skill_ids", [])
            if isinstance(applied, list):
                skill_id = str(activation.get("skill_id") or "").strip()
                if skill_id and skill_id not in applied:
                    applied.append(skill_id)
            selections = checkpoint.metadata.setdefault("skill_selections", [])
            if isinstance(selections, list):
                selections.append({
                    "skill_id": str(activation.get("skill_id") or ""),
                    "name": str(activation.get("name") or ""),
                    "version": activation.get("version"),
                    "source": str(activation.get("source") or "created"),
                    "reason": str(activation.get("reason") or "model_load"),
                })
            self.state_store.save(checkpoint)

        self._transition_turn(
            checkpoint,
            TurnPhase.TOOL_DISPATCH,
            tool_call_id=call.tool_call_id,
            status=result.status.value,
            payload={"tool_name": call.tool_name, "tool_call_id": call.tool_call_id},
        )

        self._record_local_dev_project_action_success(checkpoint, call, result)

        # Crawler keeps user-input barriers and partial evidence as a successful
        # CrawlResult. Project those stable diagnostics into RecoveryGuidance
        # here instead of creating a competing browser-recovery mechanism.
        crawler_projection = _crawler_recovery_projection(call, result)
        if crawler_projection is not None and crawler_projection.convert_to_failed:
            result = result.model_copy(
                update={
                    "status": ToolCallStatus.FAILED,
                    "error": crawler_projection.error,
                    "output": _crawler_failure_output(result.output),
                },
                deep=True,
            )

        # Evaluate progress after the crawler projection has classified policy
        # and source failures. A blocked fetch must not look like an advanced
        # step merely because the provider returned a non-empty payload.
        self._record_crawler_progress(checkpoint, call, result)

        execution_contract = outcome.execution_contract or {}
        failure_error = result.error if tool_failed(result) else None
        if tool_failed(result) and failure_error is None:
            # A third-party Host must not be able to bypass the recovery
            # contract by returning FAILED without its own RuntimeError.
            failure_error = RuntimeError(
                code=RuntimeErrorCode.TOOL_FAILED,
                message="tool failed without a structured error",
                category="model_repairable",
            )
            result = result.model_copy(update={"error": failure_error})
        if failure_error is not None:
            failure_error = _normalize_local_script_security_error(call, failure_error)
            result = result.model_copy(update={"error": failure_error})
        if (
            failure_error is not None
            and call.tool_name in _CRAWLER_TOOLS
            and review_agent_enabled(checkpoint.metadata)
        ):
            self._record_review_crawler_failure(checkpoint, call, failure_error)
            if self._review_failure_must_stop(failure_error):
                self._emit_tool_completion_progress(checkpoint, call, result)
                self._append_terminal_tool_failure_explanation(
                    checkpoint,
                    call=call,
                    runtime_error=failure_error,
                )
                return _ToolExecutionOutcome(
                    tool_result=result,
                    terminal_result=self._finish(
                        checkpoint,
                        RunStatus.BLOCKED,
                        error=failure_error,
                        tool_results=[result],
                    ),
                )
        failure_category = classify_runtime_error(failure_error) if failure_error is not None else None
        can_explicitly_retry = (
            failure_error is not None
            and failure_category == "transient"
            and checkpoint.transient_retries < self.budget.max_transient_retries
            and _is_idempotent_execution_contract(execution_contract, failure_error)
        )
        if can_explicitly_retry:
            # An explicitly idempotent transient retry is a Runtime-internal
            # transport repair. Do not poison the model context with a stale
            # intermediate failure while the same attempt is still in flight.
            checkpoint.transient_retries += 1
            self.trace_emitter.emit(
                checkpoint.run_id,
                RuntimeEventType.ERROR_RAISED,
                {
                    "error": {
                        "code": failure_error.code.value,
                        "category": failure_category,
                        "signature": error_signature(
                            failure_error,
                            tool_name=call.tool_name,
                            arguments=call.arguments,
                        ),
                        "retry": checkpoint.transient_retries,
                    }
                },
                visibility=EventVisibility.DEBUG,
                trace_id=_trace_id(checkpoint),
            )
            checkpoint.metadata["last_tool_error"] = {
                "category": failure_category,
                "signature": error_signature(
                    failure_error,
                    tool_name=call.tool_name,
                    arguments=call.arguments,
                ),
            }
            self.state_store.save(checkpoint)
            return await self._execute_tool(checkpoint, call, cancellation_token, approval_already_granted=True)
        recovery_observation = self.recovery_manager.observe(
            checkpoint.recovery_state,
            call,
            result,
            failure_error,
            category=failure_category,
            allow_guidance=not can_explicitly_retry,
            available_tools=self._available_tool_names(checkpoint),
        )
        crawler_guidance = self._crawler_recovery_guidance(
            checkpoint,
            call,
            result,
            projection=crawler_projection,
        )
        if crawler_guidance is not None:
            result = result.model_copy(update={"recovery": crawler_guidance}, deep=True)
        elif recovery_observation.guidance is not None:
            result = result.model_copy(update={"recovery": recovery_observation.guidance})
        workspace_guidance = workspace_uri_recovery_guidance(
            call,
            failure_error,
            run_metadata=checkpoint.metadata,
            resolve_authorized_directory_uri=self.resolve_authorized_directory_uri,
        )
        # A repeated malformed root call must still reach the generic loop
        # restriction. The precise workspace correction is only the first
        # recoverable nudge, never a way to bypass escalation.
        if workspace_guidance is not None and (
            result.recovery is None or result.recovery.stage == "nudge"
        ):
            result = result.model_copy(update={"recovery": workspace_guidance}, deep=True)
        adaptive_decision = self._adaptive_tool_failure_decision(
            checkpoint,
            call,
            failure_error,
            execution_contract,
            recovery_observation.fingerprint,
            result.recovery,
        )
        if crawler_projection is not None and crawler_projection.block_call:
            self.recovery_manager.block_resource(
                checkpoint.recovery_state,
                call,
                reason_code=crawler_projection.error.details["reason_code"],
            )
            reason_code = crawler_projection.error.details["reason_code"]
            if reason_code.startswith("crawler.source."):
                # A later model-gateway failure must not erase this confirmed,
                # URL-free source fact from the user-visible conclusion.
                checkpoint.metadata["last_crawler_source_failure"] = {
                    "reason_code": reason_code,
                }
        self.state_store.save(checkpoint)

        crawler_interaction = _pending_crawler_interaction(call, result)
        if crawler_interaction is not None:
            checkpoint.status = RunStatus.WAITING_INPUT
            checkpoint.pending_tool_call = call
            checkpoint.metadata["crawler_pending_interaction"] = crawler_interaction
            checkpoint.metadata["execution_gate"] = _execution_gate_outcome(
                run_id=checkpoint.run_id,
                kind="desktop_takeover",
                gate_id=str(crawler_interaction.get("interaction_id") or call.tool_call_id),
                reason_code="gate.desktop_takeover_requested",
            )
            self.context_builder.append_tool_result(checkpoint, result)
            self.state_store.save(checkpoint)
            self.trace_emitter.emit(
                checkpoint.run_id,
                RuntimeEventType.TOOL_RESULT_CREATED,
                tool_result_payload(result),
                visibility=EventVisibility.DEBUG,
                trace_id=_trace_id(checkpoint),
                redact=True,
            )
            self.trace_emitter.emit(
                checkpoint.run_id,
                RuntimeEventType.CRAWLER_INTERACTION_REQUESTED,
                {
                    **crawler_interaction,
                    "session_ref_present": bool(crawler_interaction.get("session_ref")),
                },
                trace_id=_trace_id(checkpoint),
                redact=True,
            )
            self.trace_emitter.emit(
                checkpoint.run_id,
                RuntimeEventType.PIPELINE_STAGE,
                {"stage": "execution_gate", "status": "pending", "execution_gate": checkpoint.metadata["execution_gate"]},
                visibility=EventVisibility.DEBUG,
                trace_id=_trace_id(checkpoint),
            )
            self._emit_user_progress(
                checkpoint,
                phase="input",
                title="等待你在浏览器中完成操作",
                content="页面需要登录、验证或授权。请在桌面接管窗口完成操作后继续。",
                status=RunStatus.WAITING_INPUT.value,
                step_id=call.tool_call_id,
                message_source="tool",
                display=tool_started_progress(call)["display"],
            )
            return _ToolExecutionOutcome(terminal_result=self._result(checkpoint, persist_delivery=True))

        self.context_builder.append_tool_result(checkpoint, result)
        self._append_recovery_reminder(checkpoint, result.recovery)
        if _is_ordinary_research_scope_denial(checkpoint, call, failure_error):
            # A model may still emit a stale/hidden fetch call when a Desktop
            # sidecar has an older catalog projection. This is a scope
            # violation, not missing user input: close the network branch and
            # give the model one bounded replan using the search evidence it
            # already has. Never turn this into a waiting_for_user delivery.
            disabled = checkpoint.metadata.get("disabled_tool_names")
            if not isinstance(disabled, list):
                disabled = []
                checkpoint.metadata["disabled_tool_names"] = disabled
            for tool_name in ("cowork.web.fetch_page", "cowork.web.crawl_site"):
                if tool_name not in disabled:
                    disabled.append(tool_name)
            checkpoint.messages.append(
                {
                    "role": "system",
                    "content": (
                        "RUNTIME_RESEARCH_SCOPE: 已阻止一次超出普通调研范围的网页读取。"
                        "不要再次请求 fetch_page 或 crawl_site；请直接基于已完成的公开搜索结果"
                        "整理结论，并继续生成用户要求的报告。无需用户补充登录、授权或其他信息。"
                    ),
                    "metadata": {"_runtime_research_scope_replan": True},
                }
            )
            checkpoint.metadata["research_scope_replan_requested"] = True
            self.state_store.save(checkpoint)
            self.trace_emitter.emit(
                checkpoint.run_id,
                RuntimeEventType.PIPELINE_STAGE,
                {
                    "stage": "research_scope",
                    "status": "replanned_from_model_fetch",
                    "tool_name": call.tool_name,
                    "reason_code": "research.search_first_scope_denied",
                },
                visibility=EventVisibility.DEBUG,
                trace_id=_trace_id(checkpoint),
            )
            self._emit_tool_completion_progress(checkpoint, call, result)
            return _ToolExecutionOutcome(tool_result=result)
        if (
            crawler_projection is not None
            and crawler_projection.error.details.get("reason_code")
            in {"crawler.provider.unavailable", "crawler.policy.robots_denied"}
            and not _is_search_followup_call(call)
        ):
            # Crawler 已明确没有可继续执行的路径：完全无法取得证据，或目标
            # 明确禁止自动抓取。不能再把失败交给模型“整理回答”或尝试原资源，
            # 否则模型网关未及时返回时，页面会长期显示执行中而没有失败交互。
            # 限流和部分证据仍保留原来的替代来源恢复策略。
            self._emit_tool_completion_progress(checkpoint, call, result)
            self._append_terminal_tool_failure_explanation(
                checkpoint,
                call=call,
                runtime_error=failure_error or crawler_projection.error,
            )
            return _ToolExecutionOutcome(
                tool_result=result,
                terminal_result=self._finish(
                    checkpoint,
                    RunStatus.BLOCKED,
                    error=failure_error or crawler_projection.error,
                    tool_results=[result],
                ),
            )
        if adaptive_decision is not None:
            self._record_adaptive_decision(checkpoint, adaptive_decision)
            if adaptive_decision.action == "terminalize":
                self._emit_tool_completion_progress(checkpoint, call, result)
                self._append_terminal_tool_failure_explanation(
                    checkpoint,
                    call=call,
                    runtime_error=failure_error or RuntimeError(
                        code=RuntimeErrorCode.TOOL_FAILED,
                        message="recovery restriction exhausted",
                        category="fatal",
                    ),
                )
                return _ToolExecutionOutcome(
                    tool_result=result,
                    terminal_result=self._finish(
                        checkpoint,
                        RunStatus.BLOCKED,
                        error=failure_error,
                        tool_results=[result],
                    ),
                )
            if adaptive_decision.action == "wait_for_user":
                checkpoint.metadata["waiting_input"] = {"reason": adaptive_decision.reason_code}
                checkpoint.metadata["execution_gate"] = _execution_gate_outcome(
                    run_id=checkpoint.run_id,
                    kind="user_input",
                    gate_id=f"input:{call.tool_call_id}",
                    reason_code=f"gate.{adaptive_decision.reason_code}",
                )
                self.state_store.save(checkpoint)
                self._emit_tool_completion_progress(checkpoint, call, result)
                return _ToolExecutionOutcome(
                    tool_result=result,
                    terminal_result=self._finish(
                        checkpoint,
                        RunStatus.BLOCKED,
                        error=failure_error,
                        tool_results=[result],
                    ),
                )
            if adaptive_decision.action == "local_diagnose":
                self.context_builder.append_system(
                    checkpoint,
                    adaptive_decision.system_reminder(tool_name=call.tool_name),
                    metadata={
                        "_runtime_adaptive_reasoning": True,
                        "adaptive_action": adaptive_decision.action,
                        "adaptive_reason_code": adaptive_decision.reason_code,
                        "fact_fingerprint": adaptive_decision.fact_fingerprint,
                    },
                )
        # The legacy aggregate result has its own Dataset projection.  The
        # fine-grained route reaches the same Session asset boundary only
        # after the Runtime has re-read its opaque final evidence reference.
        # Tool output and model prose are never accepted as record input.
        partial_answer = _crawler_review_terminal_answer(result) if call.tool_name in _LEGACY_CRAWLER_TOOLS else None
        if partial_answer is not None:
            checkpoint.metadata["crawler_partial_answer"] = partial_answer
        if call.tool_name in _LEGACY_CRAWLER_TOOLS and result.status is ToolCallStatus.SUCCEEDED:
            self._set_review_agent_phase(checkpoint, REVIEW_AGENT_PHASE_DELIVERY, reason="legacy_crawler_observation")
            dataset = dataset_from_crawl_result(
                run_id=checkpoint.run_id,
                source_url=call.arguments.get("url"),
                result=result.output,
            )
            if dataset is not None and self.context_builder.session_store is not None:
                existing_dataset = next(
                    (
                        candidate
                        for candidate in self.context_builder.session_store.list_crawler_datasets(checkpoint.session_id)
                        if candidate.ref.profile == dataset.ref.profile
                        and candidate.ref.source_url == dataset.ref.source_url
                    ),
                    None,
                )
                dataset = merge_session_crawler_dataset(existing_dataset, dataset)
                self.context_builder.session_store.save_crawler_dataset(
                    checkpoint.session_id,
                    dataset,
                )
                checkpoint.metadata["crawler_dataset"] = dataset.ref.model_dump(mode="json")
        elif call.tool_name in _REVIEW_EVIDENCE_TOOLS and result.status is ToolCallStatus.SUCCEEDED:
            checkpoint.metadata[REVIEW_EVIDENCE_TOOL_CALLS_KEY] = (
                int(checkpoint.metadata.get(REVIEW_EVIDENCE_TOOL_CALLS_KEY) or 0) + 1
            )
            evidence_commit = self._advance_review_evidence_phase(checkpoint, call, result)
            if evidence_commit is not None:
                self._record_review_evidence_commit(checkpoint, evidence_commit)
        crawler_signature = crawler_call_signature(call)
        if crawler_signature is not None and result.status is ToolCallStatus.SUCCEEDED:
            record_successful_crawler_call(checkpoint.metadata, crawler_signature)
            self.trace_emitter.emit(
                checkpoint.run_id,
                RuntimeEventType.PIPELINE_STAGE,
                {
                    "stage": "crawler_guard",
                    "status": "request_recorded",
                    "tool_name": call.tool_name,
                    "signature": crawler_signature,
                },
                visibility=EventVisibility.DEBUG,
                trace_id=_trace_id(checkpoint),
            )
        self.state_store.save(checkpoint)
        tool_result_event = self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.TOOL_RESULT_CREATED,
            tool_result_payload(result),
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
            redact=True,
        )
        self._emit_tool_completion_progress(checkpoint, call, result)
        if _is_client_action_proposal(result):
            # Client-action skills only create a structured Desktop proposal;
            # the command has not run yet. Pause the loop before another model
            # turn so the API can persist and dispatch the durable action.
            checkpoint.status = RunStatus.WAITING_INPUT
            checkpoint.pending_tool_call = call
            checkpoint.metadata["pending_client_action"] = {
                "tool_call_id": call.tool_call_id,
                "tool_name": call.tool_name,
                "payload": dict(result.output or {}),
            }
            self.state_store.save(checkpoint)
            return _ToolExecutionOutcome(
                tool_result=result,
                terminal_result=self._result(checkpoint, tool_results=[result], persist_delivery=True),
            )
        if partial_answer is not None:
            if partial_answer["is_complete"] == "True":
                # 完整 records 是用户请求的证据，不是最终解释。给模型严格一次
                # 无工具总结回合；若它仍要调用工具，下一轮逻辑会降级到固定结论。
                checkpoint.metadata["crawler_partial_answer_replan"] = True
                checkpoint.messages.append(
                    {
                        "role": "system",
                        "content": _crawler_review_summary_instruction(partial_answer),
                        "metadata": {"_runtime_crawler_review_summary": True},
                    }
                )
                self.state_store.save(checkpoint)
                self.trace_emitter.emit(
                    checkpoint.run_id,
                    RuntimeEventType.PIPELINE_STAGE,
                    {"stage": "crawler_review_summary", "status": "answer_requested"},
                    visibility=EventVisibility.DEBUG,
                    trace_id=_trace_id(checkpoint),
                )
                return _ToolExecutionOutcome(tool_result=result)
            if int(partial_answer["observed_count"]) <= 0:
                # 只有评分或总数而没有单条评论时，模型没有可总结的评论内容；
                # 继续请求总结会诱发越过证据边界的推断。
                content = _crawler_partial_terminal_text(partial_answer)
                await self._emit_assistant_output(
                    checkpoint,
                    content,
                    {"source": "runtime", "reason": "crawler_partial_answer"},
                    cancellation_token,
                    authoritative=True,
                )
                self.context_builder.append_assistant(checkpoint, content)
                self.trace_emitter.emit(
                    checkpoint.run_id,
                    RuntimeEventType.PIPELINE_STAGE,
                    {"stage": "crawler_partial_answer", "status": "forced_completion"},
                    visibility=EventVisibility.DEBUG,
                    trace_id=_trace_id(checkpoint),
                )
                return _ToolExecutionOutcome(
                    tool_result=result,
                    terminal_result=self._finish(
                        checkpoint,
                        RunStatus.COMPLETED,
                        tool_results=[result],
                    ),
                )
            # 部分 records 也应有一次基于已有证据的用户总结。该回合禁止新
            # 工具调用；模型若无视限制，下一轮会退回确定性的覆盖率说明。
            checkpoint.metadata["crawler_partial_answer_replan"] = True
            checkpoint.messages.append(
                {
                    "role": "system",
                    "content": _crawler_partial_answer_instruction(partial_answer),
                    "metadata": {"_runtime_crawler_partial_answer": True},
                }
            )
            self.state_store.save(checkpoint)
            self.trace_emitter.emit(
                checkpoint.run_id,
                RuntimeEventType.PIPELINE_STAGE,
                {"stage": "crawler_partial_answer", "status": "answer_requested"},
                visibility=EventVisibility.DEBUG,
                trace_id=_trace_id(checkpoint),
            )
            return _ToolExecutionOutcome(tool_result=result)
        if checkpoint.metadata.get("review_evidence_commit_terminal") is True:
            return _ToolExecutionOutcome(
                tool_result=result,
                terminal_result=self._finish(
                    checkpoint,
                    RunStatus.COMPLETED,
                    tool_results=[result],
                ),
            )
        review_commit_denial = _review_evidence_commit_denial(checkpoint)
        if review_commit_denial is not None:
            # DatasetVersion 已明确拒绝时，delivery phase 不再给模型一次会
            # 请求 crawler 的机会。这样终态保留真实质量原因，不会被后续的
            # phase allowlist 拒绝覆盖，同时不放宽任何工具权限。
            content, error = review_commit_denial
            checkpoint.final_content = content
            self.context_builder.append_assistant(
                checkpoint,
                content,
                metadata={"_runtime_review_evidence_commit_denied": True},
            )
            return _ToolExecutionOutcome(
                tool_result=result,
                terminal_result=self._finish(
                    checkpoint,
                    RunStatus.BLOCKED,
                    error=error,
                    tool_results=[result],
                ),
            )
        self.trace_emitter.emit_cognitive(
            CognitiveRecord(
                record_type="observation",
                record_id=f"observation:{call.tool_call_id}",
                record_key=call.tool_call_id,
                run_id=checkpoint.run_id,
                trace_id=_trace_id(checkpoint) or f"trace:{checkpoint.run_id}",
                source_ref=tool_result_event.event_id,
                action_ref=call.tool_call_id,
                fact_digest=digest_value(result.output),
                reason_code="tool_result",
                confidence=0.0 if tool_failed(result) else 1.0,
                evidence_refs=[
                    CognitiveEvidenceRef(
                        kind="event",
                        ref_id=tool_result_event.event_id,
                        run_id=checkpoint.run_id,
                        relation="derived_from",
                    )
                ],
                attributes={
                    "tool_name": call.tool_name,
                    "status": result.status.value,
                },
            )
        )
        created_artifacts = _artifact_refs(
            result.output,
            artifact_intent=checkpoint.metadata.get("artifact_intent"),
        )
        for artifact in created_artifacts:
            if not any(existing.artifact_id == artifact.artifact_id for existing in checkpoint.artifact_refs):
                checkpoint.artifact_refs.append(artifact)
            if self.context_builder.session_store is not None:
                self.context_builder.session_store.append_artifact_ref(
                    checkpoint.session_id,
                    artifact,
                )
            self.trace_emitter.emit(
                checkpoint.run_id,
                RuntimeEventType.ARTIFACT_CREATED,
                artifact.model_dump(mode="json"),
                trace_id=_trace_id(checkpoint),
            )
            if artifact.access.kind.value == "local_file":
                self.trace_emitter.emit(
                    checkpoint.run_id,
                    RuntimeEventType.FILE_CHANGED,
                    {
                        "artifact_id": artifact.artifact_id,
                        "uri": artifact.access.uri,
                        "size_bytes": artifact.size_bytes,
                    },
                    visibility=EventVisibility.DEBUG,
                    trace_id=_trace_id(checkpoint),
                )
        if created_artifacts:
            # 当前 Run 的每个产物（包括多产物任务的部分结果）都立即持久化；
            # 后续审批恢复或重启不能依赖 EventStore 投影已经追平。
            self.state_store.save(checkpoint)
            self._arm_artifact_completion(checkpoint)
        if tool_failed(result):
            runtime_error = result.error or RuntimeError(
                code=RuntimeErrorCode.TOOL_FAILED,
                message="tool failed",
                category="model_repairable",
            )
            provider_reason = _provider_unavailable_reason(runtime_error)
            if provider_reason is not None:
                self._disable_unavailable_provider_tool(
                    checkpoint,
                    tool_name=call.tool_name,
                    reason_code=provider_reason,
                )
            if call.tool_name == _MARKET_RESEARCH_TOOL and _is_unavailable_market_research(runtime_error):
                # Platform 认证/路由不可用不是模型可以靠重复相同请求修复的错误。
                # 立即收窄目录并让下一轮改用真实的本地联网搜索，避免同一轮
                # 预先生成的多个 market 调研调用把 Runtime 推进 tool-storm。
                self._disable_unavailable_market_research(checkpoint, runtime_error)
                self.state_store.save(checkpoint)
                return _ToolExecutionOutcome(tool_result=result)
            if provider_reason is not None and result.recovery is not None and result.recovery.action == "stop":
                self._append_terminal_tool_failure_explanation(
                    checkpoint,
                    call=call,
                    runtime_error=runtime_error,
                )
                return _ToolExecutionOutcome(
                    tool_result=result,
                    terminal_result=self._finish(
                        checkpoint,
                        RunStatus.BLOCKED,
                        error=runtime_error,
                        tool_results=[result],
                    ),
                )
            category = classify_runtime_error(runtime_error)
            signature = error_signature(
                runtime_error,
                tool_name=call.tool_name,
                arguments=call.arguments,
            )
            security_reason = _local_script_security_reason(call, runtime_error)
            if security_reason is not None:
                self._disable_local_script_after_security_failure(
                    checkpoint,
                    tool_name=call.tool_name,
                    reason_code=security_reason,
                )
                self.state_store.save(checkpoint)
            if category == "transient":
                # Transport retries are exhausted, but the failed capability
                # may be optional. Return the structured failure to the model
                # once so it can replan (for example, edit an existing PPT
                # without fresh market research). Repeated use of the same
                # unavailable capability still enters the normal repair/storm
                # breaker below and cannot loop forever or fabricate evidence.
                category = "model_repairable"
                runtime_error = runtime_error.model_copy(
                    update={"category": "model_repairable"},
                    deep=True,
                )

            if category == "model_repairable":
                checkpoint.repair_attempts += 1
                count = checkpoint.repair_error_signatures.get(signature, 0) + 1
                checkpoint.repair_error_signatures[signature] = count
                checkpoint.last_error_signature = signature
                self.trace_emitter.emit(
                    checkpoint.run_id,
                    RuntimeEventType.ERROR_RAISED,
                    {
                        "error": {
                            "code": runtime_error.code.value,
                            "category": category,
                            "signature": signature,
                            "repair_attempt": checkpoint.repair_attempts,
                        }
                    },
                    visibility=EventVisibility.DEBUG,
                    trace_id=_trace_id(checkpoint),
                )
                if _is_loop_pattern_guidance(result.recovery):
                    # Detector stages are a bounded state machine. Let it
                    # advance from nudge to restrict and enforce the next
                    # attempted call before the legacy repair budget adds a
                    # second competing terminal path.
                    artifact_verdict = evaluate_artifact_terminal(
                        checkpoint.metadata.get("artifact_intent"),
                        self._run_artifacts(checkpoint),
                        repair_attempts=1,
                    )
                    if self._can_complete_after_artifact_finalization(checkpoint, artifact_verdict):
                        # A post-delivery verification failure must not
                        # downgrade an already valid artifact, even when the
                        # new detector is the first policy to observe it.
                        self.trace_emitter.emit(
                            checkpoint.run_id,
                            RuntimeEventType.PIPELINE_STAGE,
                            {
                                "stage": "artifact_verdict",
                                "status": "completed_after_tool_storm",
                                "reason": artifact_verdict.reason,
                                "matching_artifact_ids": list(artifact_verdict.matching_artifact_ids),
                            },
                            visibility=EventVisibility.DEBUG,
                            trace_id=_trace_id(checkpoint),
                        )
                        return _ToolExecutionOutcome(
                            tool_result=result,
                            terminal_result=self._finish(
                                checkpoint,
                                RunStatus.COMPLETED,
                                tool_results=[result],
                            ),
                        )
                    self.state_store.save(checkpoint)
                    return _ToolExecutionOutcome(tool_result=result)
                if checkpoint.repair_attempts > self.budget.max_model_repairs:
                    if self._grant_tool_failure_reprieve(
                        checkpoint,
                        call=call,
                        runtime_error=runtime_error,
                        signature=signature,
                        trigger="repair_budget",
                    ):
                        self.state_store.save(checkpoint)
                        return _ToolExecutionOutcome(tool_result=result)
                    self._append_terminal_tool_failure_explanation(
                        checkpoint,
                        call=call,
                        runtime_error=runtime_error,
                    )
                    return _ToolExecutionOutcome(
                        tool_result=result,
                        terminal_result=self._finish_for_budget(
                            checkpoint,
                            LoopBudgetExceeded("model repair budget exceeded"),
                        ),
                    )
                if (
                    count >= self.budget.tool_storm_threshold
                    and checkpoint.recovery_state.get("last_pattern") != "same_call_failure"
                ):
                    artifact_verdict = evaluate_artifact_terminal(
                        checkpoint.metadata.get("artifact_intent"),
                        self._run_artifacts(checkpoint),
                        repair_attempts=1,
                    )
                    if self._can_complete_after_artifact_finalization(checkpoint, artifact_verdict):
                        # A redundant post-generation call must not downgrade a
                        # Run whose required, validated deliverables already
                        # exist. Preserve the failed ToolResult for diagnostics
                        # and terminate without permitting another model loop.
                        self.trace_emitter.emit(
                            checkpoint.run_id,
                            RuntimeEventType.PIPELINE_STAGE,
                            {
                                "stage": "artifact_verdict",
                                "status": "completed_after_tool_storm",
                                "reason": artifact_verdict.reason,
                                "matching_artifact_ids": list(artifact_verdict.matching_artifact_ids),
                            },
                            visibility=EventVisibility.DEBUG,
                            trace_id=_trace_id(checkpoint),
                        )
                        return _ToolExecutionOutcome(
                            tool_result=result,
                            terminal_result=self._finish(
                                checkpoint,
                                RunStatus.COMPLETED,
                                tool_results=[result],
                            ),
                        )
                    if self._grant_tool_failure_reprieve(
                        checkpoint,
                        call=call,
                        runtime_error=runtime_error,
                        signature=signature,
                        trigger="tool_storm",
                    ):
                        self.state_store.save(checkpoint)
                        return _ToolExecutionOutcome(tool_result=result)
                    self._append_terminal_tool_failure_explanation(
                        checkpoint,
                        call=call,
                        runtime_error=runtime_error,
                    )
                    return _ToolExecutionOutcome(
                        tool_result=result,
                        terminal_result=self._finish(
                            checkpoint,
                            RunStatus.BLOCKED,
                            error=RuntimeError(
                                code=RuntimeErrorCode.TOOL_FAILED,
                                message="repeated tool error stopped the run",
                                category="fatal",
                                details={"breaker": "tool_storm", "signature": signature},
                            ),
                            tool_results=[result],
                        ),
                    )
                self.state_store.save(checkpoint)
                return _ToolExecutionOutcome(tool_result=result)

            checkpoint.failures += 1
            try:
                assert_failure_allowed(checkpoint, self.budget)
            except LoopBudgetExceeded as exc:
                return _ToolExecutionOutcome(
                    tool_result=result,
                    terminal_result=self._finish_for_budget(checkpoint, exc),
                )
            completed_result = self._finish_after_artifact_finalization_failure(
                checkpoint,
                completion_status="completed_after_post_artifact_tool_failure",
                notice="文件已生成。后续检查未完成，文件仍可使用。",
                exception_type=runtime_error.code.value,
                retryable=runtime_error.retryable,
            )
            if completed_result is not None:
                return _ToolExecutionOutcome(
                    tool_result=result,
                    terminal_result=completed_result,
                )
            return _ToolExecutionOutcome(
                tool_result=result,
                terminal_result=self._finish(
                    checkpoint,
                    RunStatus.FAILED,
                    error=result.error
                    or RuntimeError(code=RuntimeErrorCode.TOOL_FAILED, message="tool failed"),
                    tool_results=[result],
                ),
            )
        # 修复预算回收：repair_attempts 原先是全 Run 单调累加，长 coding 任务里
        # 多次良性、可修复的失败（如漏传 overwrite）即使模型已经修复成功也会
        # 累计耗尽预算。这里在每次成功执行后回收一格；连续失败仍然会正常触顶
        # （相同签名重复失败另由 tool_storm 阈值拦截）。
        if checkpoint.repair_attempts > 0:
            checkpoint.repair_attempts -= 1
            self.state_store.save(checkpoint)
        followup_results: List[ToolResult] = []
        if call.tool_name == _WEB_SEARCH_TOOL and not self._search_only_requested(checkpoint, call):
            followup_outcome = await self._auto_fetch_search_results(
                checkpoint,
                call,
                result,
                cancellation_token,
            )
            if followup_outcome.terminal_result is not None:
                return _ToolExecutionOutcome(
                    tool_result=result,
                    followup_results=followup_outcome.followup_results,
                    terminal_result=followup_outcome.terminal_result,
                )
            followup_results.extend(followup_outcome.followup_results)
        record_successful_file_mutations(checkpoint.metadata, call, result)
        write_instruction = record_successful_write(
            checkpoint.metadata,
            call,
            limit=self.budget.successful_write_uri_limit,
        )
        if write_instruction is not None:
            checkpoint.messages.append(write_instruction)
        instruction = record_successful_read(
            checkpoint.metadata,
            call,
            threshold=self.budget.successful_read_repeat_threshold,
            uri_limit=self._successful_uri_read_limit(call),
        )
        if instruction is not None:
            checkpoint.messages.append(instruction)
        if call.tool_name == "cowork.research.market":
            research_instruction = record_successful_research(
                checkpoint.metadata,
                limit=self.budget.successful_research_limit,
            )
            if research_instruction is not None:
                checkpoint.messages.append(research_instruction)
                self._disable_exhausted_market_research(checkpoint)
                self.trace_emitter.emit(
                    checkpoint.run_id,
                    RuntimeEventType.PIPELINE_STAGE,
                    {
                        "stage": "research_budget",
                        "status": "research_budget_reached",
                        "tool_name": call.tool_name,
                        "limit": self.budget.successful_research_limit,
                    },
                    visibility=EventVisibility.DEBUG,
                    trace_id=_trace_id(checkpoint),
                )
        if (
            _is_read_only_python_script(call)
            and not created_artifacts
            and _requires_artifact(checkpoint)
        ):
            script_instruction = record_successful_read_only_script(
                checkpoint.metadata,
                limit=self.budget.successful_read_only_script_limit,
            )
            if script_instruction is not None:
                checkpoint.messages.append(script_instruction)
                self.trace_emitter.emit(
                    checkpoint.run_id,
                    RuntimeEventType.PIPELINE_STAGE,
                    {
                        "stage": "artifact_progress",
                        "status": "read_only_script_budget_reached",
                        "tool_name": call.tool_name,
                        "limit": self.budget.successful_read_only_script_limit,
                    },
                    visibility=EventVisibility.DEBUG,
                    trace_id=_trace_id(checkpoint),
                )
        if (
            call.tool_name == "cowork.file.search_text"
            and successful_uri_read_count(checkpoint.metadata, call)
            >= self.budget.successful_search_uri_limit
        ):
            self._disable_exhausted_file_reads(checkpoint)
        if call.tool_name == _WEB_SEARCH_TOOL:
            successful_call_signature = web_search_call_signature(call)
            if successful_call_signature is not None:
                record_successful_web_search_call(checkpoint.metadata, successful_call_signature)
            search_budget_instruction = record_successful_web_search_total(
                checkpoint.metadata,
                limit=self.budget.max_successful_web_searches,
            )
            if search_budget_instruction is not None:
                checkpoint.messages.append(search_budget_instruction)
                self._disable_exhausted_web_search(checkpoint)
                self.trace_emitter.emit(
                    checkpoint.run_id,
                    RuntimeEventType.PIPELINE_STAGE,
                    {
                        "stage": "web_search_guard",
                        "status": "search_budget_reached",
                        "tool_name": call.tool_name,
                        "limit": self.budget.max_successful_web_searches,
                    },
                    visibility=EventVisibility.DEBUG,
                    trace_id=_trace_id(checkpoint),
                )
            search_signature = _web_search_result_signature(call, result)
            if search_signature is not None:
                search_instruction = record_successful_web_search(
                    checkpoint.metadata,
                    signature=search_signature,
                    threshold=self.budget.successful_web_search_repeat_threshold,
                )
                if search_instruction is not None:
                    checkpoint.messages.append(search_instruction)
                    self.trace_emitter.emit(
                        checkpoint.run_id,
                        RuntimeEventType.PIPELINE_STAGE,
                        {
                            "stage": "web_search_guard",
                            "status": "repeat_result_bundle_replan",
                            "tool_name": call.tool_name,
                            "signature": search_signature,
                            "count": successful_web_search_count(checkpoint.metadata, search_signature),
                        },
                        visibility=EventVisibility.DEBUG,
                        trace_id=_trace_id(checkpoint),
                    )
        self.state_store.save(checkpoint)
        return _ToolExecutionOutcome(tool_result=result, followup_results=followup_results)

    def _search_only_requested(self, checkpoint: LoopCheckpoint, call: ToolCall) -> bool:
        # Ordinary research intentionally combines search snippets with a
        # bounded Runtime-owned public page read.  ``search_only`` remains
        # meaningful for all other requests, including an explicit request to
        # summarize search snippets without opening results.
        if checkpoint.metadata.get("research_search_first") is True:
            return False
        raw_search_only = call.arguments.get("search_only")
        if isinstance(raw_search_only, bool):
            return raw_search_only
        raw_need_page_content = call.arguments.get("need_page_content")
        if isinstance(raw_need_page_content, bool):
            return not raw_need_page_content
        raw_meta = call.metadata.get("search_only") if isinstance(call.metadata, dict) else None
        return raw_meta is True

    async def _auto_fetch_search_results(
        self,
        checkpoint: LoopCheckpoint,
        search_call: ToolCall,
        search_result: ToolResult,
        cancellation_token: CancellationToken,
    ) -> _ToolExecutionOutcome:
        candidates = self._search_followup_candidates(search_call, search_result)
        if not candidates:
            return _ToolExecutionOutcome()
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            {
                "stage": "search_followup",
                "status": "scheduled",
                "tool_name": search_call.tool_name,
                "candidate_count": len(candidates),
            },
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
        )
        followup_results: List[ToolResult] = []
        for index, candidate in enumerate(candidates):
            cancellation_token.raise_if_cancelled()
            try:
                assert_tool_call_allowed(checkpoint, self.budget)
            except LoopBudgetExceeded as exc:
                terminal = self._finish_for_budget(checkpoint, exc)
                return _ToolExecutionOutcome(
                    followup_results=followup_results,
                    terminal_result=terminal,
                )

            request_arguments = self._build_search_followup_arguments(
                candidate,
                search_call=search_call,
                search_result=search_result,
                index=index,
            )
            fetch_call = ToolCall(
                tool_call_id=str(new_contract_id("tool_call")),
                run_id=checkpoint.run_id,
                tool_name=_WEB_FETCH_PAGE_TOOL,
                arguments=request_arguments,
                status=ToolCallStatus.RUNNING,
                metadata={
                    "provider_tool_call_id": f"{search_call.tool_call_id}:followup:{index + 1}",
                    "search_followup": True,
                    "search_result_index": index,
                    "runtime_lightweight_public_fetch": checkpoint.metadata.get("research_search_first") is True,
                },
            )
            lightweight_call_ids: list[str] | None = None
            if checkpoint.metadata.get("research_search_first") is True:
                # This ephemeral id is the policy proof that this fetch was
                # generated from a completed public search, rather than being
                # a hidden model-authored crawler call.
                lightweight_call_ids = list(
                    checkpoint.metadata.get("runtime_lightweight_fetch_call_ids") or []
                )
                lightweight_call_ids.append(fetch_call.tool_call_id)
                checkpoint.metadata["runtime_lightweight_fetch_call_ids"] = lightweight_call_ids
            checkpoint.tool_calls += 1
            self.trace_emitter.emit(
                checkpoint.run_id,
                RuntimeEventType.TOOL_CALL_CREATED,
                {
                    "tool_call_id": fetch_call.tool_call_id,
                    "tool_name": fetch_call.tool_name,
                    "arguments": fetch_call.arguments,
                },
                visibility=EventVisibility.DEBUG,
                trace_id=_trace_id(checkpoint),
                redact=True,
            )
            followup_progress = tool_started_progress(fetch_call)
            self._emit_user_progress(
                checkpoint,
                phase="tool",
                title=str(followup_progress["title"]),
                content=str(followup_progress["content"]),
                step_id=fetch_call.tool_call_id,
                message_source="tool",
                display=followup_progress["display"],
            )
            try:
                outcome = await self._execute_tool(
                    checkpoint,
                    fetch_call,
                    cancellation_token,
                )
            finally:
                if lightweight_call_ids is not None:
                    checkpoint.metadata["runtime_lightweight_fetch_call_ids"] = [
                        call_id
                        for call_id in lightweight_call_ids
                        if call_id != fetch_call.tool_call_id
                    ]
                    # Do not leave a completed/failed Run with a reusable
                    # authorization token in its persisted model context.
                    self.state_store.save(checkpoint)
            if outcome.tool_result is not None:
                followup_results.append(outcome.tool_result)
            if outcome.followup_results:
                followup_results.extend(outcome.followup_results)
            if outcome.terminal_result is not None:
                return _ToolExecutionOutcome(
                    followup_results=followup_results,
                    terminal_result=outcome.terminal_result,
                )
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            {
                "stage": "search_followup",
                "status": "completed",
                "tool_name": search_call.tool_name,
                "candidate_count": len(candidates),
            },
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
        )
        return _ToolExecutionOutcome(followup_results=followup_results)

    def _search_followup_candidates(
        self,
        search_call: ToolCall,
        search_result: ToolResult,
    ) -> List[dict[str, Any]]:
        output = search_result.output if isinstance(search_result.output, dict) else {}
        if not isinstance(output, dict):
            return []
        raw_results = output.get("results")
        if not isinstance(raw_results, list) or not raw_results:
            return []

        query = _normalize_search_text(
            str(output.get("query") or search_call.arguments.get("query") or "")
        )
        required_site = _search_site_constraint(query)
        scored: list[tuple[int, int, dict[str, Any]]] = []
        seen_urls: set[str] = set()
        for index, raw in enumerate(raw_results):
            if not isinstance(raw, dict):
                continue
            url = str(raw.get("url") or "").strip()
            if not url:
                continue
            try:
                safe_url = validate_public_url_syntax(url)
            except OutboundUrlError:
                continue
            if required_site is not None and not _same_or_subdomain(safe_url, required_site):
                continue
            if safe_url in seen_urls:
                continue
            seen_urls.add(safe_url)
            title = _normalize_search_text(str(raw.get("title") or ""))
            snippet = _normalize_search_text(str(raw.get("snippet") or ""))
            score = _score_search_candidate(query, title, snippet, index)
            if not _search_candidate_has_signal(query, safe_url, title, snippet):
                continue
            scored.append(
                (
                    score,
                    index,
                    {
                        "url": safe_url,
                        "title": title,
                        "snippet": snippet,
                        "provider": str(output.get("provider") or ""),
                        "query": query,
                    },
                )
            )
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [item[2] for item in scored[:_WEB_SEARCH_FOLLOWUP_LIMIT]]

    def _build_search_followup_arguments(
        self,
        candidate: dict[str, Any],
        *,
        search_call: ToolCall,
        search_result: ToolResult,
        index: int,
    ) -> dict[str, Any]:
        query = str(candidate.get("query") or search_call.arguments.get("query") or "")
        provider = str(candidate.get("provider") or "")
        request = {
            "version": "cowork.crawler.v1",
            "request_id": new_contract_id("crawl_request"),
            "tool_name": _WEB_FETCH_PAGE_TOOL,
            "url": str(candidate.get("url") or ""),
            "respect_robots": True,
            "allow_external_sources": False,
            "metadata": {
                # Metadata is the backwards-compatible extension point in the
                # v1 crawler request. Some already-running Desktop sidecars
                # still expose a v1 manifest without top-level source_type.
                # Keep this semantic here so those hosts can fetch the page
                # instead of rejecting the same automatic follow-up repeatedly.
                "source_type": "search_result",
                "search_followup": True,
                "search_query": query,
                "search_provider": provider,
                "search_result_index": index,
                "search_result_signature": _web_search_result_signature(search_call, search_result),
                # Search-followup fetches never escalate into Desktop browser
                # takeover. Login, consent, CAPTCHA and HTTP authorization
                # barriers are reported as skipped sources instead.
                "research_lightweight_public_fetch": True,
                "browser_fallback": "off",
                "skip_user_action_required": True,
            },
        }
        return request

    def _suppress_duplicate_read(
        self,
        checkpoint: LoopCheckpoint,
        call: ToolCall,
        signature: str,
    ) -> ToolResult:
        """闭合重复工具调用，但不再次插入相同的大体积结果。"""

        result = suppressed_duplicate_result(call)
        self.context_builder.append_tool_result(checkpoint, result)
        instruction = no_progress_instruction(checkpoint.metadata, signature, call.tool_name)
        if instruction is not None:
            checkpoint.messages.append(instruction)
        self.state_store.save(checkpoint)
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.TOOL_RESULT_CREATED,
            tool_result_payload(result),
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
            redact=True,
        )
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            {
                "stage": "no_progress_guard",
                "status": "duplicate_read_suppressed",
                "tool_name": call.tool_name,
            },
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
        )
        return result

    def _suppress_duplicate_web_search(
        self,
        checkpoint: LoopCheckpoint,
        call: ToolCall,
        signature: str,
    ) -> ToolResult:
        """闭合同一搜索请求，复用已返回的公开资料而不重复执行网络调用。"""

        result = suppressed_duplicate_result(call)
        self.context_builder.append_tool_result(checkpoint, result)
        checkpoint.messages.append(
            {
                "role": "system",
                "content": (
                    "RUNTIME_WEB_SEARCH_DUPLICATE: This exact web search already succeeded in this run. "
                    "Do not repeat it. Use the existing search results and fetched page evidence to continue."
                ),
                "metadata": {"_runtime_web_search_duplicate": True, "signature": signature},
            }
        )
        self.state_store.save(checkpoint)
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.TOOL_RESULT_CREATED,
            tool_result_payload(result),
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
            redact=True,
        )
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            {
                "stage": "web_search_guard",
                "status": "duplicate_search_suppressed",
                "tool_name": call.tool_name,
            },
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
        )
        return result

    def _suppress_redundant_crawler_request(
        self,
        checkpoint: LoopCheckpoint,
        call: ToolCall,
        signature: str,
    ) -> ToolResult:
        """Stop an identical successful crawler request before it re-enters Host.

        Session datasets can be partial by design, so generic dataset reuse
        cannot reject every second crawl. Within one Run, however, the same
        request has no new input and cannot produce new evidence. The model is
        returned to the already collected result for a bounded final answer.
        """

        result = suppressed_duplicate_result(call)
        self.context_builder.append_tool_result(checkpoint, result)
        checkpoint.messages.append(
            {
                "role": "system",
                "content": (
                    "RUNTIME_CRAWLER_DUPLICATE: This exact crawler request already succeeded in this run. "
                    "Do not call crawler tools again. Use the collected records, coverage, and diagnostics already "
                    "in context to answer the user's request. A new network crawl requires an explicit user refresh "
                    "or a continuation with a different target."
                ),
                "metadata": {"_runtime_crawler_duplicate": True, "signature": signature},
            }
        )
        self.state_store.save(checkpoint)
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.TOOL_RESULT_CREATED,
            tool_result_payload(result),
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
            redact=True,
        )
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            {
                "stage": "crawler_guard",
                "status": "duplicate_request_suppressed",
                "tool_name": call.tool_name,
                "signature": signature,
            },
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
        )
        return result

    def _successful_uri_read_limit(self, call: ToolCall) -> int:
        if call.tool_name == "cowork.file.search_text":
            return self.budget.successful_search_uri_limit
        return self.budget.successful_read_uri_limit

    def _suppress_excessive_uri_read(
        self,
        checkpoint: LoopCheckpoint,
        call: ToolCall,
    ) -> ToolResult:
        """抑制同一文件的多策略重复读取，并要求模型切换为定位或写入。"""

        result = suppressed_excessive_uri_read_result(call)
        self.context_builder.append_tool_result(checkpoint, result)
        instruction = excessive_uri_read_instruction(checkpoint.metadata, call)
        if instruction is not None:
            checkpoint.messages.append(instruction)
        if call.tool_name == "cowork.file.search_text":
            self._disable_exhausted_file_reads(checkpoint)
        self.state_store.save(checkpoint)
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.TOOL_RESULT_CREATED,
            tool_result_payload(result),
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
            redact=True,
        )
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            {
                "stage": "no_progress_guard",
                "status": "read_uri_budget_suppressed",
                "tool_name": call.tool_name,
                "uri": call.arguments.get("uri"),
            },
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
            redact=True,
        )
        return result

    def _suppress_excessive_write(
        self,
        checkpoint: LoopCheckpoint,
        call: ToolCall,
    ) -> ToolResult:
        """抑制同一文件的重复整体重写，把模型指回磁盘上的最新内容。"""

        result = suppressed_excessive_write_result(call)
        self.context_builder.append_tool_result(checkpoint, result)
        self.state_store.save(checkpoint)
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.TOOL_RESULT_CREATED,
            tool_result_payload(result),
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
            redact=True,
        )
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            {
                "stage": "no_progress_guard",
                "status": "write_uri_budget_suppressed",
                "tool_name": call.tool_name,
                "uri": call.arguments.get("uri"),
                "limit": self.budget.successful_write_uri_limit,
            },
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
            redact=True,
        )
        return result

    def _suppress_excessive_research(
        self,
        checkpoint: LoopCheckpoint,
        call: ToolCall,
    ) -> ToolResult:
        """在模型忽略目录收窄时闭合调用，避免远程研究进入无界循环。"""

        result = suppressed_excessive_research_result(call)
        self.context_builder.append_tool_result(checkpoint, result)
        self._disable_exhausted_market_research(checkpoint)
        self.state_store.save(checkpoint)
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.TOOL_RESULT_CREATED,
            tool_result_payload(result),
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
            redact=True,
        )
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            {
                "stage": "research_budget",
                "status": "research_budget_suppressed",
                "tool_name": call.tool_name,
                "limit": self.budget.successful_research_limit,
            },
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
        )
        return result

    def _suppress_excessive_read_only_script(
        self,
        checkpoint: LoopCheckpoint,
        call: ToolCall,
    ) -> ToolResult:
        """只关闭无输出的检查脚本，不能删除实际生成 Artifact 的脚本能力。"""

        result = suppressed_excessive_read_only_script_result(call)
        self.context_builder.append_tool_result(checkpoint, result)
        instruction = record_successful_read_only_script(
            checkpoint.metadata,
            limit=self.budget.successful_read_only_script_limit,
        )
        if instruction is not None:
            checkpoint.messages.append(instruction)
        self.state_store.save(checkpoint)
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.TOOL_RESULT_CREATED,
            tool_result_payload(result),
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
            redact=True,
        )
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            {
                "stage": "artifact_progress",
                "status": "read_only_script_budget_suppressed",
                "tool_name": call.tool_name,
                "limit": self.budget.successful_read_only_script_limit,
            },
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
        )
        return result

    def _disable_exhausted_file_reads(self, checkpoint: LoopCheckpoint) -> None:
        """搜索预算耗尽后从后续模型工具目录移除读取能力。

        仅抑制 Host 执行仍会让模型反复选择同一工具；目录收窄让 required
        Artifact 的下一轮只能使用已有证据推进到 edit/write/script。
        """

        disabled = checkpoint.metadata.get("disabled_tool_names")
        if not isinstance(disabled, list):
            disabled = []
            checkpoint.metadata["disabled_tool_names"] = disabled
        for tool_name in (
            "cowork.file.find_name",
            "cowork.file.list_dir",
            "cowork.file.preview",
            "cowork.file.read_file",
            "cowork.file.read_range",
            "cowork.file.stat",
        ):
            if tool_name not in disabled:
                disabled.append(tool_name)

    def _available_tool_names(self, checkpoint: LoopCheckpoint) -> list[str] | None:
        """Use the frozen Catalog when present; never invent suggestions outside it."""

        snapshot = checkpoint.metadata.get("catalog_snapshot")
        manifests = snapshot.get("manifests") if isinstance(snapshot, dict) else None
        if not isinstance(manifests, list):
            resolver = getattr(self.tool_runner.tool_host, "available_tool_names", None)
            raw_names = resolver() if callable(resolver) else None
            manifests = raw_names if isinstance(raw_names, list) else None
        if not isinstance(manifests, list):
            return None
        from .review_scope import visible_manifests

        manifests = visible_manifests(manifests, checkpoint.metadata)
        names = []
        for manifest in manifests:
            name = manifest.get("name") if isinstance(manifest, dict) else (
                manifest if isinstance(manifest, str) else getattr(manifest, "name", None)
            )
            if isinstance(name, str) and name:
                names.append(name)
        return names

    def _set_review_agent_phase(
        self,
        checkpoint: LoopCheckpoint,
        phase: str,
        *,
        reason: str,
    ) -> None:
        """Persist a secret-free phase change without mutating the catalog."""

        if not review_agent_enabled(checkpoint.metadata):
            return
        if checkpoint.metadata.get("review_agent_phase") == phase:
            return
        checkpoint.metadata["review_agent_phase"] = phase
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            {
                "stage": "review_agent_scope",
                "status": "phase_changed",
                "phase": phase,
                "reason": reason,
            },
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
        )

    def _record_review_crawler_failure(
        self,
        checkpoint: LoopCheckpoint,
        call: ToolCall,
        error: RuntimeError,
    ) -> None:
        """Record one opaque failure fingerprint and open at most one replan."""

        if not review_agent_enabled(checkpoint.metadata):
            return
        signature = crawler_call_signature(call)
        if signature is None:
            return
        details = error.details if isinstance(error.details, dict) else {}
        reason_code = str(details.get("reason_code") or error.code.value)
        failures = checkpoint.metadata.get("review_agent_failed_crawler_signatures")
        if not isinstance(failures, dict):
            failures = {}
            checkpoint.metadata["review_agent_failed_crawler_signatures"] = failures
        failures.setdefault(signature, reason_code[:120])

        # Login/CAPTCHA waits are handled by the existing explicit Desktop
        # interaction path. All policy and source denials must not gain a
        # model retry window, even if a general recovery policy would offer a
        # generic alternative in a wider catalog.
        if self._review_failure_must_stop(error):
            self._set_review_agent_phase(checkpoint, REVIEW_AGENT_PHASE_DELIVERY, reason="non_retryable_crawler_failure")
            return

        attempts = int(checkpoint.metadata.get("review_agent_replan_attempts") or 0)
        if attempts < REVIEW_AGENT_MAX_REPLAN_ATTEMPTS:
            checkpoint.metadata["review_agent_replan_attempts"] = attempts + 1
            self._set_review_agent_phase(checkpoint, REVIEW_AGENT_PHASE_REPLAN, reason="crawler_failure_replan")
        else:
            self._set_review_agent_phase(checkpoint, REVIEW_AGENT_PHASE_DELIVERY, reason="crawler_replan_exhausted")

    @staticmethod
    def _review_failure_must_stop(error: RuntimeError) -> bool:
        details = error.details if isinstance(error.details, dict) else {}
        reason_code = str(details.get("reason_code") or "")
        return error.code is RuntimeErrorCode.POLICY_DENIED or reason_code.startswith(
            (
                "crawler.policy.",
                "crawler.interaction.",
                "crawler.provider.unavailable",
                "review.",
            )
        )

    def _review_duplicate_crawler_failure(
        self,
        checkpoint: LoopCheckpoint,
        call: ToolCall,
    ) -> ToolResult | None:
        """Stop a repeated failed normalized review request before Host access."""

        if not review_agent_enabled(checkpoint.metadata) or call.tool_name not in _CRAWLER_TOOLS:
            return None
        signature = crawler_call_signature(call)
        failures = checkpoint.metadata.get("review_agent_failed_crawler_signatures")
        if signature is None or not isinstance(failures, dict) or signature not in failures:
            return None
        error = RuntimeError(
            code=RuntimeErrorCode.TOOL_FAILED,
            message="repeated review crawler failure was suppressed before network access",
            retryable=False,
            category="fatal",
            details={"reason_code": "review.crawler_duplicate_failure_suppressed", "do_not_retry": True},
        )
        result = ToolResult(
            tool_call_id=call.tool_call_id,
            run_id=call.run_id,
            status=ToolCallStatus.FAILED,
            error=error,
            metadata={"review_scope": "duplicate_failure_suppressed"},
        )
        self.context_builder.append_tool_result(checkpoint, result)
        self.state_store.save(checkpoint)
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.TOOL_RESULT_CREATED,
            tool_result_payload(result),
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
            redact=True,
        )
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            {
                "stage": "review_agent_scope",
                "status": "duplicate_failure_suppressed",
                "tool_name": call.tool_name,
            },
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
        )
        return result

    def _advance_review_evidence_phase(
        self,
        checkpoint: LoopCheckpoint,
        call: ToolCall,
        result: ToolResult,
    ) -> object | None:
        """Advance only from bounded evidence facts, never from model prose.

        A successful inspection intentionally remains in ``replan`` so the
        model can consume its Runtime-issued candidates.  Once it selects the
        final candidate, DatasetVersion creation happens here through the
        Runtime-owned committer rather than model-controlled arguments.
        """

        if not review_evidence_enabled(checkpoint.metadata) or not isinstance(result.output, Mapping):
            return None
        try:
            evidence = ReviewEvidenceResult.model_validate(result.output)
        except (TypeError, ValueError):
            return None
        if evidence.status == "failed":
            checkpoint.metadata.pop(REVIEW_EVIDENCE_ALLOWED_TOOLS_KEY, None)
            self._set_review_agent_phase(checkpoint, REVIEW_AGENT_PHASE_DELIVERY, reason="review_evidence_failed")
            return None
        if call.tool_name == CRAWLER_REVIEW_EXTRACT_CANDIDATES_TOOL:
            checkpoint.metadata.pop(REVIEW_EVIDENCE_ALLOWED_TOOLS_KEY, None)
            committed = self._commit_review_evidence_dataset(checkpoint, evidence)
            self._set_review_agent_phase(
                checkpoint,
                REVIEW_AGENT_PHASE_DELIVERY,
                reason="review_candidates_committed" if committed is not None else "review_candidates_validation_denied",
            )
            return committed
        allowed = _review_evidence_tools_from_candidates(evidence)
        if not allowed:
            checkpoint.metadata.pop(REVIEW_EVIDENCE_ALLOWED_TOOLS_KEY, None)
            self._set_review_agent_phase(checkpoint, REVIEW_AGENT_PHASE_DELIVERY, reason="review_evidence_no_next_step")
            return None
        checkpoint.metadata[REVIEW_EVIDENCE_ALLOWED_TOOLS_KEY] = sorted(allowed)
        self._set_review_agent_phase(checkpoint, REVIEW_AGENT_PHASE_REPLAN, reason="review_evidence_available")
        return None

    def _commit_review_evidence_dataset(
        self,
        checkpoint: LoopCheckpoint,
        evidence: ReviewEvidenceResult,
    ) -> object | None:
        """Commit only a final Runtime-issued evidence ref to Session storage."""

        source_url = checkpoint.metadata.get("review_agent_source_url")
        evidence_ref = evidence.evidence_ref.ref_id if evidence.evidence_ref is not None else None
        if self.review_evidence_committer is None:
            checkpoint.metadata["review_evidence_commit"] = {
                "status": "denied",
                "reason_code": "review.commit_service_unavailable",
            }
            return None
        if not isinstance(source_url, str) or not source_url or not evidence_ref:
            checkpoint.metadata["review_evidence_commit"] = {
                "status": "denied",
                "reason_code": "review.commit_evidence_unavailable",
            }
            return None
        try:
            committed = self.review_evidence_committer.commit(
                run_id=checkpoint.run_id,
                session_id=checkpoint.session_id,
                source_url=source_url,
                evidence_ref_id=evidence_ref,
            )
        except ReviewEvidenceCommitError as exc:
            checkpoint.metadata["review_evidence_commit"] = {
                "status": "denied",
                "reason_code": exc.code,
            }
            self.trace_emitter.emit(
                checkpoint.run_id,
                RuntimeEventType.PIPELINE_STAGE,
                {
                    "stage": "review_evidence_commit",
                    "status": "denied",
                    "reason_code": exc.code,
                },
                visibility=EventVisibility.DEBUG,
                trace_id=_trace_id(checkpoint),
            )
            return None
        checkpoint.metadata["review_evidence_commit"] = {
            "status": "committed",
            "dataset_id": committed.dataset.ref.dataset_id,
            "dataset_version_id": committed.dataset.ref.dataset_version_id,
            "record_count": committed.dataset.ref.record_count,
            "added_record_count": committed.added_record_count,
            "rejected_record_counts": committed.rejected_record_counts,
        }
        checkpoint.metadata["crawler_dataset"] = committed.dataset.ref.model_dump(mode="json")
        return committed

    def _record_review_evidence_commit(self, checkpoint: LoopCheckpoint, committed: object) -> None:
        """Emit deterministic delivery facts from a committed DatasetVersion."""

        dataset = getattr(committed, "dataset", None)
        if dataset is None:
            return
        artifact = crawler_dataset_csv_artifact(dataset, run_id=checkpoint.run_id)
        if not any(existing.artifact_id == artifact.artifact_id for existing in checkpoint.artifact_refs):
            checkpoint.artifact_refs.append(artifact)
        if self.context_builder.session_store is not None:
            self.context_builder.session_store.append_artifact_ref(checkpoint.session_id, artifact)
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.ARTIFACT_CREATED,
            artifact.model_dump(mode="json"),
            trace_id=_trace_id(checkpoint),
        )
        content = crawler_dataset_delivery_content(dataset)
        checkpoint.final_content = content
        checkpoint.metadata["review_evidence_commit_terminal"] = True
        self.context_builder.append_assistant(checkpoint, content, metadata={
            "_runtime_review_evidence_commit": True,
        })
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.TURN_ASSISTANT_TEXT,
            {
                "content": content,
                "metadata": {"source": "runtime", "reason": "review_evidence_dataset_committed"},
            },
            trace_id=_trace_id(checkpoint),
        )
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            {
                "stage": "review_evidence_commit",
                "status": "committed",
                "dataset_id": dataset.ref.dataset_id,
                "dataset_version_id": dataset.ref.dataset_version_id,
                "record_count": dataset.ref.record_count,
            },
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
        )

    @staticmethod
    def _review_evidence_budget_exhausted(
        checkpoint: LoopCheckpoint,
        call: ToolCall,
    ) -> ToolResult | None:
        """Enforce the fixed inspect → action → select → extract ceiling.

        The general Run budget remains the outer guard.  This smaller budget
        prevents a sequence of distinct pagination candidates from consuming
        all generic Agent turns while still allowing one bounded public-review
        collection chain to reach the deterministic validator in ISSUE-104.
        """

        if (
            not review_evidence_enabled(checkpoint.metadata)
            or call.tool_name not in _REVIEW_EVIDENCE_TOOLS
            or int(checkpoint.metadata.get(REVIEW_EVIDENCE_TOOL_CALLS_KEY) or 0) < REVIEW_EVIDENCE_MAX_TOOL_CALLS
        ):
            return None
        error = RuntimeError(
            code=RuntimeErrorCode.BUDGET_EXCEEDED,
            message="review evidence tool budget is exhausted",
            retryable=False,
            category="fatal",
            details={"reason_code": "review.evidence_tool_budget_exhausted", "do_not_retry": True},
        )
        return ToolResult(
            tool_call_id=call.tool_call_id,
            run_id=call.run_id,
            status=ToolCallStatus.FAILED,
            error=error,
            metadata={"review_scope": "evidence_tool_budget_exhausted"},
        )

    def _crawler_recovery_guidance(
        self,
        checkpoint: LoopCheckpoint,
        call: ToolCall,
        result: ToolResult,
        *,
        projection: _CrawlerRecoveryProjection | None,
    ) -> RecoveryGuidance | None:
        """Attach guidance to crawler success-state barriers without changing them.

        Waiting for Desktop input remains a successful crawler observation so
        its opaque interaction resume contract is preserved. A quality stop is
        also a valid partial observation, not a failed extraction result.
        """

        if projection is None or projection.convert_to_failed:
            return None
        guidance = self.recovery_manager.failure_guidance(
            call,
            projection.error,
            category=classify_runtime_error(projection.error),
            available_tools=self._available_tool_names(checkpoint),
        )
        return guidance

    def _disable_unavailable_provider_tool(
        self,
        checkpoint: LoopCheckpoint,
        *,
        tool_name: str,
        reason_code: str,
    ) -> None:
        """Remove a confirmed-unavailable Provider tool from Context and Host replay.

        This is intentionally limited to terminal Provider availability facts.
        Transient transport failures remain eligible for the existing trusted
        manifest retry path and are not converted into a Run-wide disable.
        """

        disabled = checkpoint.metadata.get("disabled_tool_names")
        if not isinstance(disabled, list):
            disabled = []
            checkpoint.metadata["disabled_tool_names"] = disabled
        if tool_name not in disabled:
            disabled.append(tool_name)
        self.recovery_manager.block_tool(
            checkpoint.recovery_state,
            tool_name,
            reason_code=reason_code,
        )
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            {
                "stage": "provider_recovery",
                "status": "tool_disabled_for_run",
                "tool_name": tool_name,
                "reason_code": reason_code,
            },
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
        )

    def _disable_local_script_after_security_failure(
        self,
        checkpoint: LoopCheckpoint,
        *,
        tool_name: str,
        reason_code: str,
    ) -> None:
        """Script safety boundaries remove the tool from Context and block Host replays."""

        disabled = checkpoint.metadata.get("disabled_tool_names")
        if not isinstance(disabled, list):
            disabled = []
            checkpoint.metadata["disabled_tool_names"] = disabled
        if tool_name not in disabled:
            disabled.append(tool_name)
        self.recovery_manager.block_tool(
            checkpoint.recovery_state,
            tool_name,
            reason_code=reason_code,
        )
        self.context_builder.append_system(
            checkpoint,
            (
                f"RUNTIME_LOCAL_SCRIPT_SECURITY_STOP: {tool_name} reached a controlled script safety boundary. "
                "Do not retry this script tool for the current Run, even with changed code or arguments. "
                "Do not bypass the sandbox, Workspace Grant, output declaration, allow-list, network, or process restrictions. "
                "Use only an already-authorized alternative from the current Catalog, or state the needed human help."
            ),
            metadata={
                "_runtime_local_script_security_stop": True,
                "tool_name": tool_name,
                "reason_code": reason_code,
            },
        )
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            {
                "stage": "local_script_security",
                "status": "script_disabled_for_run",
                "tool_name": tool_name,
                "reason_code": reason_code,
            },
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
        )

    def _disable_exhausted_web_search(self, checkpoint: LoopCheckpoint) -> None:
        """搜索预算耗尽后从后续模型工具目录移除搜索能力。"""

        disabled = checkpoint.metadata.get("disabled_tool_names")
        if not isinstance(disabled, list):
            disabled = []
            checkpoint.metadata["disabled_tool_names"] = disabled
        if _WEB_SEARCH_TOOL not in disabled:
            disabled.append(_WEB_SEARCH_TOOL)

    def _suppress_excessive_web_search(
        self,
        checkpoint: LoopCheckpoint,
        call: ToolCall,
    ) -> ToolResult:
        result = suppressed_excessive_web_search_result(call)
        self.context_builder.append_tool_result(checkpoint, result)
        self._disable_exhausted_web_search(checkpoint)
        checkpoint.messages.append(
            {
                "role": "system",
                "content": (
                    "RUNTIME_WEB_SEARCH_TOTAL_BUDGET: Do not call cowork.web.search again in this run. "
                    "Answer from existing fetched page content and evidence, or state that the evidence is insufficient."
                ),
                "metadata": {"_runtime_web_search_total_budget": True},
            }
        )
        self.state_store.save(checkpoint)
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.TOOL_RESULT_CREATED,
            tool_result_payload(result),
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
            redact=True,
        )
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            {
                "stage": "web_search_guard",
                "status": "search_budget_suppressed",
                "tool_name": call.tool_name,
                "limit": self.budget.max_successful_web_searches,
            },
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
        )
        return result

    def _disable_exhausted_market_research(self, checkpoint: LoopCheckpoint) -> None:
        """额度用尽后从模型目录移除远程研究；Host 仍保留防御性抑制。"""

        disabled = checkpoint.metadata.get("disabled_tool_names")
        if not isinstance(disabled, list):
            disabled = []
            checkpoint.metadata["disabled_tool_names"] = disabled
        if "cowork.research.market" not in disabled:
            disabled.append("cowork.research.market")

    def _disable_unavailable_market_research(
        self,
        checkpoint: LoopCheckpoint,
        error: RuntimeError,
    ) -> None:
        """对认证/路由失败的 Platform 调研启用一次性真实 Web 降级。"""

        self._disable_exhausted_market_research(checkpoint)
        if checkpoint.metadata.get("market_research_fallback_notice") is True:
            return
        checkpoint.metadata["market_research_fallback_notice"] = True
        status = error.details.get("http_status")
        status_text = f"HTTP {status}" if status else "Platform session/route unavailable"
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            {
                "stage": "research_fallback",
                "status": "platform_unavailable",
                "tool_name": _MARKET_RESEARCH_TOOL,
                "fallback_tool": _MARKET_RESEARCH_FALLBACK_TOOL,
                "http_status": status,
            },
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
        )
        checkpoint.messages.append(
            {
                "role": "system",
                "content": (
                    f"RUNTIME_PLATFORM_RESEARCH_FALLBACK: {_MARKET_RESEARCH_TOOL} is unavailable ({status_text}). "
                    f"Do not call it again in this run. Use {_MARKET_RESEARCH_FALLBACK_TOOL} for live web sources "
                    "when current external facts are still needed; clearly distinguish web search evidence from "
                    "the authenticated Platform report and never fabricate missing sources."
                ),
                "metadata": {"_runtime_platform_research_fallback": True},
            }
        )

    def _grant_tool_failure_reprieve(
        self,
        checkpoint: LoopCheckpoint,
        *,
        call: ToolCall,
        runtime_error: RuntimeError,
        signature: str,
        trigger: str,
    ) -> bool:
        decision = grant_tool_failure_reprieve(
            checkpoint.metadata,
            tool_name=call.tool_name,
            error_code=runtime_error.code.value,
            signature=signature,
            trigger=trigger,
        )
        if decision is None:
            return False
        checkpoint.messages.append(decision.message)
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            decision.event_payload,
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
        )
        return True

    def _append_recovery_reminder(self, checkpoint: LoopCheckpoint, guidance) -> None:
        """Inject each loop-pattern escalation exactly once into model context."""

        if guidance is None or not guidance.reason_code.startswith("loop.pattern."):
            return
        emitted = checkpoint.metadata.get("recovery_reminders")
        if not isinstance(emitted, list):
            emitted = []
            checkpoint.metadata["recovery_reminders"] = emitted
        fingerprint = f"{guidance.reason_code}:{guidance.stage}:{guidance.forbidden_call_signature or ''}"
        if fingerprint in emitted:
            return
        emitted.append(fingerprint)
        self.context_builder.append_system(
            checkpoint,
            guidance.instruction,
            metadata={
                "_runtime_system_reminder": True,
                "recovery_reason_code": guidance.reason_code,
                "recovery_action": guidance.action,
                "recovery_stage": guidance.stage,
                "occurrence_count": guidance.occurrence_count,
            },
        )
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            {
                "stage": "system_reminder",
                "status": guidance.stage,
                "reason_code": guidance.reason_code,
                "action": guidance.action,
                "occurrence_count": guidance.occurrence_count,
                "tool_name": _recovery_tool_name(guidance.instruction),
            },
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
        )

    def _adaptive_tool_failure_decision(
        self,
        checkpoint: LoopCheckpoint,
        call: ToolCall,
        error: RuntimeError | None,
        execution_contract: dict[str, Any],
        fingerprint: dict[str, Any] | None,
        guidance,
    ) -> AdaptiveDecision | None:
        """把工具失败映射为受限策略事实，不把参数带入策略。"""

        if error is None:
            return None
        details = error.details if isinstance(error.details, dict) else {}
        reason_code = (
            fingerprint.get("reason_code")
            if isinstance(fingerprint, dict)
            else details.get("reason_code")
        )
        if not isinstance(reason_code, str) or not reason_code:
            reason_code = error.code.value
        # Provider availability and script security already have a stronger
        # ISSUE-057/058 enforcement path below: disable the capability first,
        # then let the existing bounded fallback/terminal handling run.
        if reason_code.startswith(("provider.", "script.security.")):
            return None
        incident_fingerprint = None
        if isinstance(fingerprint, dict):
            resource_hash = fingerprint.get("resource_hash") or "none"
            incident_fingerprint = f"{call.tool_name}:{resource_hash}:{reason_code}"
        stage = getattr(guidance, "stage", None)
        action = getattr(guidance, "action", None)
        safe_contract = (
            execution_contract.get("side_effect") == "none"
            and execution_contract.get("idempotency") in {"intrinsic", "keyed"}
        )
        if not safe_contract and stage not in {"terminate", "restrict"} and not bool(details.get("do_not_retry")):
            # 未声明安全契约的 Provider 不能获得自动诊断窗口；保留既有
            # RecoveryManager 行为，避免把未知副作用误当成幂等失败。
            return None
        facts = AdaptiveReasoningInput(
            trigger="tool_failure",
            reason_code=reason_code,
            tool_name=call.tool_name,
            error_code=error.code.value,
            recovery_stage=stage,
            recovery_action=action,
            do_not_retry=bool(details.get("do_not_retry")) or error.code in {
                RuntimeErrorCode.POLICY_DENIED,
                RuntimeErrorCode.APPROVAL_EXPIRED,
            },
            recovery_exhausted=stage == "terminate" or action == "stop",
            no_progress=stage == "restrict",
            cancelled=error.code is RuntimeErrorCode.CANCELLED,
            unknown_facts=not bool(fingerprint),
            budget_remaining=max(0, self.budget.max_model_repairs - checkpoint.repair_attempts),
            local_diagnose_used=has_local_diagnose_decision(checkpoint.metadata, incident_fingerprint),
            safe_alternative_available=bool(getattr(guidance, "suggested_tools", [])),
            safe_local_diagnose_available=safe_contract,
            incident_fingerprint=incident_fingerprint,
        )
        return self.adaptive_reasoning_policy.decide(facts)

    def _record_adaptive_decision(
        self,
        checkpoint: LoopCheckpoint,
        decision: AdaptiveDecision,
    ) -> None:
        """持久化去重记录并发布不含敏感正文的 Runtime 事实。"""

        if not record_decision(checkpoint.metadata, decision):
            return
        self.state_store.save(checkpoint)
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            {
                "stage": "adaptive_reasoning",
                "status": "decision_selected",
                "action": decision.action,
                "reason_code": decision.reason_code,
                "fact_fingerprint": decision.fact_fingerprint,
                "max_tool_calls": decision.max_tool_calls,
                "max_model_calls": decision.max_model_calls,
                "allowed_capabilities": list(decision.allowed_capabilities),
                "evidence_refs": list(decision.evidence_refs),
            },
            visibility=EventVisibility.DEBUG,
            event_id=f"adaptive.reasoning:{checkpoint.run_id}:{decision.fact_fingerprint}",
            trace_id=_trace_id(checkpoint),
        )

    def _blocked_by_recovery_guard(self, checkpoint: LoopCheckpoint, call: ToolCall, guidance) -> ToolResult:
        """Close a provider tool-call pair without allowing a blocked Host call."""

        blocked_reason = self.recovery_manager.blocked_reason_for_call(
            checkpoint.recovery_state,
            call,
        )
        crawler_messages = {
            "crawler.policy.robots_denied": "目标页面明确禁止自动抓取，Runtime 已停止重复访问。",
            "crawler.source.timeout": "目标网页响应过慢，Runtime 已停止重复读取该来源。",
            "crawler.source.network_error": "目标网页网络请求失败，Runtime 已停止重复读取该来源。",
            "crawler.source.http_error": "目标网页未成功响应，Runtime 已停止重复读取该来源。",
        }
        provider_messages = {
            "provider.tool_arguments_invalid": "模型返回的工具参数不完整或格式错误，Runtime 已停止重复调用。",
        }
        classified_messages = {**crawler_messages, **provider_messages}
        if blocked_reason in classified_messages:
            error = RuntimeError(
                code=RuntimeErrorCode.TOOL_FAILED,
                message=classified_messages[blocked_reason],
                retryable=False,
                category="model_repairable" if guidance.stage != "terminate" else "fatal",
                details={
                    "reason_code": blocked_reason,
                    "blocked_by_recovery": True,
                    "stage": guidance.stage,
                },
            )
            # Keep the original crawler reason and alternative-source advice;
            # only the guard stage/action is updated for this repeated call.
            recovery = self.recovery_manager.failure_guidance(
                call,
                error,
                category=error.category,
                available_tools=self._available_tool_names(checkpoint),
            ).model_copy(
                update={
                    "stage": guidance.stage,
                    "action": guidance.action,
                    "occurrence_count": guidance.occurrence_count,
                }
            )
        else:
            error = RuntimeError(
                code=RuntimeErrorCode.TOOL_FAILED,
                message="tool call blocked by runtime loop recovery policy",
                retryable=False,
                category="fatal" if guidance.stage == "terminate" else "model_repairable",
                details={"reason_code": guidance.reason_code, "stage": guidance.stage},
            )
            recovery = guidance
        result = ToolResult(
            tool_call_id=call.tool_call_id,
            run_id=call.run_id,
            status=ToolCallStatus.FAILED,
            error=error,
            recovery=recovery,
            metadata={"recovery_guard": True},
        )
        self.context_builder.append_tool_result(checkpoint, result)
        self._append_recovery_reminder(checkpoint, guidance)
        self.state_store.save(checkpoint)
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.TOOL_RESULT_CREATED,
            tool_result_payload(result),
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
            redact=True,
        )
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            {
                "stage": "loop_recovery",
                "status": "host_call_blocked",
                "reason_code": guidance.reason_code,
                "action": guidance.action,
                "recovery_stage": guidance.stage,
                "tool_name": call.tool_name,
            },
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
        )
        return result

    @staticmethod
    def _recovery_terminal_error(
        checkpoint: LoopCheckpoint,
        blocked_error: RuntimeError | None,
    ) -> RuntimeError:
        """Keep an actionable stable fact when the final Host call was blocked."""

        if checkpoint.recovery_state.get("last_failure_reason_code") == "tool.resource_not_found":
            return RuntimeError(
                code=RuntimeErrorCode.INVALID_REQUEST,
                message="local file path does not exist",
                category="model_repairable",
            )
        return blocked_error or RuntimeError(
            code=RuntimeErrorCode.TOOL_FAILED,
            message="tool call blocked by recovery guard",
        )

    def _finish_for_budget(
        self,
        checkpoint: LoopCheckpoint,
        exc: LoopBudgetExceeded,
    ) -> LoopResult:
        artifact_verdict = evaluate_artifact_terminal(
            checkpoint.metadata.get("artifact_intent"),
            self._run_artifacts(checkpoint),
            repair_attempts=1,
        )
        if self._can_complete_after_artifact_finalization(checkpoint, artifact_verdict):
            return self._finish(checkpoint, RunStatus.COMPLETED)
        return self._finish(checkpoint, RunStatus.BLOCKED, error=_budget_error(exc))

    def _can_complete_after_artifact_finalization(
        self,
        checkpoint: LoopCheckpoint,
        artifact_verdict,
    ) -> bool:
        """Keep generic artifact fallbacks behind the local project contract.

        Artifact delivery protects generated reports from a later optional
        failure. It must not turn partial project source files into a false
        running-application success when a local_dev lifecycle is active.
        """

        return (
            # A source-file write in a coding Run may also be represented as an
            # ArtifactRef for audit/delivery history.  That audit record is not
            # an independently requested deliverable: allowing it to satisfy
            # this fallback would turn a failed post-edit verification into a
            # false completed Run.  Only an explicit artifact contract may use
            # the "deliverable survives auxiliary failure" path.
            artifact_verdict.required
            and
            artifact_verdict.satisfied
            and self._has_deliverable_artifact(checkpoint)
            and not local_dev_project_lifecycle_missing(checkpoint.metadata)
        )

    def _finish_after_artifact_finalization_failure(
        self,
        checkpoint: LoopCheckpoint,
        *,
        completion_status: str,
        notice: str,
        exception_type: str,
        retryable: bool,
    ) -> Optional[LoopResult]:
        """已满足交付契约后的辅助失败不能覆盖可用结果。"""

        artifact_verdict = evaluate_artifact_terminal(
            checkpoint.metadata.get("artifact_intent"),
            self._run_artifacts(checkpoint),
            repair_attempts=1,
        )
        if not self._can_complete_after_artifact_finalization(checkpoint, artifact_verdict):
            return None

        self.context_builder.append_assistant(checkpoint, notice)
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            {
                "stage": "artifact_verdict",
                "status": completion_status,
                "reason": artifact_verdict.reason,
                "matching_artifact_ids": list(artifact_verdict.matching_artifact_ids),
                "exception_type": exception_type,
                "retryable": retryable,
            },
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
        )
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.TURN_ASSISTANT_TEXT,
            {
                "content": notice,
                "metadata": {
                    "source": "runtime",
                    "reason": completion_status,
                },
            },
            trace_id=_trace_id(checkpoint),
        )
        return self._finish(checkpoint, RunStatus.COMPLETED)

    def _finish_after_crawler_summary_failure(
        self,
        checkpoint: LoopCheckpoint,
        *,
        completion_status: str,
        exception_type: str,
        retryable: bool,
    ) -> Optional[LoopResult]:
        """Retain verified partial crawler evidence when only its prose summary fails.

        This applies after a crawler result has requested the single bounded
        no-tool summary turn. It must never claim that the continuation itself
        completed: the deterministic response keeps the actual coverage and
        explicitly states that the model summary was unavailable.
        """

        partial_answer = checkpoint.metadata.get("crawler_partial_answer")
        if (
            checkpoint.metadata.get("crawler_partial_answer_replan") is not True
            or not isinstance(partial_answer, dict)
        ):
            return None
        summary = {
            "summary_text": str(partial_answer.get("summary_text") or ""),
            "coverage_label": str(partial_answer.get("coverage_label") or ""),
            "observed_count": str(partial_answer.get("observed_count") or "0"),
            "expected_count": str(partial_answer.get("expected_count") or "0"),
        }
        content = (
            f"{_crawler_partial_terminal_text(summary)}"
            "平台模型服务暂时不可用，未能基于这些记录生成评论总结。"
        )
        checkpoint.metadata["completion_warning"] = True
        self.context_builder.append_assistant(checkpoint, content)
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            {
                "stage": "crawler_partial_answer",
                "status": completion_status,
                "exception_type": exception_type,
                "retryable": retryable,
            },
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
        )
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.TURN_ASSISTANT_TEXT,
            {
                "content": content,
                "metadata": {"source": "runtime", "reason": completion_status},
            },
            trace_id=_trace_id(checkpoint),
        )
        return self._finish(checkpoint, RunStatus.COMPLETED)

    def _finish_after_crawler_evidence_model_unavailable(
        self,
        checkpoint: LoopCheckpoint,
        *,
        completion_status: str,
        exception_type: str,
        retryable: bool,
    ) -> Optional[LoopResult]:
        """Deliver verified crawler records when a later planning call gets 503.

        A crawler may have produced useful partial records even when its
        structured coverage has no reliable expected count. Do not discard
        those records or turn them into a generic model failure; report the
        observed count and explicitly leave completeness unknown.
        """

        raw = checkpoint.metadata.get("crawler_dataset")
        if not isinstance(raw, dict):
            return None
        # A session-owned dataset is injected into every follow-up context.
        # Only evidence committed by this Run may use the crawler-specific
        # fallback; otherwise a normal analysis 429 is mislabeled as a crawl
        # continuation failure.
        source_run_id = str(raw.get("source_run_id") or "")
        if source_run_id and source_run_id != checkpoint.run_id:
            return None
        if str(raw.get("profile") or "").lower() != "reviews":
            return None
        observed = int(raw.get("record_count") or 0)
        if observed <= 0:
            return None
        coverage = raw.get("coverage") if isinstance(raw.get("coverage"), dict) else {}
        expected = coverage.get("expected_count")
        if isinstance(expected, int) and expected > 0:
            coverage_label = str(coverage.get("label") or f"{observed}/{expected}")
            completeness = f"评论明细覆盖率：{coverage_label}。"
        else:
            completeness = "页面评论总量未能在本次运行中可靠确认，无法声称已获取全部评论。"
        content = (
            f"已保留 {observed} 条可验证评论记录。{completeness}"
            "平台模型服务暂时不可用，未能继续完成评论分析；已获取的记录未丢失，可稍后基于本次结果重试。"
        )
        checkpoint.metadata["completion_warning"] = True
        self.context_builder.append_assistant(checkpoint, content)
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            {
                "stage": "crawler_evidence",
                "status": completion_status,
                "record_count": observed,
                "exception_type": exception_type,
                "retryable": retryable,
            },
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
        )
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.TURN_ASSISTANT_TEXT,
            {"content": content, "metadata": {"source": "runtime", "reason": completion_status}},
            trace_id=_trace_id(checkpoint),
        )
        return self._finish(checkpoint, RunStatus.COMPLETED)

    def _has_deliverable_artifact(self, checkpoint: LoopCheckpoint) -> bool:
        """判断当前 Run 是否拥有具体 ArtifactRef。

        终态策略与 Artifact verdict 必须使用同一事实源。Loop 内以 checkpoint
        为权威；EventStore 回退只兼容旧版本写入的 checkpoint。
        """

        return bool(self._run_artifacts(checkpoint))

    def _arm_artifact_completion(self, checkpoint: LoopCheckpoint) -> None:
        """产物满足契约后进入有界的仅响应阶段。"""

        if checkpoint.metadata.get("artifact_completion_armed") is True:
            return
        verdict = evaluate_artifact_terminal(
            checkpoint.metadata.get("artifact_intent"),
            self._run_artifacts(checkpoint),
        )
        if not verdict.required or not verdict.satisfied:
            return
        checkpoint.metadata["artifact_completion_armed"] = True
        checkpoint.messages.append(
            {
                "role": "system",
                "content": artifact_completion_instruction(verdict),
                "metadata": {"_runtime_artifact_completion": True},
            }
        )
        self.state_store.save(checkpoint)
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            {
                "stage": "artifact_verdict",
                "status": "ready_for_response",
                "reason": verdict.reason,
                "present_formats": [item.value for item in verdict.present_formats],
                "matching_artifact_ids": list(verdict.matching_artifact_ids),
            },
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
        )

    def _should_suppress_redundant_artifact_write(
        self,
        checkpoint: LoopCheckpoint,
        intents: List[ToolCallIntent],
    ) -> bool:
        if checkpoint.metadata.get("artifact_completion_armed") is not True:
            return False
        verdict = evaluate_artifact_terminal(
            checkpoint.metadata.get("artifact_intent"),
            self._run_artifacts(checkpoint),
            repair_attempts=1,
        )
        return verdict.satisfied and any(_declares_artifact_output(intent) for intent in intents)

    def _should_suppress_unrequested_artifact_write(
        self,
        checkpoint: LoopCheckpoint,
        intents: List[ToolCallIntent],
    ) -> bool:
        """执行 require_artifact=false，不能只依赖模型遵守 Prompt。"""

        verdict = evaluate_artifact_terminal(
            checkpoint.metadata.get("artifact_intent"),
            self._run_artifacts(checkpoint),
        )
        if verdict.required:
            return False
        artifact_writes = [intent for intent in intents if _declares_artifact_output(intent)]
        if not artifact_writes:
            return False
        # Do not turn project implementation into an artifact-export
        # policy bypass: Runtime-owned Code Runs may create only source
        # and minimal project-config files through their existing controlled
        # local tools. Workspace grants, approval, and the Node declared-output
        # contract still execute after this policy gate.
        return not all(
            _is_runtime_owned_local_dev_project_write(checkpoint, intent)
            for intent in artifact_writes
        )

    @staticmethod
    def _requires_local_dev_project_action(checkpoint: LoopCheckpoint) -> bool:
        """Return whether a Code surface still lacks a real tool receipt."""

        return (
            is_code_execution(checkpoint.metadata)
            and checkpoint.metadata.get(LOCAL_DEV_PROJECT_ACTION_SUCCEEDED_METADATA_KEY) is not True
        )

    def _record_local_dev_project_action_success(
        self,
        checkpoint: LoopCheckpoint,
        call: ToolCall,
        result: ToolResult,
    ) -> None:
        """Persist successful host execution before allowing a prose completion.

        Only results returned from ``ToolTurnRunner`` reach this method. That
        excludes Runtime-suppressed reads and model-declared calls, so the flag
        is evidence of a completed local tool action rather than merely a tool
        name appearing in model output.
        """

        if result.status is not ToolCallStatus.SUCCEEDED:
            return
        self._record_local_dev_project_lifecycle_evidence(checkpoint, call, result)
        if (
            not self._requires_local_dev_project_action(checkpoint)
            or not is_local_dev_project_action(call.tool_name, call.arguments)
        ):
            return
        checkpoint.metadata[LOCAL_DEV_PROJECT_ACTION_SUCCEEDED_METADATA_KEY] = True
        checkpoint.metadata["local_dev_project_action_tool"] = call.tool_name
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            {
                "stage": "local_dev_project_execution",
                "status": "project_action_succeeded",
                "tool_name": call.tool_name,
            },
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
        )

    def _record_local_dev_project_lifecycle_evidence(
        self,
        checkpoint: LoopCheckpoint,
        call: ToolCall,
        result: ToolResult,
    ) -> None:
        """Persist only successful Electron-backed project lifecycle facts.

        The result is deliberately interpreted as a narrow receipt: neither
        model prose nor a requested argv can satisfy build/start acceptance.
        Electron Main has already enforced the workspace grant, approval and
        command boundary before a result reaches this method.
        """

        evidence = checkpoint.metadata.get(LOCAL_DEV_PROJECT_LIFECYCLE_EVIDENCE_METADATA_KEY)
        facts = dict(evidence) if isinstance(evidence, Mapping) else {}
        already_complete = not local_dev_project_lifecycle_missing(checkpoint.metadata)
        if already_complete:
            # 首次齐全后允许一次额外的成功项目动作（典型是启动第二个
            # 前端/后端服务），随后由 Loop 的收官门直接结束本地项目 Run。
            if is_local_dev_project_action(call.tool_name, call.arguments):
                facts["post_completion_project_actions"] = int(
                    facts.get("post_completion_project_actions") or 0
                ) + 1
                checkpoint.metadata[LOCAL_DEV_PROJECT_LIFECYCLE_EVIDENCE_METADATA_KEY] = facts
                self.state_store.save(checkpoint)
            return
        updated: list[str] = []
        if _local_dev_project_source_evidence(call) and facts.get("project_source") is not True:
            facts["project_source"] = True
            updated.append("project_source")
        if _local_dev_dependency_install_evidence(call) and facts.get("dependencies_installed") is not True:
            facts["dependencies_installed"] = True
            updated.append("dependencies_installed")
        if _local_dev_build_evidence(call) and facts.get("build_succeeded") is not True:
            facts["build_succeeded"] = True
            updated.append("build_succeeded")
        if _local_dev_verified_server_evidence(call, result) and facts.get("dev_server_verified") is not True:
            facts["dev_server_verified"] = True
            updated.append("dev_server_verified")
        if _local_dev_verified_server_evidence(call, result):
            service_keys = facts.get("verified_service_keys")
            keys = list(service_keys) if isinstance(service_keys, list) else []
            service_key = _local_dev_service_key(call)
            if service_key not in keys:
                keys.append(service_key)
                facts["verified_service_keys"] = keys
                updated.append("verified_service_keys")
        # ISSUE-175：安装尝试（无论成败）说明项目声明了外部依赖，必须保留
        # install 成功回执的要求；该事实同时是无依赖豁免的排除条件。
        if (
            _local_dev_dependency_install_attempt(call)
            and facts.get("dependency_install_attempted") is not True
        ):
            facts["dependency_install_attempted"] = True
            updated.append("dependency_install_attempted")
        if not updated:
            return
        checkpoint.metadata[LOCAL_DEV_PROJECT_LIFECYCLE_EVIDENCE_METADATA_KEY] = facts
        # The Host result that changes the required evidence set from
        # incomplete to complete is itself sufficient for a single-service
        # project. Persist this transition so the loop can finalize directly
        # without spending another model turn on a redundant summary/action.
        if not local_dev_project_lifecycle_missing(checkpoint.metadata):
            facts["completed_on_current_action"] = True
        self.state_store.save(checkpoint)
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            {
                "stage": "local_dev_project_lifecycle",
                "status": "host_evidence_recorded",
                "updated": updated,
                "missing": list(local_dev_project_lifecycle_missing(checkpoint.metadata)),
            },
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
        )

    @staticmethod
    def _should_finalize_local_dev_project(checkpoint: LoopCheckpoint) -> bool:
        facts = checkpoint.metadata.get(LOCAL_DEV_PROJECT_LIFECYCLE_EVIDENCE_METADATA_KEY)
        return (
            local_dev_project_lifecycle_required(checkpoint.metadata)
            and not local_dev_project_lifecycle_missing(checkpoint.metadata)
            and isinstance(facts, Mapping)
            and (
                facts.get("completed_on_current_action") is True
                or int(facts.get("post_completion_project_actions") or 0) >= 1
            )
        )

    def _run_artifacts(self, checkpoint: LoopCheckpoint) -> List[ArtifactRef]:
        # Loop 内以 checkpoint 为权威；事件只用于恢复缺少该字段的旧 checkpoint，
        # 或兼容旧 Runtime 版本已经发出的产物。
        artifacts_by_id = {artifact.artifact_id: artifact for artifact in checkpoint.artifact_refs}
        for event in self.event_store.list_events(checkpoint.run_id):
            if event.type not in {
                RuntimeEventType.ARTIFACT_CREATED.value,
                RuntimeEventType.ARTIFACT_CREATED,
            }:
                continue
            try:
                artifact = ArtifactRef.model_validate(event.payload)
                artifacts_by_id.setdefault(artifact.artifact_id, artifact)
            except (TypeError, ValueError):
                continue
        # Multiple intermediate writes to the same local path are versions of
        # one deliverable, not separate user outputs. Keep the latest durable
        # receipt for that URI while preserving unrelated artifacts.
        latest_by_uri: dict[str, ArtifactRef] = {}
        result: list[ArtifactRef] = []
        for artifact in artifacts_by_id.values():
            uri = artifact.access.uri if artifact.access.kind.value == "local_file" else None
            if not isinstance(uri, str) or not uri:
                result.append(artifact)
                continue
            previous = latest_by_uri.get(uri)
            if previous is not None:
                result = [item for item in result if item.artifact_id != previous.artifact_id]
            latest_by_uri[uri] = artifact
            result.append(artifact)
        return result

    def _reject_pending_approval(
        self,
        checkpoint: LoopCheckpoint,
        resume: LoopResume,
        *,
        status: ApprovalStatus,
        error_code: RuntimeErrorCode,
        default_message: str,
    ) -> RuntimeError:
        pending_call = checkpoint.pending_tool_call
        pending_approval = checkpoint.pending_approval
        error = RuntimeError(
            code=error_code,
            message=resume.reason or default_message,
        )
        if pending_approval is not None:
            pending_approval.status = status
            self.trace_emitter.emit(
                checkpoint.run_id,
                RuntimeEventType.APPROVAL_RESOLVED,
                {
                    "approval_id": pending_approval.approval_id,
                    "status": status.value,
                    "reason": resume.reason,
                },
                trace_id=_trace_id(checkpoint),
            )
        if pending_call is not None:
            result = ToolResult(
                tool_call_id=pending_call.tool_call_id,
                run_id=checkpoint.run_id,
                status=ToolCallStatus.FAILED,
                error=error,
                metadata={"approval_status": status.value},
            )
            self.context_builder.append_tool_result(checkpoint, result)
            self.trace_emitter.emit(
                checkpoint.run_id,
                RuntimeEventType.TOOL_RESULT_CREATED,
                tool_result_payload(result),
                visibility=EventVisibility.DEBUG,
                trace_id=_trace_id(checkpoint),
                redact=True,
            )
        checkpoint.pending_tool_call = None
        checkpoint.pending_tool_intents.clear()
        checkpoint.pending_approval = None
        checkpoint.metadata.pop("execution_gate", None)
        self.state_store.save(checkpoint)
        return error

    def _finish(
        self,
        checkpoint: LoopCheckpoint,
        status: RunStatus,
        *,
        error: Optional[RuntimeError] = None,
        tool_results: Optional[List[ToolResult]] = None,
    ) -> LoopResult:
        _LOOP_LOGGER.info(
            "run.finishing run_id=%s status=%s model_turns=%s model_tokens=%s max_model_tokens=%s tool_calls=%s final_content_chars=%s error=%s",
            checkpoint.run_id,
            status.value,
            checkpoint.model_turns,
            checkpoint.model_tokens,
            self.budget.max_model_tokens,
            checkpoint.tool_calls,
            len(checkpoint.final_content or ""),
            error.code if error is not None else None,
        )
        with self._terminal_lock:
            persisted = self.state_store.get(checkpoint.checkpoint_key or checkpoint.run_id)
            if persisted is not None and persisted.status in TERMINAL_LOOP_STATUSES:
                existing_error = error if persisted.status == status else None
                return self._result(
                    persisted,
                    error=existing_error,
                    tool_results=tool_results,
                )

            lifecycle_phase = {
                RunStatus.COMPLETED: TurnPhase.COMPLETED,
                RunStatus.FAILED: TurnPhase.FAILED,
                RunStatus.BLOCKED: TurnPhase.FAILED,
                RunStatus.CANCELLED: TurnPhase.CANCELLED,
                RunStatus.WAITING_APPROVAL: TurnPhase.WAITING_APPROVAL,
                RunStatus.WAITING_INPUT: TurnPhase.WAITING_INPUT,
            }.get(status)
            if lifecycle_phase is not None:
                self._transition_turn(
                    checkpoint,
                    lifecycle_phase,
                    tool_call_id=(checkpoint.pending_tool_call.tool_call_id if checkpoint.pending_tool_call else None),
                    status=status.value,
                    payload={"error_code": error.code.value if error is not None else None},
                )

            review_terminal_content = _crawler_review_terminal_content(checkpoint)
            if review_terminal_content is not None and review_terminal_content != checkpoint.final_content:
                checkpoint.final_content = review_terminal_content

            delivery_summary = self._append_artifact_delivery_summary(checkpoint, status)
            checkpoint.status = status
            delivery_outcome = self._delivery_outcome(checkpoint, status, error, freeze=True)
            delivery_fallback = self._append_delivery_fallback(checkpoint, delivery_outcome, error)
            terminal_text = "" if delivery_fallback else self._missing_terminal_assistant_text(checkpoint)
            self.state_store.save(checkpoint)
            event_type = {
                RunStatus.COMPLETED: RuntimeEventType.RUN_COMPLETED,
                RunStatus.FAILED: RuntimeEventType.RUN_FAILED,
                RunStatus.CANCELLED: RuntimeEventType.RUN_CANCELLED,
                RunStatus.BLOCKED: RuntimeEventType.RUN_BLOCKED,
            }.get(status, RuntimeEventType.STATE_SYNC)
            payload = {
                "status": status.value,
                "content": checkpoint.final_content,
                "model_call_count": checkpoint.model_turns,
                "model_tokens_used": checkpoint.model_tokens,
                "max_model_tokens": self.budget.max_model_tokens,
                "token_usage_status": (
                    "reported"
                    if checkpoint.metadata.get("model_usage_reported") is True
                    else "unavailable" if checkpoint.model_turns else "not_applicable"
                ),
                "delivery_outcome": delivery_outcome.model_dump(mode="json"),
            }
            if error is not None:
                payload["error"] = error.model_dump(mode="json")
            if checkpoint.metadata.get("delegation_receipt_terminal") is not True:
                self._emit_verdict(checkpoint, status, error)
            if delivery_summary:
                self.trace_emitter.emit(
                    checkpoint.run_id,
                    RuntimeEventType.TURN_ASSISTANT_TEXT,
                    {
                        "content": delivery_summary,
                        "metadata": {
                            "source": "runtime",
                            "reason": "artifact_delivery",
                        },
                    },
                    trace_id=_trace_id(checkpoint),
                )
            if delivery_fallback:
                self.trace_emitter.emit(
                    checkpoint.run_id,
                    RuntimeEventType.TURN_ASSISTANT_TEXT,
                    {
                        "content": delivery_fallback,
                        "metadata": {
                            "source": "runtime",
                            "reason": "delivery_outcome_fallback",
                        },
                    },
                    trace_id=_trace_id(checkpoint),
                )
            if terminal_text:
                # A lifecycle row only updates status in the UI. Deterministic
                # tool completion paths must also leave replayable answer text.
                self.trace_emitter.emit(
                    checkpoint.run_id,
                    RuntimeEventType.TURN_ASSISTANT_TEXT,
                    {
                        "content": terminal_text,
                        "metadata": {
                            "source": "runtime",
                            "reason": "terminal_content_backfill",
                        },
                    },
                    trace_id=_trace_id(checkpoint),
                )
            if _owns_run_lifecycle(checkpoint):
                self.trace_emitter.emit(
                    checkpoint.run_id,
                    event_type,
                    payload,
                    trace_id=_trace_id(checkpoint),
                )
            return self._result(checkpoint, error=error, tool_results=tool_results)

    def _append_artifact_delivery_summary(
        self,
        checkpoint: LoopCheckpoint,
        status: RunStatus,
    ) -> str:
        if status is not RunStatus.COMPLETED:
            return ""
        summary = build_artifact_delivery_summary(self._run_artifacts(checkpoint))
        if not summary:
            return ""
        final_content = append_artifact_delivery_summary(checkpoint.final_content, summary)
        if final_content == checkpoint.final_content:
            return ""
        checkpoint.final_content = final_content
        return summary

    def _record_completed_clarification(
        self,
        checkpoint: LoopCheckpoint,
        *,
        stage: str,
        reason: str,
    ) -> None:
        """Mark a structured clarification as a completed conversational turn.

        This marker deliberately does not clear ``artifact_intent``. A later
        message in the same Session must still inherit an unfinished requested
        deliverable, while the current Run remains a normal completed response.
        """

        checkpoint.metadata.pop("waiting_input", None)
        checkpoint.metadata["clarification_completed"] = {
            "reason": reason,
            "stage": stage,
        }
        # Typed clarification identity belongs to the Runtime-owned snapshot.
        # Standalone Loop tests and legacy internal callers may not have one;
        # they still get the human-visible completion marker, but must never
        # manufacture task/turn IDs from run or session identity.
        raw_snapshot = checkpoint.metadata.get("context_snapshot")
        snapshot = raw_snapshot if isinstance(raw_snapshot, dict) else None
        turn_id = snapshot.get("turn_id") if snapshot else None
        task_id = snapshot.get("task_id") if snapshot else None
        outcome = None
        if isinstance(turn_id, str) and turn_id.strip() and isinstance(task_id, str) and task_id.strip():
            outcome = _clarification_outcome(
                turn_id=turn_id,
                task_id=task_id,
                reason=reason,
            )
            checkpoint.metadata["clarification_outcome"] = outcome
        else:
            checkpoint.metadata.pop("clarification_outcome", None)
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.PIPELINE_STAGE,
            {
                "stage": stage,
                "status": "clarification_completed",
                "reason": reason,
                **({"clarification_outcome": outcome} if outcome is not None else {}),
            },
            visibility=EventVisibility.DEBUG,
            trace_id=_trace_id(checkpoint),
        )

    def _append_delivery_fallback(
        self,
        checkpoint: LoopCheckpoint,
        outcome: DeliveryOutcome,
        error: RuntimeError | None,
    ) -> str:
        """Keep legacy consumers nonblank without replacing a verified response."""

        if _defer_user_response(checkpoint):
            return ""
        if checkpoint.final_content.strip():
            return ""
        details = error.details if error is not None and isinstance(error.details, dict) else {}
        timeout_message = (
            f"模型供应商{model_failure_subject(details.get('provider'), details.get('model'))}"
            "响应超时，请稍后重试；如持续发生，请切换模型或检查供应商状态。"
            if details.get("reason_code") == "model.provider_timeout"
            else None
        )
        content = timeout_message or outcome.summary
        checkpoint.final_content = content
        self.context_builder.append_assistant(checkpoint, content)
        return content

    def _missing_terminal_assistant_text(self, checkpoint: LoopCheckpoint) -> str:
        """Backfill a visible answer when a terminal path only set final_content."""

        if _defer_user_response(checkpoint):
            return ""
        content = checkpoint.final_content.strip()
        if not content:
            return ""
        has_terminal_text = any(
            event.type is RuntimeEventType.TURN_ASSISTANT_TEXT
            and str(event.payload.get("content") or "").strip() == content
            for event in self.event_store.list_events(checkpoint.run_id, after_seq=0, limit=1000)
        )
        return "" if has_terminal_text else content

    def _append_terminal_tool_failure_explanation(
        self,
        checkpoint: LoopCheckpoint,
        *,
        call: ToolCall,
        runtime_error: RuntimeError,
    ) -> None:
        """在模型无视收官指令时写入一次可信、可见的确定性终态说明。"""

        if checkpoint.metadata.get("tool_failure_terminal_explained") is True:
            return
        checkpoint.metadata["tool_failure_terminal_explained"] = True
        content = terminal_tool_failure_explanation(call, runtime_error)
        # 这条由 Runtime 生成的收官说明就是本 Run 的交付内容。仅追加到
        # history 会在 _finish 时被通用 delivery fallback 覆盖，重新把已知
        # 的 Crawler 失败伪装成“等待补充信息”。
        if not checkpoint.final_content.strip():
            checkpoint.final_content = content
        self.context_builder.append_assistant(checkpoint, content)
        self.trace_emitter.emit(
            checkpoint.run_id,
            RuntimeEventType.TURN_ASSISTANT_TEXT,
            {
                "content": content,
                "metadata": {
                    "source": "runtime",
                    "reason": "repeated_tool_failure",
                    "tool_name": call.tool_name,
                },
            },
            trace_id=_trace_id(checkpoint),
        )
        self.state_store.save(checkpoint)

    def _emit_decision(
        self,
        checkpoint: LoopCheckpoint,
        *,
        choice: str,
        alternatives: list[str],
        reason_code: str,
        evidence_events: list,
        confidence: float | None,
        action_ref: str | None = None,
        attributes: dict | None = None,
    ) -> None:
        evidence = [
            CognitiveEvidenceRef(
                kind="event",
                ref_id=event.event_id,
                run_id=checkpoint.run_id,
                relation="supports",
            )
            for event in evidence_events
            if event is not None
        ]
        self.trace_emitter.emit_cognitive(
            CognitiveRecord(
                record_type="decision",
                record_id=new_contract_id("decision"),
                record_key=choice,
                run_id=checkpoint.run_id,
                trace_id=_trace_id(checkpoint) or f"trace:{checkpoint.run_id}",
                choice=choice,
                alternatives=alternatives,
                reason_code=reason_code,
                confidence=confidence,
                action_ref=action_ref,
                evidence_refs=evidence,
                unsupported=not bool(evidence),
                attributes=attributes or {},
            )
        )

    def _emit_verdict(
        self,
        checkpoint: LoopCheckpoint,
        status: RunStatus,
        error: Optional[RuntimeError],
    ) -> None:
        evidence_events = [
            event
            for event in self.event_store.list_events(checkpoint.run_id, after_seq=0, limit=1000)
            if event.type in {
                RuntimeEventType.TOOL_RESULT_CREATED,
                RuntimeEventType.ARTIFACT_CREATED,
                RuntimeEventType.ERROR_RAISED,
                RuntimeEventType.RUN_HEARTBEAT,
            }
        ][-3:]
        evidence = [
            CognitiveEvidenceRef(
                kind="event",
                ref_id=event.event_id,
                run_id=checkpoint.run_id,
                relation="verifies" if event.type is RuntimeEventType.ARTIFACT_CREATED else "supports",
            )
            for event in evidence_events
        ]
        self.trace_emitter.emit_cognitive(
            CognitiveRecord(
                record_type="verdict",
                record_id=f"verdict:{checkpoint.run_id}:{status.value}",
                record_key=status.value,
                run_id=checkpoint.run_id,
                trace_id=_trace_id(checkpoint) or f"trace:{checkpoint.run_id}",
                reason_code="runtime_terminal",
                confidence=1.0,
                evidence_refs=evidence,
                unsupported=not bool(evidence),
                criteria_results={
                    "terminal_status": status.value,
                    "error_free": error is None,
                    "artifact_present": self._has_deliverable_artifact(checkpoint),
                },
                verifier_version="runtime-deterministic.v1",
                attributes={"error_code": error.code.value if error is not None else None},
            )
        )

    def _result(
        self,
        checkpoint: LoopCheckpoint,
        *,
        pending_approval=None,
        error: Optional[RuntimeError] = None,
        tool_results: Optional[List[ToolResult]] = None,
        persist_delivery: bool = False,
    ) -> LoopResult:
        delivery_outcome = self._delivery_outcome(
            checkpoint,
            checkpoint.status,
            error,
            freeze=persist_delivery or checkpoint.status in TERMINAL_LOOP_STATUSES,
        )
        if persist_delivery and checkpoint.status not in TERMINAL_LOOP_STATUSES:
            self.state_store.save(checkpoint)
        return LoopResult(
            run_id=checkpoint.run_id,
            status=checkpoint.status,
            content=checkpoint.final_content,
            pending_approval=pending_approval,
            error=error,
            events=self.event_store.list_events(checkpoint.run_id),
            tool_results=tool_results or [],
            delivery_outcome=delivery_outcome,
        )

    def _delivery_outcome(
        self,
        checkpoint: LoopCheckpoint,
        status: RunStatus,
        error: Optional[RuntimeError],
        *,
        freeze: bool,
    ) -> DeliveryOutcome:
        """Build a safe conclusion and freeze it only after a terminal transition."""

        existing = checkpoint.delivery_outcome
        if freeze and existing is not None:
            return existing
        artifact_intent = checkpoint.metadata.get("artifact_intent")
        # 澄清回复已经完成本轮对话，但不会满足或丢弃 Session 中的后续
        # Artifact 约束。交付 outcome 只描述当前 Run，不能因此显示为
        # “文件未生成”的失败/部分完成。
        clarification_completed = isinstance(
            checkpoint.metadata.get("clarification_completed"), dict
        )
        artifact_required = (
            not clarification_completed
            and isinstance(artifact_intent, dict)
            and artifact_intent.get("require_artifact") is True
        )
        artifact_satisfied = False
        if artifact_required:
            artifact_satisfied = evaluate_artifact_terminal(
                artifact_intent,
                self._run_artifacts(checkpoint),
                repair_attempts=int(checkpoint.metadata.get("artifact_repair_attempts") or 0),
            ).satisfied
        waiting_for = None
        if checkpoint.pending_approval is not None or status is RunStatus.WAITING_APPROVAL:
            waiting_for = "approval"
        elif checkpoint.metadata.get("waiting_input") or checkpoint.metadata.get("crawler_pending_interaction"):
            waiting_for = "input"
        reason_code = _delivery_reason_code(checkpoint, error)
        facts = DeliveryFacts(
            run_id=checkpoint.run_id,
            run_status=status.value,
            has_user_content=bool(checkpoint.final_content.strip()),
            artifact_required=artifact_required,
            artifact_satisfied=artifact_satisfied,
            artifact_count=len(self._run_artifacts(checkpoint)),
            completion_warning=status is RunStatus.COMPLETED and bool(
                error is not None or checkpoint.metadata.get("completion_warning") is True
            ),
            waiting_for=waiting_for,
            failure_retryable=bool(error and error.retryable),
            reason_code=reason_code,
            evidence_refs=_delivery_evidence_refs(checkpoint, self._run_artifacts(checkpoint), self.event_store),
        )
        outcome = map_delivery_outcome(facts)
        if freeze:
            checkpoint.delivery_outcome = outcome
            checkpoint.metadata["delivery_outcome"] = outcome.model_dump(mode="json")
        return outcome

    @staticmethod
    def _clear_nonterminal_delivery_outcome(checkpoint: LoopCheckpoint) -> None:
        """A resumed Run must not keep displaying a stale waiting conclusion."""

        checkpoint.delivery_outcome = None
        checkpoint.metadata.pop("delivery_outcome", None)

    def _require_checkpoint(self, run_id: str) -> LoopCheckpoint:
        checkpoint = self.state_store.get(run_id)
        if checkpoint is None:
            raise ValueError(f"loop checkpoint not found: {run_id}")
        return checkpoint

    def _register_active_token(self, run_id: str, token: CancellationToken) -> None:
        with self._active_tokens_lock:
            existing = self._active_tokens.get(run_id)
            if existing is not None and existing is not token:
                raise ValueError(f"loop already active: {run_id}")
            self._active_tokens[run_id] = token

    def _unregister_active_token(self, run_id: str, token: CancellationToken) -> None:
        with self._active_tokens_lock:
            if self._active_tokens.get(run_id) is token:
                self._active_tokens.pop(run_id, None)


class _ToolIntentOutcome:
    def __init__(
        self,
        *,
        result: Optional[ToolResult] = None,
        followup_results: Optional[List[ToolResult]] = None,
        terminal_result: Optional[LoopResult] = None,
    ) -> None:
        self.result = result
        self.followup_results = followup_results or []
        self.terminal_result = terminal_result


class _ToolExecutionOutcome:
    def __init__(
        self,
        *,
        tool_result: Optional[ToolResult] = None,
        followup_results: Optional[List[ToolResult]] = None,
        terminal_result: Optional[LoopResult] = None,
    ) -> None:
        self.tool_result = tool_result
        self.followup_results = followup_results or []
        self.terminal_result = terminal_result


def _with_tool_results(result: LoopResult, extras: List[ToolResult]) -> LoopResult:
    """Merge batch receipts without dropping terminal delivery metadata."""

    merged = list(result.tool_results)
    seen_ids = {item.tool_call_id for item in merged}
    for item in extras:
        if item.tool_call_id not in seen_ids:
            merged.append(item)
            seen_ids.add(item.tool_call_id)
    return LoopResult(
        run_id=result.run_id,
        status=result.status,
        content=result.content,
        pending_approval=result.pending_approval,
        error=result.error,
        events=result.events,
        tool_results=merged,
        delivery_outcome=result.delivery_outcome,
    )


def _is_unavailable_market_research(error: RuntimeError) -> bool:
    """Return whether a Platform research failure is not fixed by retrying.

    HTTP 401/403/404 represent an unusable session or route for this Runtime;
    the next model turn must choose a different live-source capability. The
    status is carried in structured details and never inferred from response
    body text.
    """

    status = error.details.get("http_status")
    return status in {401, 403, 404}


def _is_ordinary_research_scope_denial(
    checkpoint: LoopCheckpoint,
    call: ToolCall,
    error: RuntimeError | None,
) -> bool:
    """Return true for a stale model fetch blocked by ordinary research scope."""

    if checkpoint.metadata.get("research_search_first") is not True:
        return False
    if call.tool_name not in {"cowork.web.fetch_page", "cowork.web.crawl_site"}:
        return False
    if error is None or error.code is not RuntimeErrorCode.POLICY_DENIED:
        return False
    details = error.details if isinstance(error.details, dict) else {}
    return details.get("reason_code") == "research.search_first_scope_denied"


def _provider_unavailable_reason(error: RuntimeError) -> str | None:
    """Return only stable Provider facts that must disable a tool for this Run."""

    details = error.details if isinstance(error.details, dict) else {}
    reason_code = details.get("reason_code")
    if not isinstance(reason_code, str):
        return None
    if reason_code in {
        "provider.platform.unavailable",
        "provider.platform.session_unavailable",
        "provider.search.unavailable",
        "provider.health.unavailable",
        "provider.tool.unavailable",
    }:
        return reason_code
    return None


def _delivery_reason_code(
    checkpoint: LoopCheckpoint,
    error: RuntimeError | None,
) -> str | None:
    """Choose a stable public reason without lifting error messages into output."""

    if error is not None:
        details = error.details if isinstance(error.details, dict) else {}
        requested = details.get("reason_code")
        if _is_delivery_reason_code(requested):
            return requested
        if details.get("stage") == "model_output":
            return "delivery.model_output_incomplete"
        return {
            RuntimeErrorCode.CANCELLED: "delivery.cancelled",
            RuntimeErrorCode.BUDGET_EXCEEDED: "delivery.recovery_exhausted",
            RuntimeErrorCode.CONTEXT_COMPACTION_FAILED: "delivery.context_compaction_failed",
            RuntimeErrorCode.POLICY_DENIED: "delivery.policy_denied",
            RuntimeErrorCode.APPROVAL_EXPIRED: "delivery.approval_expired",
            RuntimeErrorCode.MODEL_FAILED: "delivery.model_unavailable" if error.retryable else "delivery.model_failed",
            RuntimeErrorCode.TOOL_FAILED: "delivery.tool_failed",
            RuntimeErrorCode.INVALID_REQUEST: "delivery.invalid_request",
        }.get(error.code, "delivery.unrecoverable_failure")
    if checkpoint.pending_approval is not None:
        return "delivery.waiting_approval"
    if checkpoint.metadata.get("waiting_input") or checkpoint.metadata.get("crawler_pending_interaction"):
        return "delivery.waiting_input"
    if isinstance(checkpoint.metadata.get("clarification_completed"), dict):
        return "delivery.clarification_requested"
    return None


def _is_delivery_reason_code(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[a-z][a-z0-9_.-]{0,119}", value))


def _delivery_evidence_refs(
    checkpoint: LoopCheckpoint,
    artifacts: list[ArtifactRef],
    event_store: EventStore,
) -> list[str]:
    """Expose stable IDs only; payloads remain in their original authority."""

    refs = [f"artifact:{artifact.artifact_id}" for artifact in artifacts]
    for event in event_store.list_events(checkpoint.run_id, after_seq=0, limit=1_000)[-3:]:
        refs.append(f"event:{event.event_id}")
    return list(dict.fromkeys(refs))[:32]


def _budget_error(exc: LoopBudgetExceeded) -> RuntimeError:
    return RuntimeError(
        code=RuntimeErrorCode.BUDGET_EXCEEDED,
        message=str(exc),
        retryable=False,
    )


def _is_length_limited_model_output(output) -> bool:
    return str(getattr(output, "stop_reason", "") or "").strip().lower() in _LENGTH_LIMIT_STOP_REASONS


def _output_continuation_instruction() -> str:
    return (
        "The previous assistant response was cut off by the provider output limit. "
        "Continue exactly from where it stopped. Do not repeat earlier text, do not mention "
        "the limit, and finish the answer for the user."
    )


def _is_search_followup_call(call: ToolCall) -> bool:
    metadata = call.metadata if isinstance(call.metadata, dict) else {}
    arguments = call.arguments if isinstance(call.arguments, dict) else {}
    argument_metadata = arguments.get("metadata") if isinstance(arguments.get("metadata"), dict) else {}
    return (
        call.tool_name in _LEGACY_CRAWLER_TOOLS
        and (
            metadata.get("search_followup") is True
            or argument_metadata.get("search_followup") is True
        )
    )


def _combined_truncated_output(checkpoint: LoopCheckpoint, content: str) -> str:
    fragments = checkpoint.metadata.get("truncated_output_fragments")
    prefix = "".join(str(item) for item in fragments if isinstance(item, str)) if isinstance(fragments, list) else ""
    return f"{prefix}{content}"


def _output_continuation_count(checkpoint: LoopCheckpoint) -> int:
    value = checkpoint.metadata.get("output_continuation_count", 0)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _incomplete_output_error(stop_reason: str) -> RuntimeError:
    return RuntimeError(
        code=RuntimeErrorCode.BUDGET_EXCEEDED,
        message="模型输出达到长度上限，已保留已生成内容但回答尚未完成，请继续请求后续内容。",
        retryable=True,
        category="transient",
        details={"stage": "model_output", "reason": str(stop_reason or "length")},
    )


def _cancellation_error(reason: Optional[str]) -> RuntimeError:
    return RuntimeError(
        code=RuntimeErrorCode.CANCELLED,
        message=reason or "run cancelled",
        retryable=False,
    )


def _trace_id(checkpoint: LoopCheckpoint) -> Optional[str]:
    value = checkpoint.metadata.get("trace_id")
    return str(value) if value else None


def _latest_user_message(checkpoint: LoopCheckpoint) -> str:
    for message in reversed(checkpoint.messages):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            return str(message["content"])
    return ""


def _recovery_tool_name(instruction: str) -> str:
    """Avoid duplicating raw call parameters in reminder trace payloads."""

    match = re.search(r"工具 '([^']+)'", instruction)
    return match.group(1) if match else "tool"


def _is_idempotent_execution_contract(
    execution_contract: dict[str, Any],
    error: RuntimeError,
) -> bool:
    """Replay only trusted, side-effect-free, diagnosable contracts.

    Local built-ins may use their bounded execution contracts directly. Remote
    and hybrid Providers must additionally name the stable error codes that
    qualify for replay; free-form transport errors are only replanned.
    """

    if not (
        execution_contract.get("idempotency") in {"intrinsic", "keyed"}
        and execution_contract.get("side_effect") == "none"
    ):
        return False
    if execution_contract.get("requires_stable_reason_code") is not True:
        return True
    details = error.details if isinstance(error.details, dict) else {}
    reason_code = details.get("reason_code")
    stable_error_codes = execution_contract.get("stable_error_codes")
    return (
        isinstance(reason_code, str)
        and isinstance(stable_error_codes, list)
        and reason_code in stable_error_codes
    )


def _is_loop_pattern_guidance(guidance) -> bool:
    return guidance is not None and guidance.reason_code.startswith("loop.pattern.")


def _declares_artifact_output(intent: ToolCallIntent) -> bool:
    """识别产物写入意图，同时避免 AgentLoop 耦合具体工具实现。"""

    output_uris = intent.arguments.get("output_uris")
    if isinstance(output_uris, list) and any(isinstance(uri, str) and uri for uri in output_uris):
        return True
    if any(
        isinstance(intent.arguments.get(key), str) and bool(intent.arguments[key])
        for key in ("output_uri", "target_uri")
    ):
        return True
    # ``uri`` is generally an input location for read/edit tools.  Treat it
    # as an output only for the two write tools that explicitly declare it as
    # a compatibility alias for target_uri.
    return (
        intent.tool_name in _FILE_WRITE_TOOLS
        and isinstance(intent.arguments.get("uri"), str)
        and bool(intent.arguments["uri"])
    )


def _normalize_file_write_tool_intents(
    intents: List[ToolCallIntent],
) -> List[ToolCallIntent]:
    """Canonicalize the declared file-write URI alias before policy checks.

    The model-facing catalog intentionally accepts ``uri`` for write tools as
    a compatibility spelling.  All Runtime policy code must nevertheless
    inspect the canonical target_uri.  A contradictory pair remains unchanged
    here so ToolHost can return its structured invalid-request result; it is
    never made executable by this helper.
    """

    normalized: List[ToolCallIntent] = []
    for intent in intents:
        if intent.tool_name not in _FILE_WRITE_TOOLS:
            normalized.append(intent)
            continue
        try:
            arguments = normalize_argument_aliases(
                intent.arguments,
                _FILE_WRITE_TARGET_URI_ALIASES,
            )
        except ToolArgumentAliasError:
            normalized.append(intent)
            continue
        normalized.append(replace(intent, arguments=arguments))
    return normalized


def _is_runtime_owned_local_dev_project_write(
    checkpoint: LoopCheckpoint,
    intent: ToolCallIntent,
) -> bool:
    """Admit bounded project source/config writes for a Code surface."""

    # 源码写入资格由两类 Runtime 拥有的事实决定：显式实现请求标记，或
    # Desktop local_dev Host 预算戳。后者与 objective 措辞无关，使会话内
    # 续接的修改请求（如“背景改成渐变”）不会因关键词未命中被误套
    # Artifact 导出门禁。两条路径都只豁免受限源码/配置路径；Workspace
    # grant、审批和 declared-output 契约仍在后续门禁执行。
    if not is_code_execution(checkpoint.metadata):
        return False
    # ``edit_file`` uses its existing independent local-file authorization
    # path and is not classified as an Artifact output by this guard.
    if intent.tool_name == "cowork.file.write_text":
        target_uri = intent.arguments.get("target_uri")
        alias_uri = intent.arguments.get("uri")
        # A contradictory alias pair is invalid at ToolHost and must not be
        # treated as a permitted project write by the earlier artifact guard.
        if target_uri is not None and alias_uri is not None and target_uri != alias_uri:
            return False
        return is_local_dev_project_source_uri(target_uri if target_uri is not None else alias_uri)
    if intent.tool_name == "cowork.file.edit_file":
        # Editing an existing project source is a code mutation, not a new
        # user-facing Artifact. It remains subject to read-before-edit and
        # the independent workspace/approval checks in ToolHost.
        return is_local_dev_project_source_uri(intent.arguments.get("uri"))
    if intent.tool_name in {"cowork.script.run_node", "cowork.script.run_python"}:
        output_uris = intent.arguments.get("output_uris")
        return (
            isinstance(output_uris, list)
            and bool(output_uris)
            and all(is_local_dev_project_source_uri(uri) for uri in output_uris)
        )
    return False


def _local_dev_command_argv(call: ToolCall) -> tuple[str, ...]:
    """Return the already schema-validated local command arguments safely."""

    argv = call.arguments.get("argv")
    return tuple(item.casefold() for item in argv) if isinstance(argv, list) and all(
        isinstance(item, str) for item in argv
    ) else ()


def _local_dev_project_source_evidence(call: ToolCall) -> bool:
    """Recognize one successful source creation/write receipt.

    A hand-authored project may use the already-approved Desktop command tool
    to write its initial files with ``node -e`` or ``python -c``. Those
    commands have no ``output_uris`` contract, so accept them only when their
    structured argv explicitly contains a source-file write expression.
    This remains lifecycle evidence only: Electron Main has already enforced
    the grant, approval and command policy before this function sees a result.
    """

    if call.tool_name == "cowork.file.write_text":
        return is_local_dev_project_source_uri(call.arguments.get("target_uri"))
    if call.tool_name == "cowork.file.edit_file":
        # Editing an existing project source is also a durable source receipt.
        # Reads and directory inspection stay excluded, so only a successful
        # mutation can satisfy the project_source lifecycle fact.
        return is_local_dev_project_source_uri(call.arguments.get("uri"))
    if call.tool_name in {"cowork.script.run_node", "cowork.script.run_python"}:
        outputs = call.arguments.get("output_uris")
        return (
            isinstance(outputs, list)
            and bool(outputs)
            and all(is_local_dev_project_source_uri(uri) for uri in outputs)
        )
    if call.tool_name != "cowork.local_dev.command" or call.arguments.get("operation") != "run":
        return False
    argv = _local_dev_command_argv(call)
    # Standard scaffold commands are direct evidence. A bare Node mkdir is
    # not enough; hand-authored projects must visibly write a bounded source
    # path in the successful, approved Host command receipt.
    is_scaffold = (
        len(argv) >= 3
        and argv[0] in {"npm", "pnpm", "yarn", "bun"}
        and argv[1] in {"create", "init"}
    )
    if is_scaffold:
        return True
    if len(argv) != 3:
        return False
    # ISSUE-175：模型自愈形态会把 server.js / public/index.html 等受控源文件
    # 写到项目根目录（无 src/、非 npm 脚手架）。只有 Host 成功回执才会到达
    # 这里，grant 与审批已在 Electron Main 强制执行。
    program = argv[2]
    if argv[:2] == ("node", "-e"):
        return "writefilesync" in program and bool(_NODE_E_SOURCE_FILE_PATTERN.search(program))
    # ISSUE-179：run_2733d010 通过 ``python3 -c`` 成功写入 main.py、
    # package.json 等项目文件，但旧证据只承认 Node 写入，致使已经 build 和
    # verified 的项目仍缺 project_source。仅匹配紧邻 ``open(...).write(...)``
    # 的受控源码后缀；mkdir、读文件和普通 Python 计算都不构成源码证据。
    return (
        argv[:2] in {("python", "-c"), ("python3", "-c")}
        and bool(_PYTHON_E_SOURCE_FILE_PATTERN.search(program))
    )


# 相对路径 + 受控后缀的源文件名（server.js、public/index.html 等）。
# 只匹配以引号包裹的 writeFileSync 目标形态，避免把读取或注释误判为写入。
_NODE_E_SOURCE_FILE_PATTERN = re.compile(
    r"""writefilesync\(\s*['"](?:\./)?"""
    r"""(?:[\w.-]+/)*"""
    r"""[\w-]+\.(?:js|mjs|cjs|ts|jsx|tsx|vue|svelte|html|css|json|py|md)"""
    r"""['"]""",
    re.IGNORECASE,
)

# Python ``-c`` 形态可用变量拼接文件名（例如 ``base + '/main.py'``），
# 因此不要求完整路径字面量，只要求 ``open`` 目标含允许的源码/配置后缀，
# 且紧接实际 ``.write`` 调用。该模式不会把 ``open(...).read()`` 或 mkdir
# 当成写入事实。
_PYTHON_E_SOURCE_FILE_PATTERN = re.compile(
    r"""open\([^)]*['"][^'"]*\.(?:"""
    r"""py|pyi|js|mjs|cjs|ts|tsx|jsx|vue|svelte|html|css|json|"""
    r"""toml|ya?ml|txt|md|go|rs|java|kt|cs|dart|php|rb|swift|sh)"""
    r"""['"][^)]*\)\s*\.write\(""",
    re.IGNORECASE,
)


def _local_dev_dependency_install_evidence(call: ToolCall) -> bool:
    if call.tool_name != "cowork.local_dev.command":
        return False
    if call.arguments.get("operation") == "install_python_dependencies":
        # The dedicated Host operation creates an authorized project .venv and
        # only installs a declared manifest or restricted package specs.
        return True
    if call.arguments.get("operation") != "run":
        return False
    argv = _local_dev_command_argv(call)
    return (
        len(argv) >= 2
        and argv[0] in {"npm", "pnpm", "yarn", "bun"}
        and argv[1] in {"install", "i", "add"}
    )


def _local_dev_build_evidence(call: ToolCall) -> bool:
    if call.tool_name != "cowork.local_dev.command" or call.arguments.get("operation") != "run":
        return False
    argv = _local_dev_command_argv(call)
    return (
        len(argv) >= 2
        and argv[0] in {"npm", "pnpm", "yarn", "bun"}
        and (
            argv[1] == "build"
            or (len(argv) >= 3 and argv[1] == "run" and argv[2] == "build")
        )
    )


def _local_dev_dependency_install_attempt(call: ToolCall) -> bool:
    """Whether the provider tried to install dependencies, regardless of outcome.

    与成功证据不同，尝试事实用于 ISSUE-175 的无依赖豁免排除：一旦模型
    发起过依赖安装，项目即声明了外部依赖，install 成功回执必须保留。
    """

    if call.tool_name != "cowork.local_dev.command":
        return False
    if call.arguments.get("operation") == "install_python_dependencies":
        return True
    if call.arguments.get("operation") != "run":
        return False
    argv = _local_dev_command_argv(call)
    return (
        len(argv) >= 2
        and argv[0] in {"npm", "pnpm", "yarn", "bun"}
        and argv[1] in {"install", "i", "add"}
    )


def _local_dev_verified_server_evidence(call: ToolCall, result: ToolResult) -> bool:
    operation = call.arguments.get("operation")
    # Electron Main also promotes a structured `run` request whose argv is a
    # dev-server command to its start_dev_server executor.  Keep the receipt
    # classifier aligned with that Host behavior; otherwise a verified server
    # is returned successfully but the lifecycle remains permanently missing
    # `dev_server_verified` and the Run is blocked on its repair turn.
    if operation not in {"start_dev_server", "run"}:
        return False
    if operation == "run" and not _local_dev_is_start_command(call):
        return False
    if (
        call.tool_name != "cowork.local_dev.command"
        or operation not in {"start_dev_server", "run"}
        or not isinstance(result.output, Mapping)
    ):
        return False
    host_result = result.output.get("result")
    server = host_result.get("dev_server") if isinstance(host_result, Mapping) else None
    if not isinstance(server, Mapping) or server.get("readiness") != "verified":
        return False
    url = server.get("url")
    return isinstance(url, str) and url.startswith(("http://127.0.0.1:", "http://localhost:"))


def _local_dev_service_key(call: ToolCall) -> str:
    """Identify one service receipt without trusting model-provided labels."""

    workspace = str(call.arguments.get("workspace_uri") or "")
    argv = _local_dev_command_argv(call)
    return f"{workspace}|{' '.join(argv)}"


def _local_dev_is_start_command(call: ToolCall) -> bool:
    """Mirror Desktop's bounded package-script server promotion."""

    argv = _local_dev_command_argv(call)
    if len(argv) < 2 or argv[0] not in {"npm", "pnpm", "yarn", "bun"}:
        return False
    script = argv[1]
    if script in {"dev", "start", "serve", "preview"}:
        return True
    return len(argv) >= 3 and script == "run" and argv[2] in {
        "dev", "start", "serve", "preview",
    }




def _confidence(metadata: object) -> float | None:
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("confidence")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0.0, min(1.0, float(value)))
    return None


def _owns_run_lifecycle(checkpoint: LoopCheckpoint) -> bool:
    return checkpoint.metadata.get("orchestrated") is not True


def _defer_user_response(checkpoint: LoopCheckpoint) -> bool:
    """Whether this isolated plan step may persist evidence only."""

    return checkpoint.metadata.get("defer_user_response") is True


def _session_artifact_intent_needs_clarification(checkpoint: LoopCheckpoint) -> bool:
    """Read only the Runtime-validated classifier result, never prose heuristics."""

    resolution = checkpoint.metadata.get("session_artifact_intent_resolution")
    return isinstance(resolution, dict) and resolution.get("needs_clarification") is True


_CLARIFICATION_REQUEST = re.compile(
    r"[?？]|(?:请|麻烦|烦请)\s*(?:提供|确认|选择|说明|告诉|补充|上传|指定)|"
    r"(?:需要|缺少|还需)\s*(?:更多|补充)?(?:信息|资料|文件|路径|主题|受众|格式|内容)"
)


def _is_clarification_request(content: object) -> bool:
    """Accept an artifact clarification only when the response asks the user for input.

    The classifier tells Runtime that a delivery intent is unresolved, but it
    does not turn arbitrary model prose into a completed clarification. This
    guard preserves the repair/replan path for statements such as "the script
    did not create a file".
    """

    return isinstance(content, str) and bool(_CLARIFICATION_REQUEST.search(content))


_TEXTUAL_TOOL_CALL = re.compile(
    r"(?m)^\s*(?:```[a-zA-Z]*\s*)?(?:cowork\.)?[a-z0-9_.]+\.[a-z0-9_]+\s*\("
)


def _looks_like_textual_tool_call(content: object) -> bool:
    """Detect a tool invocation printed as prose instead of a function call.

    Providers occasionally render the intended call as plain text after a
    repair instruction. Runtime must tell them apart from ordinary prose so the
    follow-up instruction can name the real mistake.
    """

    if not isinstance(content, str) or not content.strip():
        return False
    return bool(_TEXTUAL_TOOL_CALL.search(content))


_LOW_INFORMATION_ASSISTANT_TEXT = re.compile(
    r"^(?:分析已完成[，。,:：]?\s*(?:结论已在上方完整呈现|无需生成文件[^。]*。?)[。.!！]?|"
    r"已完成本步骤[^。]*。?|结论见上方[^。]*。?)$",
    re.IGNORECASE,
)


def _is_low_information_assistant_text(content: str) -> bool:
    """Recognize pointer-only summaries that contain no actual answer."""

    normalized = " ".join(str(content or "").split())
    return bool(normalized) and len(normalized) <= 120 and bool(
        _LOW_INFORMATION_ASSISTANT_TEXT.fullmatch(normalized)
    )


def _latest_substantial_assistant_content(event_store: EventStore, run_id: str) -> str:
    """Return the latest rich assistant event for cross-step preservation."""

    events = event_store.list_events(run_id, after_seq=0, limit=1000)
    for event in reversed(events):
        if event.type is not RuntimeEventType.TURN_ASSISTANT_TEXT:
            continue
        content = str(event.payload.get("content") or "").strip()
        if len(content) >= 120 and not _is_low_information_assistant_text(content):
            return content
    return ""


def _artifact_refs(
    output: object,
    *,
    artifact_intent: object = None,
) -> List[ArtifactRef]:
    if not isinstance(output, dict):
        return []
    raw_artifacts = output.get("artifacts")
    if not isinstance(raw_artifacts, list):
        return []
    artifacts: List[ArtifactRef] = []
    for raw in raw_artifacts:
        try:
            artifacts.append(ArtifactRef.model_validate(raw))
        except (TypeError, ValueError):
            continue
    try:
        intent = ArtifactIntent.model_validate(artifact_intent)
    except (TypeError, ValueError):
        intent = None
    if intent is None:
        return artifacts

    formats = [_artifact_format(artifact) for artifact in artifacts]
    primary_index = next(
        (index for index, value in enumerate(formats) if value == intent.primary_format),
        next((index for index, value in enumerate(formats) if value is not None), None),
    )
    decorated: List[ArtifactRef] = []
    for index, artifact in enumerate(artifacts):
        artifact_format = formats[index]
        if artifact.manifest is not None or artifact_format is None:
            decorated.append(artifact)
            continue
        decorated.append(
            artifact.model_copy(
                update={
                    "manifest": ArtifactManifest(
                        purpose=intent.purpose,
                        format=artifact_format,
                        is_primary=index == primary_index,
                        requested_by=intent.format_source,
                        validation_status=ArtifactValidationStatus.VALIDATED,
                        interactive=intent.interactive and artifact_format == ArtifactFormat.HTML,
                    )
                },
                deep=True,
            )
        )
    return decorated


def _normalize_search_text(value: str) -> str:
    return " ".join(str(value or "").split()).strip()


def _score_search_candidate(query: str, title: str, snippet: str, index: int) -> int:
    score = 1000 - index
    query_fold = query.casefold()
    title_fold = title.casefold()
    snippet_fold = snippet.casefold()
    if query_fold and query_fold in title_fold:
        score += 50
    if query_fold and query_fold in snippet_fold:
        score += 20
    for token in (part for part in query_fold.split(" ") if len(part) >= 2):
        if token in title_fold:
            score += 8
        if token in snippet_fold:
            score += 4
    return score


def _search_site_constraint(query: str) -> str | None:
    match = re.search(r"(?:^|\s)site:([^\s]+)", query, flags=re.IGNORECASE)
    return match.group(1).strip().lower().rstrip("/") if match else None


def _same_or_subdomain(url: str, required_host: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    required = required_host.removeprefix("www.")
    host = host.removeprefix("www.")
    return host == required or host.endswith(f".{required}")


def _search_candidate_has_signal(query: str, url: str, title: str, snippet: str) -> bool:
    query_without_site = re.sub(r"(?:^|\s)site:[^\s]+", " ", query, flags=re.IGNORECASE)
    terms = [
        token.casefold()
        for token in re.findall(r"[\w-]+", query_without_site, flags=re.UNICODE)
        if len(token) >= 3
    ]
    haystack = " ".join((url, title, snippet)).casefold()
    return not terms or any(token in haystack for token in terms)


def _web_search_result_signature(search_call: ToolCall, search_result: ToolResult) -> str | None:
    output = search_result.output if isinstance(search_result.output, dict) else None
    if not isinstance(output, dict):
        return None
    raw_results = output.get("results")
    if not isinstance(raw_results, list) or not raw_results:
        return None
    query = _normalize_search_text(str(output.get("query") or search_call.arguments.get("query") or ""))
    bundle: list[dict[str, str]] = []
    for item in raw_results[:_WEB_SEARCH_FOLLOWUP_LIMIT]:
        if not isinstance(item, dict):
            continue
        try:
            url = validate_public_url_syntax(str(item.get("url") or "").strip())
        except OutboundUrlError:
            continue
        bundle.append(
            {
                "url": url,
                "title": _normalize_search_text(str(item.get("title") or "")),
                "snippet": _normalize_search_text(str(item.get("snippet") or ""))[:240],
            }
        )
    if not bundle:
        return None
    bundle.sort(key=lambda item: (item["url"], item["title"]))
    return digest_value({"query": query, "results": bundle})


def _artifact_format(artifact: ArtifactRef) -> ArtifactFormat | None:
    suffix = Path(artifact.title).suffix.lower().lstrip(".")
    aliases = {"markdown": "md", "text": "txt", "jpeg": "jpg"}
    try:
        return ArtifactFormat(aliases.get(suffix, suffix))
    except ValueError:
        return None


def _review_evidence_tools_from_candidates(evidence: ReviewEvidenceResult) -> set[str]:
    """Map Runtime-issued candidate kinds to the next visible capability.

    Candidate refs are validated again by the provider before any read.  This
    catalogue reduction is a separate control: it keeps unrelated expensive
    or semantically invalid operations out of the next model turn entirely.
    """

    tool_by_candidate_kind = {
        "render_page": CRAWLER_REVIEW_RENDER_PAGE_TOOL,
        "review_region": CRAWLER_REVIEW_SELECT_REGION_TOOL,
        "next_page": CRAWLER_REVIEW_FETCH_NEXT_PAGE_TOOL,
        "review_records": CRAWLER_REVIEW_EXTRACT_CANDIDATES_TOOL,
    }
    return {
        tool_by_candidate_kind[candidate.kind]
        for candidate in evidence.candidates
        if candidate.kind in tool_by_candidate_kind
    }
