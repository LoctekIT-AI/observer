"""Dataset-level quality gates independent of crawler package initialization."""

from __future__ import annotations

from collections import Counter
from math import ceil
from typing import Iterable

from cowork_contracts.crawler import CrawlerDataset, CrawlerDatasetQuality
from cowork_runtime.contracts.base import utc_now


def evaluate_dataset_quality(dataset: CrawlerDataset) -> CrawlerDatasetQuality:
    """Classify a dataset as analyzable or export-only using verified fields."""

    records = dataset.records
    profile = dataset.ref.profile
    if profile != "reviews":
        return CrawlerDatasetQuality(
            status="analysis_ready" if records else "export_only",
            profile=profile,
            record_count=len(records),
            reason_codes=[] if records else ["dataset.empty"],
        )

    total = len(records)
    contents = [_text_field(record.fields, "content") for record in records]
    authors = [_text_field(record.fields, "author") for record in records]
    ratings = [_valid_rating(record.fields.get("rating")) for record in records]
    content_count = sum(value is not None for value in contents)
    rating_count = sum(value is not None for value in ratings)
    author_count = sum(value is not None for value in authors)
    duplicated_content_count = _duplicate_count(value for value in contents if value)
    author_like_body_count = sum(
        author is not None and (len(author) > 120 or author == content)
        for author, content in zip(authors, contents)
    )
    reasons: list[str] = []
    if total == 0:
        reasons.append("dataset.empty")
    # 纯评分本身就是可分析的评论事实。正文覆盖不足只在评分也不足时阻断
    # 分析，避免 41 条文字 + 21 条纯评分被错误降级为只能导出。
    if content_count < max(1, ceil(total * 0.7)) and rating_count < max(1, ceil(total * 0.5)):
        reasons.append("reviews.content_incomplete")
    if rating_count < max(1, ceil(total * 0.5)):
        reasons.append("reviews.rating_incomplete")
    if total >= 3 and duplicated_content_count / total > 0.5:
        reasons.append("reviews.content_highly_duplicated")
    if total >= 3 and author_like_body_count / total > 0.4:
        reasons.append("reviews.author_content_misaligned")
    return CrawlerDatasetQuality(
        status="analysis_ready" if not reasons else "export_only",
        checked_at=utc_now(),
        profile=profile,
        record_count=total,
        field_completion={
            "content": _ratio(content_count, total),
            "rating": _ratio(rating_count, total),
            "author": _ratio(author_count, total),
        },
        reason_codes=reasons,
        details={
            "content_count": content_count,
            "rating_count": rating_count,
            "author_count": author_count,
            "duplicated_content_count": duplicated_content_count,
            "author_like_body_count": author_like_body_count,
        },
    )


def dataset_is_analysis_ready(dataset: CrawlerDataset) -> bool:
    quality = dataset.ref.quality or evaluate_dataset_quality(dataset)
    return quality.status == "analysis_ready"


def with_dataset_quality(dataset: CrawlerDataset) -> CrawlerDataset:
    quality = evaluate_dataset_quality(dataset)
    ref = dataset.ref.model_copy(update={"quality": quality}, deep=True)
    return dataset.model_copy(update={"ref": ref}, deep=True)


def _text_field(fields: object, name: str) -> str | None:
    value = fields.get(name) if isinstance(fields, dict) else None
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized if len(normalized) >= 2 else None


def _valid_rating(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        rating = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return int(rating) if rating.is_integer() and 1 <= rating <= 5 else None


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _duplicate_count(values: Iterable[str]) -> int:
    counts = Counter(values)
    return sum(count - 1 for count in counts.values() if count > 1)


__all__ = ["dataset_is_analysis_ready", "evaluate_dataset_quality", "with_dataset_quality"]
