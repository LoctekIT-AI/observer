"""Versioned crawler contracts shared by local and remote Cowork runtimes.

The crawler contract is data-only.  Provider implementations may live in the
Desktop sidecar or a server worker, but both sides must agree on the request,
result, status, diagnostics, artifact, evidence, and resume semantics here.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Mapping, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


CRAWLER_CONTRACT_VERSION = "cowork.crawler.v1"
CRAWLER_DATASET_VERSION = "cowork.crawler.dataset.v1"
CRAWLER_DATASET_ASSET_VERSION = "cowork.crawler.dataset_asset.v1"
CRAWLER_CAPABILITY_CATALOG_VERSION = "cowork.crawler_capability_catalog.v1"
CRAWLER_BROWSER_SESSION_RESOLVE_VERSION = "cowork.crawler.browser_session.resolve.v1"
CRAWLER_FETCH_PAGE_TOOL = "cowork.web.fetch_page"
CRAWLER_CRAWL_SITE_TOOL = "cowork.web.crawl_site"
CRAWLER_EXPORT_DATASET_TOOL = "cowork.web.export_dataset"
# ``review.*`` capabilities are intentionally additive.  The legacy page
# fetch/crawl contracts remain the compatibility path for already persisted
# Runs; the review Agent uses opaque Runtime-issued references instead of
# allowing a model to compose browser or network parameters.
CRAWLER_REVIEW_INSPECT_PAGE_TOOL = "cowork.web.review.inspect_page"
CRAWLER_REVIEW_RENDER_PAGE_TOOL = "cowork.web.review.render_page"
CRAWLER_REVIEW_SELECT_REGION_TOOL = "cowork.web.review.select_region"
CRAWLER_REVIEW_FETCH_NEXT_PAGE_TOOL = "cowork.web.review.fetch_next_page"
CRAWLER_REVIEW_EXTRACT_CANDIDATES_TOOL = "cowork.web.review.extract_candidates"
REVIEW_EVIDENCE_CONTRACT_VERSION = "cowork.crawler.review_evidence.v1"
CRAWLER_REVIEW_EVIDENCE_TOOLS = (
    CRAWLER_REVIEW_INSPECT_PAGE_TOOL,
    CRAWLER_REVIEW_RENDER_PAGE_TOOL,
    CRAWLER_REVIEW_SELECT_REGION_TOOL,
    CRAWLER_REVIEW_FETCH_NEXT_PAGE_TOOL,
    CRAWLER_REVIEW_EXTRACT_CANDIDATES_TOOL,
)
CRAWLER_TOOL_NAMES = (
    CRAWLER_FETCH_PAGE_TOOL,
    CRAWLER_CRAWL_SITE_TOOL,
    *CRAWLER_REVIEW_EVIDENCE_TOOLS,
)
_CATALOG_PATH = Path(__file__).with_name("crawler_capability_catalog.v1.json")
EXTRACTION_PROFILES = ("article", "product", "reviews", "price", "table", "research_report")
INTERACTION_PLAN_VERSION = CRAWLER_CONTRACT_VERSION
INTERACTION_DIAGNOSTIC_CODES = frozenset(
    {
        "crawl.interaction_target_not_found",
        "crawl.interaction_target_blocked",
        "crawl.interaction_no_change",
        "crawl.interaction_timeout",
        "crawl.interaction_user_input_required",
        "crawl.interaction_budget_exhausted",
    }
)

_PROFILE_SYNONYMS = {
    "article": {"article", "文章", "正文", "内容", "summary", "report"},
    "product": {"product", "商品", "产品", "item", "sku"},
    "reviews": {"review", "reviews", "评论", "评价", "口碑", "feedback"},
    "price": {"price", "价格", "售价", "金额", "多少钱"},
    "table": {"table", "表格", "表", "sheet", "列表"},
    "research_report": {
        "research report", "analyst report", "broker report", "研报", "券商研报",
        "研究报告", "分析师报告", "目标价", "投资评级", "评级报告",
    },
}
_PROFILE_MATCH_PRIORITY = ("research_report", "reviews", "price", "product", "table", "article")

PRODUCT_FIELD_SYNONYMS: dict[str, tuple[str, ...]] = {
    "name": ("name", "title", "名称", "商品名", "产品名", "型号名称"),
    "brand": ("brand", "品牌", "厂商", "制造商"),
    "sku": ("sku", "productnumber", "product number", "商品编号", "产品编号", "货号", "型号"),
    "category": ("category", "类别", "分类", "品类"),
    "price": ("price", "价格", "售价", "金额", "多少钱"),
    "currency": ("currency", "货币", "币种"),
    "description": ("description", "desc", "描述", "简介", "说明"),
    "availability": ("availability", "stock", "库存", "是否有货", "可售", "供货"),
    "rating": ("rating", "score", "评分", "星级", "评分"),
    "review_count": ("review_count", "review count", "评论数", "评价数", "评论数量"),
    "material": ("material", "材质", "材料", "面料"),
    "color": ("color", "colour", "颜色", "色号"),
    "size": ("size", "尺寸", "规格", "大小"),
    "dimensions": ("dimensions", "dimension", "长宽高", "宽度", "高度", "深度"),
    "weight": ("weight", "重量", "承重"),
    "warranty": ("warranty", "保修", "质保", "保修期"),
}

PRODUCT_DEFAULT_FIELDS = tuple(PRODUCT_FIELD_SYNONYMS)

RESEARCH_REPORT_FIELD_SYNONYMS: dict[str, tuple[str, ...]] = {
    "institution": ("institution", "broker", "券商", "机构", "研究所"),
    "title": ("title", "标题", "研报标题"),
    "company": ("company", "公司", "标的", "股票", "证券"),
    "published_at": ("published_at", "published", "date", "日期", "发布日期", "发布时间"),
    "rating": ("rating", "recommendation", "评级", "投资评级", "买入", "增持", "卖出"),
    "target_price": ("target_price", "target price", "目标价", "目标价格"),
    "current_price": ("current_price", "current price", "现价", "当前价", "当前股价"),
    "earnings_forecast": ("earnings", "forecast", "盈利预测", "业绩预测", "营收预测", "利润预测"),
    "core_thesis": ("thesis", "观点", "核心观点", "投资逻辑", "核心逻辑"),
    "risks": ("risk", "risks", "风险", "主要风险", "风险提示"),
}

RESEARCH_REPORT_DEFAULT_FIELDS = tuple(RESEARCH_REPORT_FIELD_SYNONYMS)

_PROFILE_PRESETS: dict[str, dict[str, Any]] = {
    "article": {
        "type": "object",
        "properties": {
            "title": {"type": ["string", "null"]},
            "body": {"type": ["string", "null"]},
        },
        "required": ["title", "body"],
        "additionalProperties": False,
    },
    "product": {
        "type": "object",
        "properties": {
            "name": {"type": ["string", "null"]},
            "brand": {"type": ["string", "null"]},
            "sku": {"type": ["string", "null"]},
            "category": {"type": ["string", "null"]},
            "price": {"type": ["number", "string", "null"]},
            "currency": {"type": ["string", "null"]},
            "description": {"type": ["string", "null"]},
            "availability": {"type": ["string", "boolean", "null"]},
            "rating": {"type": ["number", "string", "null"]},
            "review_count": {"type": ["integer", "string", "null"]},
            "material": {"type": ["string", "null"]},
            "color": {"type": ["string", "null"]},
            "size": {"type": ["string", "null"]},
            "dimensions": {"type": ["string", "null"]},
            "weight": {"type": ["string", "number", "null"]},
            "warranty": {"type": ["string", "null"]},
        },
        "required": ["name"],
        "additionalProperties": False,
    },
    "reviews": {
        "type": "object",
        "properties": {
            "author": {"type": ["string", "null"]},
            "rating": {"type": ["number", "string", "null"]},
            "content": {"type": ["string", "null"]},
            "published_at": {"type": ["string", "null"]},
        },
        "required": ["content"],
        "additionalProperties": False,
    },
    "price": {
        "type": "object",
        "properties": {
            "label": {"type": ["string", "null"]},
            "price": {"type": ["number", "string", "null"]},
            "currency": {"type": ["string", "null"]},
            "unit": {"type": ["string", "null"]},
            "available": {"type": ["boolean", "string", "null"]},
        },
        "required": ["price"],
        "additionalProperties": False,
    },
    "table": {
        "type": "object",
        "properties": {
            "row_index": {"type": "integer"},
            "headers": {"type": "array", "items": {"type": "string"}},
            "cells": {"type": "array", "items": {"type": ["string", "number", "null"]}},
        },
        "required": ["row_index", "cells"],
        "additionalProperties": False,
    },
    "research_report": {
        "type": "object",
        "properties": {
            "institution": {"type": ["string", "null"]},
            "title": {"type": ["string", "null"]},
            "company": {"type": ["string", "null"]},
            "published_at": {"type": ["string", "null"]},
            "rating": {"type": ["string", "null"]},
            "target_price": {"type": ["number", "string", "null"]},
            "current_price": {"type": ["number", "string", "null"]},
            "earnings_forecast": {"type": ["string", "null"]},
            "core_thesis": {"type": ["string", "null"]},
            "risks": {"type": ["string", "null"]},
        },
        "required": ["title"],
        "additionalProperties": False,
    },
}


class CrawlStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    PARTIAL = "partial"
    FAILED = "failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


KNOWN_CRAWL_STATUSES = frozenset(item.value for item in CrawlStatus)
TERMINAL_CRAWL_STATUSES = frozenset(
    {
        CrawlStatus.WAITING_INPUT.value,
        CrawlStatus.PARTIAL.value,
        CrawlStatus.FAILED.value,
        CrawlStatus.COMPLETED.value,
        CrawlStatus.CANCELLED.value,
    }
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _canonical_string(value: object, field: str, *, lower: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field} must not be empty")
    return text.lower() if lower else text


def normalize_crawl_status(value: object) -> str:
    """Accept additive future statuses while preserving known terminal rules."""

    return _canonical_string(value, "status", lower=True)


def canonical_crawl_url(value: object) -> str:
    raw = _canonical_string(value, "crawl url")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("crawl url must use http or https")
    if not parsed.hostname:
        raise ValueError("crawl url must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("crawl url must not contain userinfo")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("crawl url has an invalid port") from exc
    hostname = parsed.hostname.lower()
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    if (parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443):
        port = None
    netloc = hostname if port is None else f"{hostname}:{port}"
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = f"/{path}"
    query = _canonical_query(parsed.query)
    return urlunsplit((parsed.scheme.lower(), netloc, path, query, ""))


def _canonical_crawl_domain(value: object) -> str:
    domain = _canonical_string(value, "allowed_domain", lower=True).rstrip(".")
    if domain.startswith("."):
        domain = domain[1:]
    if not domain or any(marker in domain for marker in ("://", "/", "?", "#", "@", "*")):
        raise ValueError("allowed_domain must be a hostname without scheme, path, port, or wildcard")
    try:
        encoded = domain.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("allowed_domain must be a valid hostname") from exc
    labels = encoded.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or any(not (character.isalnum() or character == "-") for character in label)
        for label in labels
    ):
        raise ValueError("allowed_domain must be a valid hostname")
    return encoded


def _host_matches_crawl_domain(hostname: str, allowed_domain: str) -> bool:
    host = hostname.lower().rstrip(".")
    return host == allowed_domain or host.endswith(f".{allowed_domain}")


def is_terminal_crawl_status(status: object) -> bool:
    try:
        normalized = normalize_crawl_status(status)
    except ValueError:
        return False
    return normalized in TERMINAL_CRAWL_STATUSES


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _default_extraction_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


def _is_default_extraction_json_schema(value: Mapping[str, Any]) -> bool:
    return dict(value) == _default_extraction_json_schema()


def _match_extraction_profile(text: str) -> str | None:
    lowered = text.strip().lower()
    if not lowered:
        return None
    best: tuple[int, int, str] | None = None
    for priority, canonical in enumerate(_PROFILE_MATCH_PRIORITY):
        synonyms = _PROFILE_SYNONYMS[canonical]
        score = 100 if lowered == canonical else 0
        for token in synonyms:
            normalized = token.lower()
            if lowered == normalized:
                score += 80
            elif normalized in lowered:
                score += max(1, len(normalized))
        if score <= 0:
            continue
        candidate = (score, -priority, canonical)
        if best is None or candidate > best:
            best = candidate
    return best[2] if best is not None else None


def normalize_extraction_profile(value: object) -> str | None:
    if value is None:
        return None
    text = _canonical_string(value, "extraction profile")
    return _match_extraction_profile(text) or text


def build_extraction_schema(
    target: str,
    *,
    completion_mode: Literal["partial_ok", "complete_required"] = "partial_ok",
    metadata: dict[str, Any] | None = None,
) -> "ExtractionSchema":
    profile = normalize_extraction_profile(target)
    description = _canonical_string(target, "extraction target")
    if profile is None and _looks_like_product_fields(description):
        profile = "product"
    if profile is None and _looks_like_research_report_fields(description):
        profile = "research_report"
    json_schema = _target_schema_for_profile(profile, description)
    return ExtractionSchema(
        profile=profile,
        description=description,
        json_schema=json_schema,
        completion_mode=completion_mode,
        metadata=dict(metadata or {}),
    )


def _target_schema_for_profile(profile: str | None, target: str) -> dict[str, Any]:
    if profile == "product":
        return _product_schema_for_target(target)
    if profile == "research_report":
        return _research_report_schema_for_target(target)
    return (
        deepcopy(_PROFILE_PRESETS[profile]) if profile in _PROFILE_PRESETS else _default_extraction_json_schema()
    )


def _looks_like_product_fields(target: str) -> bool:
    lowered = target.lower()
    return any(
        synonym.lower() in lowered
        for field, synonyms in PRODUCT_FIELD_SYNONYMS.items()
        if field not in {"price", "currency", "rating", "review_count"}
        for synonym in synonyms
    )


def _looks_like_research_report_fields(target: str) -> bool:
    lowered = target.lower()
    return any(
        synonym.lower() in lowered
        for synonyms in RESEARCH_REPORT_FIELD_SYNONYMS.values()
        for synonym in synonyms
    )
def _product_schema_for_target(target: str) -> dict[str, Any]:
    """Build a strict product schema from explicit field words.

    A generic product request receives the stable default field set. A request
    naming fields receives only the recognized subset, so page content cannot
    silently expand the requested output contract.
    """

    lowered = target.lower()
    requested = [
        field
        for field, synonyms in PRODUCT_FIELD_SYNONYMS.items()
        if any(synonym.lower() in lowered for synonym in synonyms)
    ]
    fields = requested or list(PRODUCT_DEFAULT_FIELDS)
    preset = deepcopy(_PROFILE_PRESETS["product"])
    preset["properties"] = {field: preset["properties"][field] for field in fields}
    preset["required"] = ["name"] if "name" in fields else [fields[0]]
    return preset


def _research_report_schema_for_target(target: str) -> dict[str, Any]:
    lowered = _explicit_field_target(target).lower()
    requested = [
        field
        for field, synonyms in RESEARCH_REPORT_FIELD_SYNONYMS.items()
        if any(synonym.lower() in lowered for synonym in synonyms)
    ]
    fields = requested or list(RESEARCH_REPORT_DEFAULT_FIELDS)
    preset = deepcopy(_PROFILE_PRESETS["research_report"])
    preset["properties"] = {field: preset["properties"][field] for field in fields}
    preset["required"] = ["title"] if "title" in fields else [fields[0]]
    return preset


def _explicit_field_target(target: str) -> str:
    """Return the user-described field segment, excluding task context."""

    text = target.strip()
    for marker in ("提取", "抽取", "获取字段", "字段", "包括", "需要", "extract", "fields"):
        index = text.lower().find(marker.lower())
        if index >= 0:
            return text[index + len(marker):].lstrip(" ：:，,、")
    if "：" in text or ":" in text:
        return text.split("：", 1)[-1] if "：" in text else text.split(":", 1)[-1]
    return ""


class ExtractionSchema(_StrictModel):
    version: Literal[CRAWLER_CONTRACT_VERSION] = CRAWLER_CONTRACT_VERSION
    profile: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1000)
    json_schema: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
    )
    completion_mode: Literal["partial_ok", "complete_required"] = "partial_ok"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("profile", "description")
    @classmethod
    def _strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _canonical_string(value, "text")

    @field_validator("profile")
    @classmethod
    def _normalize_profile(cls, value: str | None) -> str | None:
        return normalize_extraction_profile(value)

    @field_validator("json_schema")
    @classmethod
    def _require_json_schema_object(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict) or value.get("type") != "object":
            raise ValueError("extraction json_schema must be an object schema")
        return value

    @model_validator(mode="after")
    def _fill_profile_schema(self) -> "ExtractionSchema":
        resolved_profile = _match_extraction_profile(self.profile or "") or _match_extraction_profile(self.description or "")
        if resolved_profile is not None:
            self.profile = resolved_profile
        if self.profile in _PROFILE_PRESETS and _is_default_extraction_json_schema(self.json_schema):
            self.json_schema = deepcopy(_PROFILE_PRESETS[self.profile])
        return self


class ExtractionRecord(_StrictModel):
    record_id: str = Field(min_length=1, max_length=256)
    index: int = Field(ge=0)
    profile: str | None = Field(default=None, max_length=128)
    fields: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("profile")
    @classmethod
    def _normalize_profile(cls, value: str | None) -> str | None:
        return normalize_extraction_profile(value)


class Coverage(_StrictModel):
    observed_count: int = Field(ge=0)
    expected_count: int | None = Field(default=None, ge=0)
    ratio: float | None = Field(default=None, ge=0, le=1)
    label: str | None = Field(default=None, max_length=128)
    confidence: float | None = Field(default=None, ge=0, le=1)
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("label")
    @classmethod
    def _strip_label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _canonical_string(value, "label")


class CrawlDiagnostic(_StrictModel):
    code: str = Field(min_length=1, max_length=128)
    severity: Literal["info", "warning", "error"] = "error"
    message: str = Field(min_length=1, max_length=2000)
    retryable: bool = False
    category: Literal["model_repairable", "transient", "fatal", "user_action_required", "policy"] = "fatal"
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("code")
    @classmethod
    def _canonical_code(cls, value: str) -> str:
        return _canonical_string(value, "code", lower=True)

    @field_validator("message")
    @classmethod
    def _canonical_message(cls, value: str) -> str:
        return _canonical_string(value, "message")


class ReviewEvidenceRef(_StrictModel):
    """An opaque, scoped handle issued only by the Runtime.

    The reference deliberately has no URL, selector, cookie, header, or
    endpoint field.  Its owner scope is checked by the Runtime store before a
    subsequent capability can use it.
    """

    ref_id: str = Field(min_length=12, max_length=256, pattern=r"^review_(?:evidence|candidate)_[A-Za-z0-9_]+$")
    kind: Literal["static_page", "rendered_page", "review_region", "next_page", "review_records"]
    expires_at: datetime
    parent_ref_id: str | None = Field(default=None, min_length=12, max_length=256)

    @field_validator("ref_id", "parent_ref_id")
    @classmethod
    def _canonical_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _canonical_string(value, "review evidence reference")


class ReviewEvidenceCandidate(_StrictModel):
    """A bounded semantic action candidate, never a model-authored selector."""

    candidate_ref: ReviewEvidenceRef
    label: str = Field(min_length=1, max_length=160)
    kind: Literal["render_page", "review_region", "next_page", "review_records"]
    ordinal: int = Field(ge=0, le=32)

    @field_validator("label")
    @classmethod
    def _canonical_label(cls, value: str) -> str:
        return _canonical_string(value, "review evidence candidate label")


class ReviewRecordSample(_StrictModel):
    """A small de-identified preview, not a Dataset record or a fact commit."""

    ordinal: int = Field(ge=0, le=200)
    rating: float | None = Field(default=None, ge=0, le=5)
    content: str = Field(min_length=1, max_length=600)
    published_at: str | None = Field(default=None, max_length=128)

    @field_validator("content", "published_at")
    @classmethod
    def _canonical_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _canonical_string(value, "review record sample field")


class ReviewEvidenceResult(_StrictModel):
    """Safe projection returned by a fine-grained review evidence tool."""

    version: Literal[REVIEW_EVIDENCE_CONTRACT_VERSION] = REVIEW_EVIDENCE_CONTRACT_VERSION
    request_id: str = Field(min_length=1, max_length=256)
    tool_name: Literal[
        CRAWLER_REVIEW_INSPECT_PAGE_TOOL,
        CRAWLER_REVIEW_RENDER_PAGE_TOOL,
        CRAWLER_REVIEW_SELECT_REGION_TOOL,
        CRAWLER_REVIEW_FETCH_NEXT_PAGE_TOOL,
        CRAWLER_REVIEW_EXTRACT_CANDIDATES_TOOL,
    ]
    status: Literal["completed", "partial", "failed"]
    evidence_ref: ReviewEvidenceRef | None = None
    candidates: list[ReviewEvidenceCandidate] = Field(default_factory=list, max_length=16)
    record_samples: list[ReviewRecordSample] = Field(default_factory=list, max_length=20)
    observed_count: int = Field(default=0, ge=0, le=5000)
    expected_count: int | None = Field(default=None, ge=0, le=5000)
    page_title: str | None = Field(default=None, max_length=256)
    diagnostics: list[CrawlDiagnostic] = Field(default_factory=list, max_length=16)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("request_id", "page_title")
    @classmethod
    def _canonical_optional_fields(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _canonical_string(value, "review evidence result field")

    @model_validator(mode="after")
    def _validate_result_shape(self) -> "ReviewEvidenceResult":
        if self.status == "failed" and self.evidence_ref is not None:
            raise ValueError("failed review evidence result must not issue an evidence reference")
        if self.status != "failed" and self.evidence_ref is None:
            raise ValueError("successful review evidence result requires an evidence reference")
        if self.expected_count is not None and self.observed_count > self.expected_count:
            raise ValueError("review observed_count must not exceed expected_count")
        return self


class ReviewEvidenceInspectRequest(_StrictModel):
    version: Literal[REVIEW_EVIDENCE_CONTRACT_VERSION] = REVIEW_EVIDENCE_CONTRACT_VERSION
    request_id: str = Field(min_length=1, max_length=256)
    tool_name: Literal[CRAWLER_REVIEW_INSPECT_PAGE_TOOL] = CRAWLER_REVIEW_INSPECT_PAGE_TOOL


class ReviewEvidenceRenderRequest(_StrictModel):
    version: Literal[REVIEW_EVIDENCE_CONTRACT_VERSION] = REVIEW_EVIDENCE_CONTRACT_VERSION
    request_id: str = Field(min_length=1, max_length=256)
    tool_name: Literal[CRAWLER_REVIEW_RENDER_PAGE_TOOL] = CRAWLER_REVIEW_RENDER_PAGE_TOOL
    # Rendering is an explicit Runtime-issued action candidate.  Accepting a
    # page evidence ref here would let the model invoke expensive rendering
    # even when the inspection result did not authorize it.
    candidate_ref: str = Field(min_length=12, max_length=256)


class ReviewEvidenceSelectRegionRequest(_StrictModel):
    version: Literal[REVIEW_EVIDENCE_CONTRACT_VERSION] = REVIEW_EVIDENCE_CONTRACT_VERSION
    request_id: str = Field(min_length=1, max_length=256)
    tool_name: Literal[CRAWLER_REVIEW_SELECT_REGION_TOOL] = CRAWLER_REVIEW_SELECT_REGION_TOOL
    evidence_ref: str = Field(min_length=12, max_length=256)
    candidate_ref: str = Field(min_length=12, max_length=256)


class ReviewEvidenceFetchNextPageRequest(_StrictModel):
    version: Literal[REVIEW_EVIDENCE_CONTRACT_VERSION] = REVIEW_EVIDENCE_CONTRACT_VERSION
    request_id: str = Field(min_length=1, max_length=256)
    tool_name: Literal[CRAWLER_REVIEW_FETCH_NEXT_PAGE_TOOL] = CRAWLER_REVIEW_FETCH_NEXT_PAGE_TOOL
    candidate_ref: str = Field(min_length=12, max_length=256)


class ReviewEvidenceExtractCandidatesRequest(_StrictModel):
    version: Literal[REVIEW_EVIDENCE_CONTRACT_VERSION] = REVIEW_EVIDENCE_CONTRACT_VERSION
    request_id: str = Field(min_length=1, max_length=256)
    tool_name: Literal[CRAWLER_REVIEW_EXTRACT_CANDIDATES_TOOL] = CRAWLER_REVIEW_EXTRACT_CANDIDATES_TOOL
    candidate_ref: str = Field(min_length=12, max_length=256)


class EvidenceRef(_StrictModel):
    kind: Literal["page", "artifact", "response", "search_result", "link", "network", "interaction"]
    ref_id: str = Field(min_length=1, max_length=256)
    url: str | None = Field(default=None, max_length=4096)
    title: str | None = Field(default=None, max_length=512)
    source_type: str | None = Field(default=None, max_length=128)
    captured_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("ref_id", "url", "title", "source_type")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _canonical_string(value, "evidence field")


class CrawlArtifactRef(_StrictModel):
    kind: Literal["html", "text", "screenshot", "network", "dom", "json", "log"]
    artifact_id: str = Field(min_length=1, max_length=256)
    uri: str | None = Field(default=None, max_length=4096)
    sha256: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    content_type: str | None = Field(default=None, max_length=256)
    url: str | None = Field(default=None, max_length=4096)
    title: str | None = Field(default=None, max_length=512)
    captured_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("artifact_id", "uri", "content_type", "url", "title")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _canonical_string(value, "artifact field")


class PendingInteraction(_StrictModel):
    version: Literal[CRAWLER_CONTRACT_VERSION] = CRAWLER_CONTRACT_VERSION
    interaction_id: str = Field(min_length=1, max_length=256)
    crawl_job_id: str = Field(min_length=1, max_length=256)
    kind: Literal["login", "captcha", "mfa", "permission", "manual_confirmation", "unknown"]
    message: str = Field(min_length=1, max_length=2000)
    required_action: str | None = Field(default=None, max_length=1000)
    session_ref: str | None = Field(default=None, min_length=1, max_length=512)
    resume_contract_version: Literal[CRAWLER_CONTRACT_VERSION] = CRAWLER_CONTRACT_VERSION
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("interaction_id", "crawl_job_id", "message", "required_action", "session_ref")
    @classmethod
    def _canonical_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _canonical_string(value, "pending interaction field")


class InteractionTarget(_StrictModel):
    """A semantic, read-only target; arbitrary script and coordinates are excluded."""

    text: str | None = Field(default=None, max_length=512)
    aria_label: str | None = Field(default=None, max_length=512)
    role: Literal["button", "tab", "link", "summary", "heading", "region"] | None = None
    data_attributes: dict[str, str] = Field(default_factory=dict, max_length=8)
    css_selector: str | None = Field(default=None, max_length=512)

    @field_validator("text", "aria_label", "css_selector")
    @classmethod
    def _canonical_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _canonical_string(value, "interaction target field")

    @field_validator("data_attributes")
    @classmethod
    def _canonical_data_attributes(cls, value: dict[str, str]) -> dict[str, str]:
        result = {
            _canonical_string(key, "interaction data attribute name"): _canonical_string(
                item, "interaction data attribute value"
            )
            for key, item in value.items()
        }
        if any(not key.lower().startswith("data-") for key in result):
            raise ValueError("interaction data attributes must use data-* names")
        if any(token in key.lower() for key in result for token in ("password", "passwd", "cookie", "token", "authorization", "secret")):
            raise ValueError("interaction data attributes must not contain credentials")
        return result

    @field_validator("css_selector")
    @classmethod
    def _reject_script_selector(cls, value: str | None) -> str | None:
        if value is not None and any(token in value.lower() for token in ("javascript:", " on", "eval(", "script")):
            raise ValueError("interaction css_selector must be a read-only selector")
        return value

    @model_validator(mode="after")
    def _require_selector(self) -> "InteractionTarget":
        if not any((self.text, self.aria_label, self.role, self.data_attributes, self.css_selector)):
            raise ValueError("interaction target requires a semantic selector")
        return self


class InteractionAction(_StrictModel):
    action_id: str = Field(min_length=1, max_length=128)
    kind: Literal["wait", "scroll", "click", "expand", "select_tab", "paginate"]
    target: InteractionTarget | None = None
    timeout_ms: int = Field(default=5000, ge=100, le=120000)
    wait_after_ms: int = Field(default=250, ge=0, le=30000)
    wait_until: list[Literal["time_elapsed", "dom_change", "network_idle", "target_visible", "target_hidden"]] = Field(
        default_factory=lambda: ["dom_change"]
    )
    direction: Literal["up", "down"] = "down"
    distance_px: int = Field(default=1000, ge=0, le=5000)
    to_end: bool = False
    page_number: int | None = Field(default=None, ge=1, le=1000)
    on_failure: Literal["stop", "skip", "continue"] = "stop"

    @field_validator("action_id")
    @classmethod
    def _canonical_action_id(cls, value: str) -> str:
        return _canonical_string(value, "interaction action_id")

    @model_validator(mode="after")
    def _validate_action_shape(self) -> "InteractionAction":
        if self.kind in {"click", "expand", "select_tab", "paginate"} and self.target is None:
            raise ValueError(f"{self.kind} interaction requires target")
        if self.kind == "scroll" and not self.to_end and self.distance_px == 0:
            raise ValueError("scroll interaction requires distance_px or to_end")
        if self.kind != "paginate" and self.page_number is not None:
            raise ValueError("page_number is only valid for paginate interaction")
        return self


class InteractionBudget(_StrictModel):
    max_actions: int = Field(default=16, ge=1, le=64)
    max_duration_ms: int = Field(default=60000, ge=1000, le=300000)
    max_network_responses: int = Field(default=50, ge=1, le=200)
    max_page_bytes: int = Field(default=5 * 1024 * 1024, ge=1024, le=20 * 1024 * 1024)
    max_scrolls: int = Field(default=8, ge=0, le=32)


class InteractionPlan(_StrictModel):
    version: Literal[CRAWLER_CONTRACT_VERSION] = CRAWLER_CONTRACT_VERSION
    actions: list[InteractionAction] = Field(default_factory=list, max_length=64)
    budget: InteractionBudget = Field(default_factory=InteractionBudget)
    generated_by: Literal["runtime", "provider"] = "runtime"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_plan(self) -> "InteractionPlan":
        ids = [action.action_id for action in self.actions]
        if len(ids) != len(set(ids)):
            raise ValueError("interaction action_id values must be unique")
        if len(self.actions) > self.budget.max_actions:
            raise ValueError("interaction plan exceeds budget.max_actions")
        if sum(action.kind == "scroll" for action in self.actions) > self.budget.max_scrolls:
            raise ValueError("interaction plan exceeds budget.max_scrolls")
        return self


class InteractionTrace(_StrictModel):
    action_id: str = Field(min_length=1, max_length=128)
    kind: Literal["wait", "scroll", "click", "expand", "select_tab", "paginate"]
    status: Literal["planned", "started", "succeeded", "skipped", "blocked", "timed_out"]
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0, le=300000)
    page_url: str | None = Field(default=None, max_length=4096)
    before_hash: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    after_hash: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    evidence_ref_ids: list[str] = Field(default_factory=list, max_length=200)
    network_response_refs: list[str] = Field(default_factory=list, max_length=200)
    extraction_record_ids: list[str] = Field(default_factory=list, max_length=1000)
    diagnostics: list[CrawlDiagnostic] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("action_id", "page_url")
    @classmethod
    def _canonical_trace_field(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _canonical_string(value, "interaction trace field")

    @field_validator("evidence_ref_ids", "network_response_refs", "extraction_record_ids")
    @classmethod
    def _canonical_trace_references(cls, value: list[str]) -> list[str]:
        return [_canonical_string(item, "interaction trace reference") for item in value]


class BrowserSessionScope(_StrictModel):
    version: Literal[CRAWLER_CONTRACT_VERSION] = CRAWLER_CONTRACT_VERSION
    session_ref: str = Field(min_length=1, max_length=512)
    user_id: str = Field(min_length=1, max_length=256)
    tenant_id: str = Field(min_length=1, max_length=256)
    allowed_origin: str = Field(min_length=1, max_length=4096)
    environment: Literal["local", "server"]
    source: Literal["os_credential", "desktop_local"] = "os_credential"
    status: Literal["active", "expired", "revoked", "missing", "needs_user"] = "active"
    lease_id: str | None = Field(default=None, min_length=1, max_length=256)
    expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("session_ref", "user_id", "tenant_id", "lease_id")
    @classmethod
    def _canonical_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _canonical_string(value, "browser session field")

    @field_validator("allowed_origin")
    @classmethod
    def _canonical_origin(cls, value: str) -> str:
        origin = _canonical_string(value, "allowed_origin")
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"}:
            raise ValueError("allowed_origin must be an http(s) origin")
        return f"{parsed.scheme}://{parsed.netloc}"


class BrowserSessionResolveRequest(_StrictModel):
    version: Literal[CRAWLER_BROWSER_SESSION_RESOLVE_VERSION] = CRAWLER_BROWSER_SESSION_RESOLVE_VERSION
    user_id: str = Field(min_length=1, max_length=256)
    tenant_id: str = Field(min_length=1, max_length=256)
    url: str = Field(min_length=1, max_length=4096)
    environment: Literal["local", "server"]
    session_ref: str | None = Field(default=None, min_length=1, max_length=512)
    remember_site: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("user_id", "tenant_id", "url", "session_ref")
    @classmethod
    def _canonical_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _canonical_string(value, "browser session resolve request field")


class BrowserSessionResolveResult(_StrictModel):
    version: Literal[CRAWLER_BROWSER_SESSION_RESOLVE_VERSION] = CRAWLER_BROWSER_SESSION_RESOLVE_VERSION
    status: Literal["active", "missing", "expired", "revoked", "denied", "needs_user"]
    session_scope: BrowserSessionScope | None = None
    reason: str | None = Field(default=None, max_length=1000)
    policy_version: str = Field(min_length=1, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("reason", "policy_version")
    @classmethod
    def _canonical_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _canonical_string(value, "browser session resolve result field")

    @model_validator(mode="after")
    def _validate_active_scope(self) -> "BrowserSessionResolveResult":
        if self.status == "active" and self.session_scope is None:
            raise ValueError("active browser session resolve result requires session_scope")
        if self.status != "active" and self.session_scope is not None and self.session_scope.status == "active":
            raise ValueError("non-active browser session result must not include an active session_scope")
        return self


class CrawlRequest(_StrictModel):
    version: Literal[CRAWLER_CONTRACT_VERSION] = CRAWLER_CONTRACT_VERSION
    request_id: str = Field(min_length=1, max_length=256)
    tool_name: Literal[CRAWLER_FETCH_PAGE_TOOL, CRAWLER_CRAWL_SITE_TOOL]
    url: str | None = Field(default=None, max_length=4096)
    seed_urls: list[str] = Field(default_factory=list, max_length=100)
    query: str | None = Field(default=None, max_length=1000)
    extraction_schema: ExtractionSchema | None = None
    session_ref: str | None = Field(default=None, min_length=1, max_length=512)
    session_scope: BrowserSessionScope | None = None
    max_pages: int = Field(default=1, ge=1, le=1000)
    max_depth: int = Field(default=0, ge=0, le=10)
    per_domain_limit: int = Field(default=10, ge=1, le=1000)
    allowed_domains: list[str] = Field(default_factory=list, max_length=100)
    same_origin: bool | None = None
    include_url_patterns: list[str] = Field(default_factory=list, max_length=100)
    exclude_url_patterns: list[str] = Field(default_factory=list, max_length=100)
    respect_robots: bool = True
    allow_external_sources: bool = False
    source_type: str | None = Field(default=None, max_length=128)
    interaction_plan: InteractionPlan | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("request_id", "url", "query", "session_ref", "source_type")
    @classmethod
    def _canonical_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _canonical_string(value, "crawl request field")

    @field_validator("seed_urls")
    @classmethod
    def _canonical_seed_urls(cls, value: list[str]) -> list[str]:
        return [_canonical_string(item, "seed_url") for item in value]

    @field_validator("allowed_domains")
    @classmethod
    def _canonical_allowed_domains(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(_canonical_crawl_domain(item) for item in value))

    @field_validator("include_url_patterns", "exclude_url_patterns")
    @classmethod
    def _canonical_url_patterns(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(_canonical_string(item, "url_pattern") for item in value))

    @model_validator(mode="after")
    def _validate_tool_shape(self) -> "CrawlRequest":
        if self.tool_name == CRAWLER_FETCH_PAGE_TOOL:
            if not self.url:
                raise ValueError("fetch_page request requires url")
            if self.seed_urls:
                raise ValueError("fetch_page request must not include seed_urls")
        if self.tool_name == CRAWLER_CRAWL_SITE_TOOL and not (self.url or self.seed_urls):
            raise ValueError("crawl_site request requires url or seed_urls")
        if self.allowed_domains:
            for candidate in [self.url, *self.seed_urls]:
                if candidate is None:
                    continue
                hostname = urlsplit(canonical_crawl_url(candidate)).hostname or ""
                if not any(_host_matches_crawl_domain(hostname, domain) for domain in self.allowed_domains):
                    raise ValueError("crawl request URL is outside allowed_domains")
        if self.same_origin is True:
            origins = {
                urlunsplit((*urlsplit(canonical_crawl_url(candidate))[:2], "", "", ""))
                for candidate in [self.url, *self.seed_urls]
                if candidate is not None
            }
            if len(origins) > 1:
                raise ValueError("same_origin request URLs must share one origin")
        if self.session_scope is not None:
            if self.session_ref and self.session_ref != self.session_scope.session_ref:
                raise ValueError("session_ref must match session_scope.session_ref")
            self.session_ref = self.session_scope.session_ref
        return self


class ResumeCrawlRequest(_StrictModel):
    version: Literal[CRAWLER_CONTRACT_VERSION] = CRAWLER_CONTRACT_VERSION
    crawl_job_id: str = Field(min_length=1, max_length=256)
    interaction_id: str = Field(min_length=1, max_length=256)
    session_ref: str = Field(min_length=1, max_length=512)
    resumed_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("crawl_job_id", "interaction_id", "session_ref")
    @classmethod
    def _canonical_required(cls, value: str) -> str:
        return _canonical_string(value, "resume crawl field")


class CrawlPageResult(_StrictModel):
    url: str = Field(min_length=1, max_length=4096)
    final_url: str | None = Field(default=None, max_length=4096)
    title: str | None = Field(default=None, max_length=512)
    fetched_at: datetime | None = None
    status_code: int | None = Field(default=None, ge=100, le=599)
    content_type: str | None = Field(default=None, max_length=256)
    content_text: str | None = Field(default=None, max_length=20000)
    coverage: Coverage | None = None
    extraction_coverage: Coverage | None = None
    extraction_records: list[ExtractionRecord] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    artifact_refs: list[CrawlArtifactRef] = Field(default_factory=list)
    diagnostics: list[CrawlDiagnostic] = Field(default_factory=list)
    interaction_traces: list[InteractionTrace] = Field(default_factory=list, max_length=200)
    source_type: str | None = Field(default=None, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("url", "final_url", "title", "content_type", "content_text", "source_type")
    @classmethod
    def _canonical_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _canonical_string(value, "crawl page field")


class CrawlResult(_StrictModel):
    version: Literal[CRAWLER_CONTRACT_VERSION] = CRAWLER_CONTRACT_VERSION
    request_id: str = Field(min_length=1, max_length=256)
    crawl_job_id: str = Field(min_length=1, max_length=256)
    tool_name: Literal[CRAWLER_FETCH_PAGE_TOOL, CRAWLER_CRAWL_SITE_TOOL]
    status: str = Field(min_length=1, max_length=128)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    pages: list[CrawlPageResult] = Field(default_factory=list)
    summary_text: str | None = Field(default=None, max_length=20000)
    coverage: Coverage | None = None
    extraction_coverage: Coverage | None = None
    extraction_records: list[ExtractionRecord] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    artifact_refs: list[CrawlArtifactRef] = Field(default_factory=list)
    diagnostics: list[CrawlDiagnostic] = Field(default_factory=list)
    interaction_traces: list[InteractionTrace] = Field(default_factory=list, max_length=500)
    pending_interaction: PendingInteraction | None = None
    session_ref: str | None = Field(default=None, min_length=1, max_length=512)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("request_id", "crawl_job_id", "summary_text", "session_ref")
    @classmethod
    def _canonical_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _canonical_string(value, "crawl result field")

    @field_validator("status")
    @classmethod
    def _canonical_status(cls, value: str) -> str:
        return normalize_crawl_status(value)

    @model_validator(mode="after")
    def _validate_waiting_input(self) -> "CrawlResult":
        if self.status == CrawlStatus.WAITING_INPUT.value and self.pending_interaction is None:
            raise ValueError("waiting_input result requires pending_interaction")
        if self.pending_interaction is not None and self.status != CrawlStatus.WAITING_INPUT.value:
            raise ValueError("pending_interaction is only allowed for waiting_input results")
        trace_ids = [trace.action_id for trace in self.interaction_traces]
        if len(trace_ids) != len(set(trace_ids)):
            raise ValueError("interaction trace action_id values must be unique per result")
        return self

    @property
    def is_terminal(self) -> bool:
        return is_terminal_crawl_status(self.status)


class CrawlerDatasetRef(_StrictModel):
    """同一会话内可复用的结构化爬虫结果引用。

    记录本体由 Runtime Artifact/Session Store 保管，不进入模型上下文；模型只
    能拿到这个引用并交给受控的导出能力，避免为了导出而再次访问原站。
    """

    version: Literal[CRAWLER_DATASET_VERSION] = CRAWLER_DATASET_VERSION
    dataset_id: str = Field(min_length=1, max_length=256)
    source_run_id: str = Field(min_length=1, max_length=256)
    source_url: str = Field(min_length=1, max_length=4096)
    profile: str | None = Field(default=None, max_length=128)
    record_count: int = Field(ge=0)
    coverage: Coverage | None = None
    status: Literal["completed", "partial"]
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Optional during the SQLite migration so pre-versioning assets remain
    # readable and exportable without a data rewrite.
    dataset_version_id: str | None = Field(default=None, min_length=1, max_length=256)
    parent_dataset_version_id: str | None = Field(default=None, min_length=1, max_length=256)
    quality: Optional["CrawlerDatasetQuality"] = None

    @field_validator("dataset_id", "source_run_id", "source_url")
    @classmethod
    def _canonical_required(cls, value: str) -> str:
        return _canonical_string(value, "crawler dataset field")

    @field_validator("profile")
    @classmethod
    def _normalize_profile(cls, value: str | None) -> str | None:
        return normalize_extraction_profile(value)


class CrawlerDataset(_StrictModel):
    """持久化的爬虫数据集，仅供 Runtime 受控能力读取。"""

    ref: CrawlerDatasetRef
    records: list[ExtractionRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def _match_record_count(self) -> "CrawlerDataset":
        if self.ref.record_count != len(self.records):
            raise ValueError("crawler dataset record_count must match records")
        return self


class CrawlerDatasetQuality(_StrictModel):
    """Deterministic readiness gate for persisted crawler records."""

    version: Literal[CRAWLER_DATASET_ASSET_VERSION] = CRAWLER_DATASET_ASSET_VERSION
    status: Literal["analysis_ready", "export_only"]
    checked_at: datetime = Field(default_factory=utc_now)
    profile: str | None = Field(default=None, max_length=128)
    record_count: int = Field(ge=0)
    field_completion: dict[str, float] = Field(default_factory=dict)
    reason_codes: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("profile")
    @classmethod
    def _normalize_profile(cls, value: str | None) -> str | None:
        return normalize_extraction_profile(value)


class CrawlerDatasetVersion(_StrictModel):
    """An immutable snapshot; newer collections point back to their parent."""

    version: Literal[CRAWLER_DATASET_ASSET_VERSION] = CRAWLER_DATASET_ASSET_VERSION
    dataset_version_id: str = Field(min_length=1, max_length=256)
    dataset_id: str = Field(min_length=1, max_length=256)
    parent_dataset_version_id: str | None = Field(default=None, min_length=1, max_length=256)
    source_run_id: str = Field(min_length=1, max_length=256)
    created_at: datetime = Field(default_factory=utc_now)
    dataset: CrawlerDataset

    @model_validator(mode="after")
    def _match_dataset_identity(self) -> "CrawlerDatasetVersion":
        if self.dataset.ref.dataset_id != self.dataset_id:
            raise ValueError("crawler dataset version dataset_id must match dataset")
        if self.dataset.ref.dataset_version_id not in {None, self.dataset_version_id}:
            raise ValueError("crawler dataset version id must match dataset")
        return self


class SessionCrawlerDatasetLink(_StrictModel):
    """A Session pin to the dataset version currently used for follow-ups."""

    version: Literal[CRAWLER_DATASET_ASSET_VERSION] = CRAWLER_DATASET_ASSET_VERSION
    session_id: str = Field(min_length=1, max_length=256)
    dataset_id: str = Field(min_length=1, max_length=256)
    dataset_version_id: str = Field(min_length=1, max_length=256)
    pinned_at: datetime = Field(default_factory=utc_now)


class CrawlerDatasetExportRequest(_StrictModel):
    version: Literal[CRAWLER_DATASET_VERSION] = CRAWLER_DATASET_VERSION
    request_id: str = Field(min_length=1, max_length=256)
    tool_name: Literal[CRAWLER_EXPORT_DATASET_TOOL] = CRAWLER_EXPORT_DATASET_TOOL
    dataset_id: str = Field(min_length=1, max_length=256)
    output_uri: str = Field(min_length=1, max_length=4096)
    format: Literal["csv", "xlsx"] = "csv"
    metadata: dict[str, Any] = Field(default_factory=dict)


class CrawlerCapability(_StrictModel):
    name: Literal[
        CRAWLER_FETCH_PAGE_TOOL,
        CRAWLER_CRAWL_SITE_TOOL,
        CRAWLER_REVIEW_INSPECT_PAGE_TOOL,
        CRAWLER_REVIEW_RENDER_PAGE_TOOL,
        CRAWLER_REVIEW_SELECT_REGION_TOOL,
        CRAWLER_REVIEW_FETCH_NEXT_PAGE_TOOL,
        CRAWLER_REVIEW_EXTRACT_CANDIDATES_TOOL,
    ]
    display_name: str
    version: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    permissions: list[str] = Field(default_factory=list)
    output_mode: str = "paged"
    max_output_chars: int = Field(default=24000, ge=512)
    spill_threshold_bytes: int = Field(default=131072, ge=1024)
    secret_fields: list[str] = Field(default_factory=list)
    provider_id: str = "crawler"
    execution_location: Literal["local", "remote", "hybrid"] = "hybrid"
    side_effect: Literal["none", "local_mutation", "remote_mutation"] = "none"
    idempotency: Literal["none", "keyed", "intrinsic"] = "none"
    timeout_ms: int = Field(default=120000, ge=1)
    availability: Literal["ready", "degraded", "unavailable"] = "ready"
    certification: Literal["draft", "tested", "certified", "published", "deprecated"] = "certified"
    metadata: dict[str, Any] = Field(default_factory=dict)


class CrawlerCapabilityCatalog(_StrictModel):
    version: Literal[CRAWLER_CAPABILITY_CATALOG_VERSION]
    contract_versions: dict[str, Literal[CRAWLER_CONTRACT_VERSION, REVIEW_EVIDENCE_CONTRACT_VERSION]]
    capabilities: list[CrawlerCapability] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_unique_tools(self) -> "CrawlerCapabilityCatalog":
        names = [item.name for item in self.capabilities]
        if len(names) != len(set(names)):
            raise ValueError("crawler capability catalog contains duplicate tools")
        return self


ExtractionSchema.model_rebuild()
ExtractionRecord.model_rebuild()
CrawlerDatasetRef.model_rebuild()
CrawlPageResult.model_rebuild()
CrawlResult.model_rebuild()


@lru_cache(maxsize=1)
def load_crawler_capability_catalog() -> CrawlerCapabilityCatalog:
    raw = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    return CrawlerCapabilityCatalog.model_validate(raw)


def crawler_capabilities() -> tuple[CrawlerCapability, ...]:
    return tuple(load_crawler_capability_catalog().capabilities)


def crawler_capability(tool_name: str) -> CrawlerCapability | None:
    return next((item for item in crawler_capabilities() if item.name == tool_name), None)


def canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


_CRAWLER_DIAGNOSTIC_CODE_ALIASES = {
    "crawl.http_error": "http_error",
    "crawl.rate_limited": "rate_limited",
    "crawl.redirect_limit_exceeded": "network_timeout",
    "crawl.redirect_missing_location": "http_blocked",
    "crawl.unsupported_content_type": "content_blocked",
    "crawl.robots_denied": "robots_denied",
    "crawl.robots_unavailable": "robots_unavailable",
    "crawl.login_required": "auth_required",
    "crawl.extraction_empty": "content_empty",
    "crawl.extraction_failed": "content_empty",
    "crawl.extraction_partial": "partial_content",
    "crawl.browser_rendered": "browser_rendered",
    "crawl.browser_worker_timeout": "network_timeout",
    "crawl.browser_worker_failed": "browser_worker_failed",
    "crawl.browser_worker_protocol_error": "browser_worker_protocol_error",
    "crawl.browser_worker_unavailable": "browser_worker_unavailable",
    "crawl.browser_blocked_cross_origin": "http_blocked",
    "crawl.browser_blocked_non_read_request": "http_blocked",
    "crawl.browser_cross_origin_navigation": "http_blocked",
    "crawl.browser_size_limit": "content_blocked",
}


def normalize_crawl_diagnostic_code(value: object) -> str:
    code = _canonical_string(value, "diagnostic code", lower=True)
    return _CRAWLER_DIAGNOSTIC_CODE_ALIASES.get(code, code)


def crawl_diagnostic_signature(diagnostic: Mapping[str, Any] | "CrawlDiagnostic") -> str:
    payload: dict[str, Any]
    if isinstance(diagnostic, Mapping):
        payload = dict(diagnostic)
    else:
        payload = diagnostic.model_dump(mode="json")
    return hashlib.sha256(
        canonical_json(
            {
                "code": normalize_crawl_diagnostic_code(payload.get("code")),
                "severity": str(payload.get("severity") or ""),
                "retryable": bool(payload.get("retryable", False)),
                "category": str(payload.get("category") or ""),
                "details": payload.get("details") if isinstance(payload.get("details"), Mapping) else {},
            }
        )
    ).hexdigest()


def crawl_attempt_fingerprint(
    *,
    url: str,
    strategy: str,
    source_level: str | None = None,
    status: str | None = None,
    summary_text: str | None = None,
    diagnostics: list[Mapping[str, Any] | "CrawlDiagnostic"] | None = None,
) -> str:
    return hashlib.sha256(
        canonical_json(
            {
                "url": _canonical_string(url, "crawl url"),
                "strategy": _canonical_string(strategy, "crawl strategy", lower=True),
                "source_level": _canonical_string(source_level, "source level", lower=True) if source_level is not None else None,
                "status": normalize_crawl_status(status) if status is not None else None,
                "summary_text": _canonical_string(summary_text, "summary text") if summary_text is not None else None,
                "diagnostics": [crawl_diagnostic_signature(item) for item in (diagnostics or [])],
            }
        )
    ).hexdigest()


def browser_session_scope_fingerprint(scope: Mapping[str, Any] | BrowserSessionScope) -> str:
    payload = dict(scope) if isinstance(scope, Mapping) else scope.model_dump(mode="json")
    return hashlib.sha256(
        canonical_json(
            {
                "session_ref": _canonical_string(payload.get("session_ref"), "session_ref"),
                "user_id": _canonical_string(payload.get("user_id"), "user_id"),
                "tenant_id": _canonical_string(payload.get("tenant_id"), "tenant_id"),
                "allowed_origin": _origin_from_url(_canonical_string(payload.get("allowed_origin"), "allowed_origin")),
                "environment": _canonical_string(payload.get("environment"), "environment", lower=True),
                "source": _canonical_string(payload.get("source") or "os_credential", "source", lower=True),
            }
        )
    ).hexdigest()


def browser_session_scope_matches(
    scope: Mapping[str, Any] | BrowserSessionScope,
    *,
    url: str,
    user_id: str,
    tenant_id: str,
    environment: Literal["local", "server"],
    now: datetime | None = None,
) -> bool:
    payload = dict(scope) if isinstance(scope, Mapping) else scope.model_dump(mode="json")
    if str(payload.get("status") or "").strip().lower() != "active":
        return False
    expires_at = payload.get("expires_at")
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            return False
    if isinstance(expires_at, datetime) and expires_at <= (now or utc_now()):
        return False
    try:
        return (
            _origin_from_url(_canonical_string(url, "url"))
            == _origin_from_url(_canonical_string(payload.get("allowed_origin"), "allowed_origin"))
            and _canonical_string(payload.get("user_id"), "user_id") == _canonical_string(user_id, "user_id")
            and _canonical_string(payload.get("tenant_id"), "tenant_id") == _canonical_string(tenant_id, "tenant_id")
            and _canonical_string(payload.get("environment"), "environment", lower=True) == environment
        )
    except ValueError:
        return False


def _origin_from_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("url must have an http(s) origin")
    return f"{parsed.scheme}://{parsed.netloc}"


def _canonical_query(value: str) -> str:
    if not value:
        return ""
    pairs = parse_qsl(value, keep_blank_values=True)
    if not pairs:
        return ""
    return urlencode(sorted(pairs), doseq=True)


def crawler_catalog_fingerprint() -> str:
    catalog = load_crawler_capability_catalog().model_dump(mode="json")
    return hashlib.sha256(canonical_json(catalog)).hexdigest()


__all__ = [
    "CRAWLER_DATASET_ASSET_VERSION",
    "CRAWLER_CAPABILITY_CATALOG_VERSION",
    "CRAWLER_BROWSER_SESSION_RESOLVE_VERSION",
    "CRAWLER_CONTRACT_VERSION",
    "CRAWLER_CRAWL_SITE_TOOL",
    "CRAWLER_REVIEW_INSPECT_PAGE_TOOL",
    "CRAWLER_REVIEW_RENDER_PAGE_TOOL",
    "CRAWLER_REVIEW_SELECT_REGION_TOOL",
    "CRAWLER_REVIEW_FETCH_NEXT_PAGE_TOOL",
    "CRAWLER_REVIEW_EXTRACT_CANDIDATES_TOOL",
    "CRAWLER_REVIEW_EVIDENCE_TOOLS",
    "CRAWLER_TOOL_NAMES",
    "REVIEW_EVIDENCE_CONTRACT_VERSION",
    "CRAWLER_FETCH_PAGE_TOOL",
    "KNOWN_CRAWL_STATUSES",
    "TERMINAL_CRAWL_STATUSES",
    "Coverage",
    "BrowserSessionScope",
    "BrowserSessionResolveRequest",
    "BrowserSessionResolveResult",
    "CrawlArtifactRef",
    "CrawlDiagnostic",
    "InteractionAction",
    "InteractionBudget",
    "InteractionPlan",
    "InteractionTarget",
    "InteractionTrace",
    "INTERACTION_PLAN_VERSION",
    "INTERACTION_DIAGNOSTIC_CODES",
    "canonical_crawl_url",
    "CrawlPageResult",
    "CrawlRequest",
    "CrawlResult",
    "CrawlStatus",
    "CrawlerCapability",
    "CrawlerCapabilityCatalog",
    "CrawlerDataset",
    "CrawlerDatasetRef",
    "CrawlerDatasetQuality",
    "CrawlerDatasetVersion",
    "EvidenceRef",
    "ReviewEvidenceRef",
    "ReviewEvidenceCandidate",
    "ReviewRecordSample",
    "ReviewEvidenceResult",
    "ReviewEvidenceInspectRequest",
    "ReviewEvidenceRenderRequest",
    "ReviewEvidenceSelectRegionRequest",
    "ReviewEvidenceFetchNextPageRequest",
    "ReviewEvidenceExtractCandidatesRequest",
    "ExtractionSchema",
    "ExtractionRecord",
    "build_extraction_schema",
    "browser_session_scope_fingerprint",
    "browser_session_scope_matches",
    "crawl_attempt_fingerprint",
    "crawl_diagnostic_signature",
    "normalize_extraction_profile",
    "normalize_crawl_diagnostic_code",
    "PendingInteraction",
    "ResumeCrawlRequest",
    "SessionCrawlerDatasetLink",
    "crawler_capabilities",
    "crawler_capability",
    "crawler_catalog_fingerprint",
    "is_terminal_crawl_status",
    "load_crawler_capability_catalog",
    "normalize_crawl_status",
]
