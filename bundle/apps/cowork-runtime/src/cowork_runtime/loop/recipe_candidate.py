"""Safe extraction of a Recipe Candidate from a successful crawler Run.

Candidates are proposals for later replay/canary certification.  They never
carry credentials, raw responses, selectors or model reasoning and are not
treated as executable policy until a separate registry certifies them.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Mapping


@dataclass(frozen=True)
class RecipeCandidate:
    version: str
    source_run_id: str
    origin: str
    profile: str
    actions: tuple[str, ...]
    capability_versions: tuple[str, ...]
    quality: Mapping[str, Any]
    status: str = "candidate"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["actions"] = list(self.actions)
        value["capability_versions"] = list(self.capability_versions)
        value["quality"] = dict(self.quality)
        return value


def build_recipe_candidate(checkpoint_metadata: Mapping[str, Any]) -> RecipeCandidate | None:
    """Build a candidate only from a completed, evidence-backed crawler run."""

    dataset = checkpoint_metadata.get("crawler_dataset")
    progress = checkpoint_metadata.get("crawler_progress")
    if not isinstance(dataset, Mapping) or not isinstance(progress, Mapping):
        return None
    source_run_id = str(dataset.get("source_run_id") or "")
    origin = _origin(str(dataset.get("source_url") or ""))
    if not source_run_id or not origin or str(dataset.get("status") or "") not in {"completed", "partial"}:
        return None
    actions = _actions(checkpoint_metadata.get("execution_trace"))
    if not actions:
        return None
    quality = {
        "record_count": int(dataset.get("record_count") or 0),
        "coverage": float(progress.get("coverage") or 0.0),
        "progress_fingerprint": str(progress.get("fingerprint") or ""),
    }
    return RecipeCandidate(
        version="crawler-recipe.v1",
        source_run_id=source_run_id,
        origin=origin,
        profile=str(dataset.get("profile") or "reviews"),
        actions=tuple(actions),
        capability_versions=tuple(_capability_versions(checkpoint_metadata.get("execution_trace"))),
        quality=quality,
    )


def _actions(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    allowed = {"cowork.web.review.inspect_page", "cowork.web.review.render_page", "cowork.web.review.select_region", "cowork.web.review.fetch_next_page", "cowork.web.review.extract_candidates"}
    actions: list[str] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        payload = item.get("payload")
        name = payload.get("tool_name") if isinstance(payload, Mapping) else None
        if isinstance(name, str) and name in allowed and name not in actions:
            actions.append(name)
    return actions


def _capability_versions(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    versions: list[str] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        payload = item.get("payload")
        if not isinstance(payload, Mapping):
            continue
        value = payload.get("contract_version")
        if isinstance(value, str) and value and value not in versions:
            versions.append(value[:128])
    return versions


def _origin(url: str) -> str | None:
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    return parsed.hostname.lower()
