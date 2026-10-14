"""Session-level crawler dataset reuse rules.

本模块只做确定性的意图和数据集投影判断。它不访问网络；完整记录由
SessionMessageStore 保管，分析路径只投影有长度上限的证据样本与统计摘要。
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
from typing import Any, Iterable, Mapping

from cowork_contracts.crawler import (
    CrawlerDataset,
    CrawlerDatasetRef,
    CrawlResult,
    Coverage,
    ExtractionRecord,
    canonical_json,
    canonical_crawl_url,
)
from cowork_runtime.review_collection import MAX_REVIEW_COLLECTION_TARGET
from cowork_runtime.dataset_quality import dataset_is_analysis_ready, with_dataset_quality


_EXPORT_MARKERS = ("导出", "下载", "export", "download", "csv", "xlsx", "excel")
_ANALYSIS_MARKERS = (
    "分析", "汇总", "统计", "特点", "主题", "关键词", "分布", "优缺点", "原因", "趋势",
    "占比", "比例", "排行", "排名",
    "analyze", "analysis", "summarize", "summary", "insight", "insights",
)
# 问句信号：请求里有需要语义回答的部分，就不能被无模型的确定性快路径整体消费。
# 高召回设计——漏检一个问句会把用户的问题静默丢弃（run_7544e71 的教训），误检
# 只会多花一次受限分类调用，因此宁可误报。
_QUESTION_MARKERS = (
    "？", "?", "吗", "呢", "多少", "几", "哪些", "哪个", "哪种", "谁", "什么",
    "为什么", "怎么", "怎样", "如何", "是否", "能不能", "有没有",
)
# 裸导出命令里允许出现的全部语义成分：动作动词、数据集指代、格式词和客气用语。
# 残留任何本表以外的内容（主题限定、附加要求、问句）都说明这不是一条裸命令，
# 必须交给模型判断——判定失败方向永远是放行，而不是拦截。
_EXPORT_COMMAND_FILLER_WORDS = (
    "导出", "下载", "保存", "export", "download", "save",
    "评论", "评价", "数据", "数据集", "记录", "内容", "信息", "表格", "文件",
    "csv", "xlsx", "excel", "格式",
    "请", "帮我", "帮忙", "麻烦", "一下", "谢谢", "了", "吧",
    "这些", "这个", "那些", "那个", "该", "上述", "当前", "会话", "所有", "全部",
    "已", "已经", "到", "成", "为", "一份", "个",
)
# Desktop 会把本地工作区/附件描述附在 Turn 正文末尾，供模型和工具使用。
# 这部分是传输上下文，不是用户意图；所有会话资产意图判断都必须先剥离它。
_DESKTOP_CONTEXT_SEPARATOR = "\n\nDesktop local context"
_REFRESH_MARKERS = (
    "重新爬",
    "重新抓",
    "重爬",
    "刷新",
    "最新数据",
    "重新获取",
    # “继续抓取剩余评论”不是对已完成证据的重复读取。即使旧数据集的
    # 状态标记存在异常，也必须让这类明确的续采集请求进入 Browser Worker。
    "继续抓取",
    "继续爬取",
    "抓取剩余",
    "爬取剩余",
    "剩余评论",
    "补全评论",
)

# 仅在当前 Session 已经有一份不完整的评论数据集时使用。这样“抓取100条”
# 不会被误解为任意网页抓取，也不会把“要100条评论”的查看诉求变成网络操作。
_REVIEW_CONTINUATION_TARGET = re.compile(
    r"(?:继续\s*)?(?:抓取|爬取|采集|获取)\s*(?:到|至)?\s*(?P<count>\d{1,4})\s*条?\s*(?:评论|评价|reviews?)?"
    r"|(?:补齐|补全)\s*(?:到|至)?\s*(?P<fill_count>\d{1,4})\s*条?\s*(?:评论|评价|reviews?)?",
    re.IGNORECASE,
)

# “剩余评论”是对当前 Session 数据集的续采请求，而不是新页面抓取。
# 这里必须同时要求采集动词和剩余范围，避免“分析剩余评论”误触发网络操作。
_REVIEW_CONTINUATION_REQUEST = re.compile(
    r"(?:继续\s*)?(?:抓取|爬取|采集|获取)\s*(?:尚未|未|剩下|剩余)?\s*"
    r"(?:的\s*)?(?:评论|评价|reviews?)"
    r"|(?:补齐|补全)\s*(?:尚未|未|剩下|剩余)?\s*(?:的\s*)?"
    r"(?:评论|评价|reviews?)",
    re.IGNORECASE,
)


def user_request_text(text: str) -> str:
    """Return only the user-authored portion of a Desktop turn.

    Desktop transport metadata can contain arbitrary path names such as
    ``Downloads``.  Treating that suffix as natural language caused an
    analysis request to match the ``download`` export marker.  Keeping this
    normalization in the shared intent module makes every caller use the same
    boundary while preserving the full turn for model/tool context assembly.
    """

    raw = str(text or "")
    return raw.split(_DESKTOP_CONTEXT_SEPARATOR, 1)[0].strip()


def is_export_request(text: str) -> bool:
    lowered = user_request_text(text).lower()
    # English markers are token-bounded.  A path such as ``/tmp/downloads``
    # is transport metadata (and is stripped above), while prose like
    # ``download csv`` remains an explicit export request.
    return any(
        marker in lowered
        if any("\u4e00" <= char <= "\u9fff" for char in marker)
        else re.search(rf"(?<![a-z0-9]){re.escape(marker)}(?![a-z0-9])", lowered)
        for marker in _EXPORT_MARKERS
    )


def request_carries_question(text: str) -> bool:
    """Return whether the authored request asks for an answer, not just an action.

    这是导出快路径的反向护栏：命中问句信号意味着请求中有需要模型回答的
    部分。``user_request_text`` 先剥离 Desktop 传输上下文，避免路径名里的
    英文词干扰判定。
    """

    authored = user_request_text(text)
    return any(marker in authored for marker in _QUESTION_MARKERS)


def is_export_only_request(text: str) -> bool:
    """Return true only for an authored, export-only request.

    A mixed request such as “分析评论并导出 CSV” must not be consumed by
    the no-model export fast path.  The asset classifier/workflow handles that
    request so analysis remains the primary user-visible result.
    问句形态（“大概有多少评论？帮我导出”）同样是混合请求：问题部分需要
    语义回答，仅命中导出词不足以证明整条请求都是导出动作。
    """

    authored = user_request_text(text).lower()
    if not is_export_request(authored):
        return False
    if request_carries_question(authored):
        return False
    return not any(marker in authored for marker in _ANALYSIS_MARKERS)


def is_bare_export_command(text: str) -> bool:
    """Return true only for a provable bare export command.

    这是唯一允许在模型路由之前消费请求的导出形态：除导出动作、数据集
    指代、格式词和客气用语外没有任何其他语义成分。任何残留内容（主题
    限定如“提到靠背的”、附加要求如“发给同事”、问句）都判 False，
    交给 ``SessionAssetRouter`` 语义判断。
    """

    if not is_export_only_request(text):
        return False
    if is_explicit_crawler_refresh(text):
        return False
    residue = user_request_text(text).lower()
    stripped = True
    while stripped:
        stripped = False
        for word in _EXPORT_COMMAND_FILLER_WORDS:
            if word in residue:
                residue = residue.replace(word, "")
                stripped = True
    return not re.sub(r"[\s,，。.!！~～、/\\\-]+", "", residue)


def is_explicit_crawler_refresh(text: str) -> bool:
    lowered = user_request_text(text).lower()
    # “不要重新抓取/无需刷新”是对现有 session 数据的消费约束，不是刷新
    # 指令。否则分析追问会被误判为刷新请求，绕过本地数据集路由并重新进入模型。
    if re.search(r"(?:不要|无需|不必|不用)\s*(?:再)?\s*(?:重新抓|重爬|刷新|重新获取|继续抓取|继续爬取)", lowered):
        return False
    return any(marker in lowered for marker in _REFRESH_MARKERS)


def review_continuation_target(text: str) -> int | None:
    """Parse an explicit numeric continuation target without guessing a subject.

    The caller must additionally prove that the active session owns a partial
    reviews dataset.  Keeping that proof outside this string parser prevents a
    generic phrase such as ``抓取100条`` from becoming an unsolicited crawl.
    """

    lowered = user_request_text(text).lower()
    if not lowered or re.search(r"(?:不要|无需|不必|不用)\s*(?:再)?\s*(?:抓取|爬取|采集|获取|补齐|补全)", lowered):
        return None
    match = _REVIEW_CONTINUATION_TARGET.search(lowered)
    if match is None:
        return None
    raw = match.group("count") or match.group("fill_count")
    try:
        target = int(raw)
    except (TypeError, ValueError):
        return None
    return min(target, MAX_REVIEW_COLLECTION_TARGET) if target >= 1 else None


def review_continuation_requested(text: str) -> bool:
    """识别不带数字的“剩余评论”续采请求。

    只有明确的采集动词和评论范围同时出现才返回 true；否定句继续保持
    本地数据消费语义，不会发起新的网络请求。
    """

    lowered = user_request_text(text).lower()
    if not lowered or re.search(
        r"(?:不要|无需|不必|不用|别)\s*(?:再\s*)?(?:继续\s*)?"
        r"(?:抓取|爬取|采集|获取|补齐|补全)",
        lowered,
    ):
        return False
    return _REVIEW_CONTINUATION_REQUEST.search(lowered) is not None


def review_continuation_target_for_dataset(
    text: str,
    *,
    expected_count: int | None,
    record_count: int,
) -> int | None:
    """Resolve a bounded continuation target from explicit text or coverage.

    无数字续采只允许使用 Dataset 声明的 expected_count，且必须确实仍有
    未覆盖记录；未知总量不猜测、不扩大采集范围。
    """

    explicit = review_continuation_target(text)
    if explicit is not None:
        return explicit
    if not review_continuation_requested(text):
        return None
    if expected_count is None or expected_count <= record_count:
        return None
    return min(int(expected_count), MAX_REVIEW_COLLECTION_TARGET)


def active_dataset(
    datasets: Iterable[CrawlerDataset],
    *,
    profile: str | None = None,
    require_analysis_ready: bool = False,
) -> CrawlerDataset | None:
    now = datetime.now(timezone.utc)
    candidates = []
    for dataset in datasets:
        if not dataset.records or dataset.ref.status not in {"completed", "partial"}:
            continue
        if dataset.ref.expires_at is not None and dataset.ref.expires_at <= now:
            continue
        if profile and dataset.ref.profile and dataset.ref.profile != profile:
            continue
        if require_analysis_ready and not dataset_is_analysis_ready(dataset):
            continue
        candidates.append(dataset)
    return max(candidates, key=lambda item: item.ref.created_at, default=None)


def resolve_session_dataset(
    datasets: Iterable[CrawlerDataset],
    reference: str,
) -> CrawlerDataset | None:
    """Resolve one session-owned dataset from a stable or source-job reference.

    A crawler result exposes ``crawl_job_id`` before its records are promoted
    into the session asset store. The store assigns the durable
    ``crawler_dataset_<crawl_job_id>`` ID. Both name the same asset, but the
    lookup remains exact and only considers datasets already scoped to the
    current session.
    """

    normalized = str(reference or "").strip()
    if not normalized:
        return None
    for dataset in datasets:
        if dataset.ref.dataset_id == normalized:
            return dataset
    for dataset in datasets:
        metadata = dataset.ref.metadata if isinstance(dataset.ref.metadata, Mapping) else {}
        if metadata.get("crawl_job_id") == normalized:
            return dataset
    return None


def dataset_from_crawl_result(
    *,
    run_id: str,
    source_url: str | None,
    result: object,
) -> CrawlerDataset | None:
    """将成功抓取的结构化记录投影成 Session 可复用数据集。"""

    if not isinstance(result, dict):
        return None
    # ToolHost 在大输出时附加 owner-scoped output_ref。该句柄不是
    # CrawlerResult 的公开契约字段，但不会改变内联的结构化抽取证据。
    # 仅剥离这个 transport envelope，再执行严格业务契约校验。
    candidate = dict(result)
    candidate.pop("output_ref", None)
    try:
        crawl = CrawlResult.model_validate(candidate)
    except (TypeError, ValueError):
        return None
    if crawl.status not in {"completed", "partial"} or not crawl.extraction_records:
        return None
    url = source_url or next(
        (ref.url for record in crawl.extraction_records for ref in record.evidence_refs if ref.url),
        None,
    )
    if not url:
        return None
    try:
        canonical_url = canonical_crawl_url(url)
    except ValueError:
        return None
    if crawl.extraction_records[0].profile == "reviews":
        # All review Dataset paths share the same Runtime admission checks.
        # A successful tool response is not enough: records must be tied to the
        # requested product page and carry acceptable field/provenance evidence.
        page = next(
            (
                item for item in crawl.pages
                if _same_product_page(item.final_url or item.url, canonical_url)
            ),
            None,
        )
        try:
            # Import lazily: the crawler package registers dataset-export
            # tools, which depend on this module. Keeping validation out of
            # module import time prevents analysis -> crawler_reuse -> crawler
            # package -> dataset_export from forming a cycle.
            from cowork_runtime.crawler.review_validation import (
                ReviewValidationError,
                validate_review_page,
                validate_review_records,
            )

            if page is not None:
                review_page = page.model_copy(
                    update={
                        "extraction_records": list(crawl.extraction_records),
                        "extraction_coverage": crawl.extraction_coverage,
                        "diagnostics": list(crawl.diagnostics),
                    },
                    deep=True,
                )
                records, rejected = validate_review_page(review_page, canonical_url)
            else:
                # Pre-versioned tool results may not retain page snapshots.
                # They are accepted only when every row carries its own
                # same-product page evidence.
                records, rejected = validate_review_records(
                    list(crawl.extraction_records),
                    canonical_url,
                    diagnostics=crawl.diagnostics,
                )
        except ReviewValidationError:
            return None
        if not records:
            return None
        crawl = crawl.model_copy(
            update={
                "extraction_records": records,
                "extraction_coverage": _coverage_for_valid_review_records(
                    crawl.extraction_coverage,
                    len(records),
                ),
                "metadata": {
                    **crawl.metadata,
                    "review_dataset_admission": {
                        "validated": True,
                        "snapshotless_legacy_result": page is None,
                        "accepted_count": len(records),
                        "rejected_record_counts": rejected,
                    },
                },
            },
            deep=True,
        )
    dataset_id = f"crawler_dataset_{crawl.crawl_job_id}"
    dataset_version_id = f"dataset_version_{crawl.crawl_job_id}"
    coverage = crawl.extraction_coverage
    review_collection = crawl.metadata.get("review_collection") if isinstance(crawl.metadata, Mapping) else None
    lineage_metadata: dict[str, Any] = {"tool_name": crawl.tool_name, "crawl_job_id": crawl.crawl_job_id}
    admission = crawl.metadata.get("review_dataset_admission") if isinstance(crawl.metadata, Mapping) else None
    if isinstance(admission, Mapping):
        lineage_metadata["review_dataset_admission"] = dict(admission)
    if isinstance(review_collection, Mapping):
        # 仅保存 Provider 形成的聚合状态，不向资产 metadata 复制正文、URL
        # 或浏览器诊断，后续追问只依赖该谱系决定是否需要继续采集。
        lineage_metadata["review_collection"] = dict(review_collection)
    return with_dataset_quality(CrawlerDataset(
        ref=CrawlerDatasetRef(
            dataset_id=dataset_id,
            dataset_version_id=dataset_version_id,
            source_run_id=run_id,
            source_url=canonical_url,
            profile=(crawl.extraction_records[0].profile if crawl.extraction_records else None),
            record_count=len(crawl.extraction_records),
            coverage=coverage,
            status="completed" if coverage is not None and coverage.ratio == 1 else "partial",
            metadata=lineage_metadata,
        ),
        records=list(crawl.extraction_records),
    ))


def _same_product_page(left: str | None, right: str) -> bool:
    if not isinstance(left, str) or not left:
        return False
    try:
        return _product_page_identity(left) == _product_page_identity(right)
    except ValueError:
        return False


def _product_page_identity(value: str) -> str:
    from urllib.parse import urlsplit

    parsed = urlsplit(canonical_crawl_url(value))
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _coverage_for_valid_review_records(coverage: Coverage | None, count: int) -> Coverage:
    expected = coverage.expected_count if coverage is not None else None
    if expected is not None and expected < count:
        expected = count
    return Coverage(
        observed_count=count,
        expected_count=expected,
        ratio=round(count / expected, 6) if expected else (None if count else 0.0),
        label=f"{count}/{expected}" if expected is not None else ("unknown" if count else "empty"),
        confidence=coverage.confidence if coverage is not None else None,
        details={
            "runtime_review_dataset_admission": True,
            "source_observed_count": coverage.observed_count if coverage is not None else count,
            "source_expected_count": expected,
        },
    )


def merge_session_crawler_dataset(
    existing: CrawlerDataset | None,
    incoming: CrawlerDataset,
) -> CrawlerDataset:
    """合并同源评论续采，确保 Session 资产而非 Run 持有累计事实。

    Browser Worker 通常会从第一页重放到目标数量，部分站点却只返回下一页。
    因而不能直接用本轮结果覆盖旧资产；按公开评论标识或稳定字段指纹去重后，
    两种站点行为都能得到一致的累计数据集。
    """

    if existing is None or not _can_merge_session_dataset(existing, incoming):
        return incoming
    records = _merge_dataset_records(existing.records, incoming.records)
    expected = _merged_expected_count(existing.ref.coverage, incoming.ref.coverage, len(records))
    coverage = Coverage(
        observed_count=len(records),
        expected_count=expected,
        ratio=round(len(records) / expected, 6) if expected else (None if records else 0.0),
        label=f"{len(records)}/{expected}" if expected is not None else ("unknown" if records else "empty"),
        details={
            "session_asset_merged": True,
            "prior_record_count": existing.ref.record_count,
            "incoming_record_count": incoming.ref.record_count,
        },
    )
    metadata = dict(existing.ref.metadata)
    metadata.update(dict(incoming.ref.metadata))
    _merge_review_collection_metadata(metadata, coverage)
    return with_dataset_quality(CrawlerDataset(
        ref=existing.ref.model_copy(
            update={
                "record_count": len(records),
                "coverage": coverage,
                "status": "completed" if expected is not None and len(records) >= expected else "partial",
                "metadata": metadata,
                # Incoming evidence creates a new immutable collection state;
                # the current Session pin moves to it while the prior state is
                # retained by the DatasetVersion store.
                "dataset_version_id": incoming.ref.dataset_version_id,
                "parent_dataset_version_id": existing.ref.dataset_version_id,
            },
            deep=True,
        ),
        records=records,
    ))


def _can_merge_session_dataset(existing: CrawlerDataset, incoming: CrawlerDataset) -> bool:
    return (
        existing.ref.profile == incoming.ref.profile
        and existing.ref.profile == "reviews"
        and existing.ref.source_url == incoming.ref.source_url
    )


def _merge_dataset_records(
    existing: Iterable[ExtractionRecord],
    incoming: Iterable[ExtractionRecord],
) -> list[ExtractionRecord]:
    merged: list[ExtractionRecord] = []
    seen: set[str] = set()
    for record in [*existing, *incoming]:
        fingerprint = _dataset_record_fingerprint(record)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        merged.append(record.model_copy(update={"index": len(merged)}, deep=True))
    return merged


def _dataset_record_fingerprint(record: ExtractionRecord) -> str:
    review_id = str(record.metadata.get("review_id") or "").strip()
    identity = {"review_id": review_id} if review_id else {
        "author": record.fields.get("author"),
        "content": record.fields.get("content"),
        "published_at": record.fields.get("published_at"),
        "rating": record.fields.get("rating"),
    }
    return hashlib.sha256(canonical_json(identity)).hexdigest()


def _merged_expected_count(
    prior: Coverage | None,
    current: Coverage | None,
    observed: int,
) -> int | None:
    expected = [item.expected_count for item in (prior, current) if item is not None and item.expected_count is not None]
    return max([observed, *[int(item) for item in expected]]) if expected else None


def _merge_review_collection_metadata(metadata: dict[str, Any], coverage: Coverage) -> None:
    collection = metadata.get("review_collection")
    if not isinstance(collection, Mapping):
        return
    normalized = dict(collection)
    target = int(normalized.get("target_count") or 0)
    observed = coverage.observed_count
    total = coverage.expected_count
    fully_covered = total is not None and observed >= total
    target_reached = fully_covered or (observed >= target if target > 0 else False)
    normalized.update({
        "observed_count": observed,
        "declared_total": total,
        "target_reached": target_reached,
        "coverage_state": (
            "complete" if fully_covered else "target_reached_partial" if target_reached else "target_unreached"
        ),
        "continuation": {
            "recommended": not fully_covered,
            "reason": (
                None if fully_covered else "declared_total_not_reached" if target_reached and total is not None
                else "site_total_unknown" if target_reached else "collection_target_not_reached"
            ),
        },
    })
    metadata["review_collection"] = normalized


def dataset_context(
    datasets: Iterable[CrawlerDataset],
    *,
    profile: str | None = None,
    dataset_id: str | None = None,
    purpose: str = "export",
) -> dict[str, Any] | None:
    dataset = next((item for item in datasets if item.ref.dataset_id == dataset_id), None) if dataset_id else None
    dataset = dataset or active_dataset(
        datasets,
        profile=profile,
        require_analysis_ready=purpose == "analysis",
    )
    if dataset is None:
        return None
    if purpose == "analysis" and not dataset_is_analysis_ready(dataset):
        quality = dataset.ref.quality
        reason_codes = quality.reason_codes if quality is not None else ["dataset.quality_unavailable"]
        return {
            "kind": "crawler_dataset",
            "dataset": dataset.ref.model_dump(mode="json"),
            "instruction": (
                "RUNTIME_SESSION_DATASET_QUALITY_GATE: The selected dataset is export-only. "
                "Do not analyze, summarize, infer, or cite its records. Explain that verified fields are not "
                "reliable enough for analysis and offer only export or an explicit continuation/refresh. "
                f"QUALITY_REASON_CODES={canonical_json(reason_codes)}"
            ),
        }
    ref = dataset.ref.model_dump(mode="json")
    if purpose == "analysis":
        summary = {
            "dataset": ref,
            "records": [_bounded_dataset_record(record) for record in dataset.records[:25]],
            "truncated": len(dataset.records) > 25,
        }
        return {
            "kind": "crawler_dataset",
            "dataset": ref,
            "instruction": (
                "RUNTIME_SESSION_DATASET: Answer the user's request from the selected session-owned review dataset. "
                "Do not access the source page or call crawler tools. The bounded structured sample below is evidence, "
                "not a claim that unlisted records do not exist.\n"
                f"DATASET_SUMMARY={canonical_json(summary)}"
            ),
        }
    return {
        "kind": "crawler_dataset",
        "dataset": ref,
        "instruction": (
            "已有可复用的结构化爬虫数据集。当前请求是导出/下载，请直接基于该数据集生成文件；"
            "不要再次调用 cowork.web.fetch_page/cowork.web.crawl_site，也不要访问网络 API。"
        ),
    }


def _bounded_dataset_record(record: ExtractionRecord) -> dict[str, Any]:
    """Expose a bounded evidence sample for model-led dataset analysis."""

    fields = record.fields if isinstance(record.fields, Mapping) else {}
    return {
        "record_id": record.record_id,
        "index": record.index,
        "fields": {
            str(key): (str(value)[:320] if isinstance(value, str) else value)
            for key, value in fields.items()
            if isinstance(value, (str, int, float, bool)) or value is None
        },
    }


def is_same_source(call_url: str | None, dataset: CrawlerDataset) -> bool:
    if not call_url:
        return True
    try:
        return canonical_crawl_url(call_url) == dataset.ref.source_url
    except ValueError:
        return False


def has_complete_coverage(dataset: CrawlerDataset) -> bool:
    """Return whether a dataset can safely replace another network fetch.

    ``active_dataset`` deliberately accepts partial data so users can export
    or inspect records that were already collected. Network de-duplication is
    stricter: without an expected count that has actually been reached, a
    later request may be asking to continue the same collection.
    """

    coverage = dataset.ref.coverage
    return bool(
        dataset.ref.status == "completed"
        and coverage is not None
        and coverage.expected_count is not None
        and coverage.observed_count >= coverage.expected_count
    )


def crawler_dataset_matches_call_profile(
    call_arguments: Mapping[str, Any],
    dataset: CrawlerDataset,
) -> bool:
    """Require a same-Run reuse to preserve the requested extraction target.

    A complete reviews dataset should suppress another reviews fetch, but must
    not turn a later request for product specifications on the same URL into a
    false cache hit. A profile-less repeat can only reuse a profile-less
    dataset; the AgentLoop normally enriches crawler calls before reaching
    this check.
    """

    raw_schema = call_arguments.get("extraction_schema")
    call_profile = raw_schema.get("profile") if isinstance(raw_schema, Mapping) else None
    # 同一 Run 的无 profile 重复调用通常由模型省略了可选 schema 造成。
    # 对已有 profile 数据集而言，不能把这种不完整调用当成“任意 profile
    # 均可复用”，否则同页商品规格请求可能错误继承评论结果。
    if not isinstance(call_profile, str) or not call_profile.strip():
        return dataset.ref.profile is None
    dataset_profile = dataset.ref.profile
    return dataset_profile is None or dataset_profile == call_profile.strip().lower()


__all__ = [
    "active_dataset",
    "crawler_dataset_matches_call_profile",
    "dataset_context",
    "dataset_from_crawl_result",
    "has_complete_coverage",
    "is_export_request",
    "is_export_only_request",
    "is_bare_export_command",
    "is_explicit_crawler_refresh",
    "request_carries_question",
    "user_request_text",
    "review_continuation_target",
    "review_continuation_requested",
    "review_continuation_target_for_dataset",
    "is_same_source",
    "merge_session_crawler_dataset",
    "resolve_session_dataset",
]
