"""受控评论 Agent 的按轮工具目录与调用范围。

评论采集仍复用通用 ``AgentLoop``；本模块只编译当前 Run 的最小网页能力，
并在 Tool Policy 中复核调用参数。它不读取页面正文、不解释模型推理，也不
改变 ``ToolRegistry`` 的 immutable catalog snapshot。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

from cowork_contracts.crawler import (
    CRAWLER_CRAWL_SITE_TOOL,
    CRAWLER_FETCH_PAGE_TOOL,
    CRAWLER_REVIEW_EVIDENCE_TOOLS,
    CRAWLER_REVIEW_EXTRACT_CANDIDATES_TOOL,
    CRAWLER_REVIEW_FETCH_NEXT_PAGE_TOOL,
    CRAWLER_REVIEW_INSPECT_PAGE_TOOL,
    CRAWLER_REVIEW_RENDER_PAGE_TOOL,
    CRAWLER_REVIEW_SELECT_REGION_TOOL,
    canonical_crawl_url,
)

REVIEW_AGENT_MODE = "crawler_only"
REVIEW_AGENT_PHASE_INITIAL = "initial"
REVIEW_AGENT_PHASE_REPLAN = "replan"
REVIEW_AGENT_PHASE_DELIVERY = "delivery"
REVIEW_AGENT_PHASES = frozenset(
    {
        REVIEW_AGENT_PHASE_INITIAL,
        REVIEW_AGENT_PHASE_REPLAN,
        REVIEW_AGENT_PHASE_DELIVERY,
    }
)
REVIEW_AGENT_MAX_REPLAN_ATTEMPTS = 1
REVIEW_LEGACY_CRAWLER_TOOLS = frozenset({CRAWLER_FETCH_PAGE_TOOL, CRAWLER_CRAWL_SITE_TOOL})
REVIEW_EVIDENCE_CRAWLER_TOOLS = frozenset(CRAWLER_REVIEW_EVIDENCE_TOOLS)
REVIEW_CRAWLER_TOOLS = REVIEW_LEGACY_CRAWLER_TOOLS | REVIEW_EVIDENCE_CRAWLER_TOOLS
# This key is written only by ``AgentLoop`` after it validates a
# ``ReviewEvidenceResult``.  A model must not be able to turn a generic
# replan phase into permission for every evidence capability merely by
# requesting one by name.
REVIEW_EVIDENCE_ALLOWED_TOOLS_KEY = "review_agent_evidence_allowed_tool_names"
REVIEW_EVIDENCE_TOOL_CALLS_KEY = "review_agent_evidence_tool_calls"
REVIEW_EVIDENCE_MAX_TOOL_CALLS = 4


def review_agent_enabled(metadata: Mapping[str, Any] | None) -> bool:
    return isinstance(metadata, Mapping) and metadata.get("review_agent_mode") == REVIEW_AGENT_MODE


def review_evidence_enabled(metadata: Mapping[str, Any] | None) -> bool:
    """Gate the additive v1 evidence route without changing persisted Runs."""

    return review_agent_enabled(metadata) and bool((metadata or {}).get("review_agent_evidence_enabled"))


def review_agent_phase(metadata: Mapping[str, Any] | None) -> str:
    """Return a known phase; malformed persisted state fails closed."""

    if not isinstance(metadata, Mapping):
        return REVIEW_AGENT_PHASE_DELIVERY
    phase = str(metadata.get("review_agent_phase") or "").strip().lower()
    return phase if phase in REVIEW_AGENT_PHASES else REVIEW_AGENT_PHASE_DELIVERY


def visible_manifests(
    manifests: Iterable[Any],
    metadata: Mapping[str, Any] | None,
) -> list[Any]:
    """Compile the provider-visible manifests without mutating the snapshot."""

    disabled_raw = metadata.get("disabled_tool_names") if isinstance(metadata, Mapping) else None
    disabled = {item for item in disabled_raw if isinstance(item, str)} if isinstance(disabled_raw, list) else set()
    review_mode = review_agent_enabled(metadata)
    phase = review_agent_phase(metadata) if review_mode else None
    if review_evidence_enabled(metadata):
        if phase == REVIEW_AGENT_PHASE_INITIAL:
            # First turn only observes the Runtime-fixed page. The model must
            # see an issued candidate before it can render, paginate, select,
            # or project any records.
            allowed = {CRAWLER_REVIEW_INSPECT_PAGE_TOOL}
        elif phase == REVIEW_AGENT_PHASE_REPLAN:
            # A replan is not a blanket grant.  The preceding evidence output
            # carries Runtime-issued candidates (render / region / next page),
            # and AgentLoop derives this small set from that typed output.
            # Missing or malformed persisted state fails closed rather than
            # recovering to the previous broad replan catalog.
            allowed_raw = metadata.get(REVIEW_EVIDENCE_ALLOWED_TOOLS_KEY) if isinstance(metadata, Mapping) else None
            allowed = {
                item
                for item in allowed_raw
                if isinstance(item, str) and item in REVIEW_EVIDENCE_CRAWLER_TOOLS
            } if isinstance(allowed_raw, (list, tuple)) else set()
        elif phase == REVIEW_AGENT_PHASE_DELIVERY:
            allowed = set()
        else:
            allowed = set()
    elif phase == REVIEW_AGENT_PHASE_INITIAL:
        # Compatibility for a Run checkpoint created before ISSUE-103.
        allowed = REVIEW_LEGACY_CRAWLER_TOOLS
    elif phase == REVIEW_AGENT_PHASE_REPLAN:
        allowed = {CRAWLER_CRAWL_SITE_TOOL}
    elif phase == REVIEW_AGENT_PHASE_DELIVERY:
        allowed = set()
    else:
        allowed = None

    result: list[Any] = []
    for manifest in manifests:
        name = manifest.get("name") if isinstance(manifest, Mapping) else getattr(manifest, "name", None)
        if not isinstance(name, str) or not name or name in disabled:
            continue
        if review_mode and (allowed is None or name not in allowed):
            continue
        result.append(manifest)
    # 普通市场调研只需先看公开搜索结果，不应因为模型偏好网页工具而触发
    # Desktop 登录/授权。该限制与评论采集按阶段目录叠加，互不放宽。
    from .research_scope import search_first_manifests

    return search_first_manifests(result, metadata)


def _source_url(metadata: Mapping[str, Any]) -> str | None:
    raw = metadata.get("review_agent_source_url")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return canonical_crawl_url(raw)
    except ValueError:
        return None


def review_call_violation(call: Any, metadata: Mapping[str, Any] | None) -> tuple[str, str] | None:
    """Return a safe reason code when a review call exceeds its Run scope."""

    if not review_agent_enabled(metadata):
        return None
    if str(getattr(call, "run_id", "") or "") != str((metadata or {}).get("review_agent_run_id") or ""):
        return "review.run_mismatch", "review crawler call is not bound to the active review run"
    phase = review_agent_phase(metadata)
    name = str(getattr(call, "tool_name", "") or "")
    if name not in REVIEW_CRAWLER_TOOLS:
        return "review.tool_not_allowed", "review Agent only permits crawler capabilities"
    allowed = visible_manifests([{ "name": name }], metadata)
    if not allowed:
        return "review.phase_tool_not_allowed", "crawler capability is not allowed in the current review phase"

    arguments = getattr(call, "arguments", {})
    if not isinstance(arguments, Mapping):
        return "review.invalid_arguments", "review crawler arguments are outside the controlled scope"
    if review_evidence_enabled(metadata):
        return _review_evidence_call_violation(name, arguments)

    # Legacy scope validation is intentionally retained for persisted Runs.
    if name not in REVIEW_LEGACY_CRAWLER_TOOLS:
        return "review.tool_not_allowed", "review evidence capabilities are not enabled for this run"
    source = _source_url(metadata or {})
    raw_url = arguments.get("url")
    try:
        url = canonical_crawl_url(raw_url)
    except (TypeError, ValueError):
        return "review.origin_mismatch", "review crawler URL must match the user-provided source"
    if source is None or url != source:
        return "review.origin_mismatch", "review crawler URL must match the user-provided source"

    host = urlsplit(source).hostname
    if (
        arguments.get("seed_urls") not in (None, [])
        or arguments.get("query") is not None
        or arguments.get("session_ref") is not None
        or arguments.get("session_scope") is not None
        or arguments.get("interaction_plan") is not None
        or arguments.get("include_url_patterns") not in (None, [])
        or arguments.get("exclude_url_patterns") not in (None, [])
        or arguments.get("allowed_domains") != ([host] if host else [])
        or arguments.get("same_origin") is not True
        or arguments.get("respect_robots") is not True
        or arguments.get("allow_external_sources") is not False
        or arguments.get("max_pages") != 1
        or arguments.get("max_depth") != 0
        or arguments.get("per_domain_limit") != 1
    ):
        return "review.scope_expanded", "review crawler scope cannot add browsing, session, or budget parameters"

    schema = arguments.get("extraction_schema")
    profile = schema.get("profile") if isinstance(schema, Mapping) else None
    if profile != "reviews":
        return "review.extraction_profile_required", "review crawler must use the reviews extraction profile"
    raw_metadata = arguments.get("metadata")
    if not isinstance(raw_metadata, Mapping) or set(raw_metadata) - {
        "runtime_generated_request_id",
        "review_collection",
        "initial_review_target_count",
        "runtime_initial_review_target",
    }:
        return "review.metadata_scope_denied", "review crawler metadata is outside the controlled collection scope"
    return None


def _review_evidence_call_violation(name: str, arguments: Mapping[str, Any]) -> tuple[str, str] | None:
    """Validate only opaque references; no model-authored transport exists."""

    allowed_keys = {
        CRAWLER_REVIEW_INSPECT_PAGE_TOOL: {"version", "request_id", "tool_name"},
        CRAWLER_REVIEW_RENDER_PAGE_TOOL: {"version", "request_id", "tool_name", "candidate_ref"},
        CRAWLER_REVIEW_SELECT_REGION_TOOL: {"version", "request_id", "tool_name", "evidence_ref", "candidate_ref"},
        CRAWLER_REVIEW_FETCH_NEXT_PAGE_TOOL: {"version", "request_id", "tool_name", "candidate_ref"},
        CRAWLER_REVIEW_EXTRACT_CANDIDATES_TOOL: {"version", "request_id", "tool_name", "candidate_ref"},
    }
    expected = allowed_keys.get(name)
    if expected is None:
        return "review.tool_not_allowed", "review evidence capability is not enabled for this run"
    if set(arguments) - expected:
        return "review.evidence_argument_denied", "review evidence calls only accept Runtime-issued opaque references"
    if arguments.get("tool_name") != name:
        return "review.evidence_tool_name_mismatch", "review evidence tool identity is invalid"
    for field in expected - {"version", "request_id", "tool_name"}:
        value = arguments.get(field)
        if not isinstance(value, str) or not value.strip():
            return "review.evidence_ref_required", "review evidence calls require an opaque Runtime-issued reference"
    return None


__all__ = [
    "REVIEW_AGENT_MODE",
    "REVIEW_AGENT_PHASE_INITIAL",
    "REVIEW_AGENT_PHASE_REPLAN",
    "REVIEW_AGENT_PHASE_DELIVERY",
    "REVIEW_AGENT_PHASES",
    "REVIEW_AGENT_MAX_REPLAN_ATTEMPTS",
    "REVIEW_LEGACY_CRAWLER_TOOLS",
    "REVIEW_EVIDENCE_CRAWLER_TOOLS",
    "REVIEW_CRAWLER_TOOLS",
    "REVIEW_EVIDENCE_ALLOWED_TOOLS_KEY",
    "review_agent_enabled",
    "review_evidence_enabled",
    "review_agent_phase",
    "visible_manifests",
    "review_call_violation",
]
