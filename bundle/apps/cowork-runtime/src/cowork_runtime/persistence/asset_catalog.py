"""Runtime-owned Asset Catalog contracts and legacy projections.

Artifact and Dataset stores predate the common catalog. They remain readable
for migration, while this module gives every continuation one stable
``AssetRef`` vocabulary and keeps source payloads private to Runtime.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit

from cowork_contracts import AssetProvenance, AssetRef, AssetResolution
from cowork_contracts.crawler import CrawlerDataset
from cowork_runtime.contracts import ArtifactRef


_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


class AssetCatalogStore(Protocol):
    """The only persistence API used by asset continuation routing."""

    def register_asset(
        self,
        session_id: str,
        asset: AssetRef,
        *,
        source: dict[str, Any],
    ) -> AssetRef:
        ...

    def list_asset_refs(self, session_id: str, *, limit: int | None = None) -> list[AssetRef]:
        ...

    def get_asset_ref(
        self,
        session_id: str,
        asset_id: str,
        *,
        asset_version_id: str | None = None,
    ) -> AssetRef | None:
        ...


def asset_ref_from_artifact(artifact: ArtifactRef) -> tuple[AssetRef, dict[str, Any]]:
    """Project a legacy Artifact as a catalog version without exposing its URI."""

    source = artifact.model_dump(mode="json")
    digest = _artifact_digest(artifact, source)
    state = str(artifact.metadata.get("state") or "ready")
    normalized_state = state if state in {"ready", "superseded", "missing", "corrupt", "revoked"} else "ready"
    version_id = f"artifact:{artifact.artifact_id}:{digest[:20]}"
    return (
        AssetRef(
            asset_id=artifact.artifact_id,
            asset_version_id=version_id,
            version=1,
            kind="artifact",
            title=artifact.title,
            content_type=artifact.content_type,
            size_bytes=artifact.size_bytes,
            sha256=digest,
            provenance=AssetProvenance(source_kind="artifact", source_run_id=artifact.run_id),
            capabilities=["read", "analyze", "download"],
            state=normalized_state,
            summary=_artifact_summary(artifact),
            created_at=artifact.created_at,
        ),
        {"source_kind": "artifact", "artifact": source},
    )


def asset_ref_from_dataset(dataset: CrawlerDataset) -> tuple[AssetRef, dict[str, Any]]:
    """Project a DatasetVersion without bypassing its existing quality gate."""

    source = dataset.model_dump(mode="json")
    digest = _canonical_digest(source)
    ref = dataset.ref
    version_id = ref.dataset_version_id or f"dataset:{ref.dataset_id}:{digest[:20]}"
    capabilities = ["read", "export"]
    if ref.quality is None or ref.quality.status == "analysis_ready":
        capabilities.append("analyze")
    return (
        AssetRef(
            asset_id=ref.dataset_id,
            asset_version_id=version_id,
            version=1,
            kind="dataset",
            title=f"{ref.profile or 'crawler'} dataset ({ref.record_count} records)",
            content_type="application/vnd.cowork.dataset+json",
            size_bytes=len(_canonical_json(source).encode("utf-8")),
            sha256=digest,
            provenance=AssetProvenance(
                source_kind="dataset",
                source_run_id=ref.source_run_id,
                parent_asset_id=ref.dataset_id if ref.parent_dataset_version_id else None,
                parent_asset_version_id=ref.parent_dataset_version_id,
            ),
            capabilities=capabilities,
            state="ready",
            summary=(
                f"{ref.profile or 'crawler'} dataset; {ref.record_count} records; "
                f"status={ref.status}; quality={ref.quality.status if ref.quality else 'unchecked'}"
            ),
            created_at=ref.created_at,
        ),
        {"source_kind": "dataset", "dataset": source},
    )


def asset_ref_from_attachment(
    *,
    attachment: dict[str, Any],
    source_run_id: str,
) -> tuple[AssetRef, dict[str, Any]] | None:
    """Project one Host-hashed Desktop file selection into the local catalog.

    The URI is intentionally retained only in the private source payload. The
    Runtime receives an already calculated Host hash and never opens a
    Renderer-supplied path while compiling task context.
    """

    uri = attachment.get("uri")
    name = attachment.get("name")
    digest = attachment.get("sha256")
    if (
        not isinstance(uri, str)
        or not isinstance(name, str)
        or not name.strip()
        or not isinstance(digest, str)
        or _SHA256.fullmatch(digest.strip()) is None
    ):
        return None
    parsed = urlsplit(uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"} or not parsed.path:
        return None

    size_bytes = attachment.get("size_bytes")
    content_type = attachment.get("mime_type")
    normalized_digest = digest.strip().lower()
    locator_digest = _canonical_digest({"uri": uri})
    normalized_size = size_bytes if isinstance(size_bytes, int) and not isinstance(size_bytes, bool) and size_bytes >= 0 else None
    normalized_type = content_type.strip() if isinstance(content_type, str) and content_type.strip() else None
    source = {
        "source_kind": "attachment",
        "attachment": {
            "uri": uri,
            "name": name.strip(),
            "size_bytes": normalized_size,
            "mime_type": normalized_type,
            "sha256": normalized_digest,
        },
    }
    identity = f"attachment:{locator_digest[:24]}"
    return (
        AssetRef(
            asset_id=identity,
            asset_version_id=f"{identity}:{normalized_digest[:20]}",
            version=1,
            kind="attachment",
            title=name.strip(),
            content_type=normalized_type,
            size_bytes=normalized_size,
            sha256=normalized_digest,
            provenance=AssetProvenance(source_kind="attachment", source_run_id=source_run_id),
            capabilities=["read", "analyze"],
            summary="Desktop attachment selected by the user; content remains behind the Runtime file grant.",
            created_at=datetime.now(timezone.utc),
        ),
        source,
    )


class AssetCatalogResolver:
    """Resolve follow-up language from Runtime-owned, bounded catalog facts."""

    def resolve(
        self,
        store: AssetCatalogStore,
        *,
        session_id: str,
        request_text: str,
        requested_asset_id: str | None = None,
        requested_asset_version_id: str | None = None,
    ) -> AssetResolution | None:
        explicit_id = _asset_id(requested_asset_id)
        explicit_version = _asset_id(requested_asset_version_id)
        if explicit_id is not None:
            candidate = store.get_asset_ref(
                session_id,
                explicit_id,
                asset_version_id=explicit_version,
            )
            return _resolution_for_candidate(candidate, exact=True)
        if not _looks_like_asset_continuation(request_text):
            return None
        candidates = _candidate_assets(store.list_asset_refs(session_id, limit=32), request_text)
        if not candidates:
            return AssetResolution(status="missing", reason_code="asset.continuation_missing")
        if len(candidates) > 1:
            return AssetResolution(
                status="ambiguous",
                reason_code="asset.continuation_ambiguous",
                candidates=candidates[:16],
            )
        return _resolution_for_candidate(candidates[0], exact=False)


def asset_context_message(asset: AssetRef) -> str:
    """Provider-safe projection. It never includes local paths or source bodies."""

    return (
        "RUNTIME_ACTIVE_ASSET="
        + json.dumps(
            {
                "asset_id": asset.asset_id,
                "asset_version_id": asset.asset_version_id,
                "kind": asset.kind,
                "title": asset.title,
                "content_type": asset.content_type,
                "size_bytes": asset.size_bytes,
                "sha256": asset.sha256,
                "capabilities": asset.capabilities,
                "summary": asset.summary,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\nUse only this exact asset_id/version. Request a bounded Runtime read; do not infer paths or filenames."
    )


def ensure_legacy_asset_catalog(store: object, session_id: str) -> list[AssetRef]:
    """Project old session records once per access, without a bulk migration.

    A pre-v7 database may have Artifact/Dataset rows but no catalog rows. The
    projection is idempotent, so opening it after a restart never mutates the
    legacy source and creates the same version IDs.
    """

    register = getattr(store, "register_asset", None)
    list_assets = getattr(store, "list_asset_refs", None)
    if not callable(register) or not callable(list_assets):
        return []
    list_artifacts = getattr(store, "list_artifact_refs", None)
    if callable(list_artifacts):
        for artifact in list_artifacts(session_id):
            asset, source = asset_ref_from_artifact(artifact)
            register(session_id, asset, source=source)
    list_datasets = getattr(store, "list_crawler_datasets", None)
    if callable(list_datasets):
        for dataset in list_datasets(session_id):
            asset, source = asset_ref_from_dataset(dataset)
            register(session_id, asset, source=source)
    return list_assets(session_id, limit=32)


def _resolution_for_candidate(candidate: AssetRef | None, *, exact: bool) -> AssetResolution:
    if candidate is None:
        return AssetResolution(
            status="missing",
            reason_code="asset.explicit_reference_missing" if exact else "asset.continuation_missing",
        )
    status_by_state = {
        "corrupt": "corrupt",
        "revoked": "unauthorized",
        "superseded": "superseded",
        "missing": "missing",
    }
    status = status_by_state.get(candidate.state)
    if status is not None:
        return AssetResolution(status=status, reason_code=f"asset.state_{candidate.state}")
    return AssetResolution(
        status="resolved",
        reason_code="asset.exact_reference" if exact else "asset.single_candidate",
        asset=candidate,
    )


def _candidate_assets(assets: list[AssetRef], request_text: str) -> list[AssetRef]:
    text = request_text.casefold()
    wanted_dataset = any(token in text for token in ("dataset", "数据集", "抓取结果", "评论数据"))
    wanted_file = any(token in text for token in ("文件", "file", "附件", "表格", "报告"))
    filtered = [asset for asset in assets if (asset.kind == "dataset" if wanted_dataset else True)]
    if wanted_file and not wanted_dataset:
        file_like = [asset for asset in filtered if asset.kind in {"file", "artifact", "attachment", "tool_result"}]
        filtered = file_like or filtered
    title_matches = [asset for asset in filtered if asset.title.casefold() in text]
    return title_matches or filtered


def _looks_like_asset_continuation(value: str) -> bool:
    lowered = value.casefold()
    return any(
        marker in lowered
        for marker in (
            "刚才的文件", "刚刚的文件", "这个文件", "这份文件", "前面的文件", "该文件",
            "刚才的数据", "这份数据", "前面的数据", "这个数据集", "该数据集", "dataset",
            "基于文件", "基于数据", "继续分析", "继续处理",
        )
    )


def _asset_id(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if normalized and len(normalized) <= 200 else None


def _artifact_digest(artifact: ArtifactRef, payload: dict[str, Any]) -> str:
    for candidate in (
        artifact.access.metadata.get("sha256"),
        artifact.metadata.get("sha256"),
        artifact.metadata.get("source_digest"),
    ):
        if isinstance(candidate, str) and _SHA256.fullmatch(candidate.strip()):
            return candidate.strip().lower()
    return _canonical_digest(payload)


def _artifact_summary(artifact: ArtifactRef) -> str:
    detail = [artifact.artifact_type]
    if artifact.content_type:
        detail.append(artifact.content_type)
    if artifact.size_bytes is not None:
        detail.append(f"{artifact.size_bytes} bytes")
    return "; ".join(detail)


def _canonical_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


__all__ = [
    "AssetCatalogResolver",
    "AssetCatalogStore",
    "asset_context_message",
    "ensure_legacy_asset_catalog",
    "asset_ref_from_attachment",
    "asset_ref_from_artifact",
    "asset_ref_from_dataset",
]
