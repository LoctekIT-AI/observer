"""Session lineage persistence ports and in-memory implementation."""

from __future__ import annotations

from copy import deepcopy
from threading import RLock
from typing import Any, Dict, List, Optional, Protocol

from cowork_contracts import AssetRef as CatalogAssetRef
from cowork_contracts.crawler import CrawlerDataset, CrawlerDatasetVersion, SessionCrawlerDatasetLink
from cowork_runtime.contracts import ArtifactRef, CompactionRecord, PinnedConstraint, SessionMessage
from cowork_runtime.dataset_quality import with_dataset_quality
from cowork_runtime.persistence.asset_catalog import asset_ref_from_artifact, asset_ref_from_dataset


class SessionMessageStore(Protocol):
    def append_message(self, message: SessionMessage) -> SessionMessage:
        ...

    def list_messages(self, session_id: str, *, limit: Optional[int] = None) -> List[SessionMessage]:
        ...

    def append_artifact_ref(self, session_id: str, artifact: ArtifactRef) -> ArtifactRef:
        ...

    def list_artifact_refs(self, session_id: str, *, limit: Optional[int] = None) -> List[ArtifactRef]:
        ...

    def register_asset(
        self,
        session_id: str,
        asset: CatalogAssetRef,
        *,
        source: dict[str, Any],
    ) -> CatalogAssetRef:
        ...

    def list_asset_refs(self, session_id: str, *, limit: Optional[int] = None) -> List[CatalogAssetRef]:
        ...

    def get_asset_ref(
        self,
        session_id: str,
        asset_id: str,
        *,
        asset_version_id: str | None = None,
    ) -> CatalogAssetRef | None:
        ...

    def save_crawler_dataset(self, session_id: str, dataset: CrawlerDataset) -> CrawlerDataset:
        ...

    def list_crawler_datasets(self, session_id: str, *, limit: Optional[int] = None) -> List[CrawlerDataset]:
        ...

    def get_crawler_dataset(self, session_id: str, dataset_id: str) -> Optional[CrawlerDataset]:
        ...

    def list_crawler_dataset_versions(self, session_id: str, dataset_id: str) -> List[CrawlerDatasetVersion]:
        ...

    def get_crawler_dataset_link(self, session_id: str, dataset_id: str) -> Optional[SessionCrawlerDatasetLink]:
        ...

    def list_pending_crawler_dataset_sync(self, *, limit: int = 20) -> List[tuple[CrawlerDatasetVersion, SessionCrawlerDatasetLink]]:
        ...

    def mark_crawler_dataset_synced(self, dataset_version_id: str) -> None:
        ...

    def save_pinned_constraint(self, constraint: PinnedConstraint) -> PinnedConstraint:
        ...

    def list_pinned_constraints(self, session_id: str) -> List[PinnedConstraint]:
        ...

    def save_compaction(self, record: CompactionRecord) -> CompactionRecord:
        ...

    def list_compactions(self, session_id: str) -> List[CompactionRecord]:
        ...


class InMemorySessionMessageStore:
    def __init__(self) -> None:
        self._messages: Dict[str, SessionMessage] = {}
        self._artifacts: Dict[tuple[str, str], ArtifactRef] = {}
        self._asset_versions: Dict[tuple[str, str], CatalogAssetRef] = {}
        self._asset_links: Dict[tuple[str, str], str] = {}
        self._asset_sources: Dict[tuple[str, str], dict[str, Any]] = {}
        self._crawler_datasets: Dict[tuple[str, str], CrawlerDataset] = {}
        self._crawler_dataset_versions: Dict[tuple[str, str], CrawlerDatasetVersion] = {}
        self._crawler_dataset_links: Dict[tuple[str, str], SessionCrawlerDatasetLink] = {}
        self._constraints: Dict[str, PinnedConstraint] = {}
        self._compactions: Dict[str, CompactionRecord] = {}
        self._lock = RLock()

    def append_message(self, message: SessionMessage) -> SessionMessage:
        with self._lock:
            existing = self._messages.get(message.message_id)
            if existing is not None:
                return existing.model_copy(deep=True)
            self._messages[message.message_id] = message.model_copy(deep=True)
            return message.model_copy(deep=True)

    def list_messages(self, session_id: str, *, limit: Optional[int] = None) -> List[SessionMessage]:
        with self._lock:
            values = [message for message in self._messages.values() if message.session_id == session_id]
            values.sort(key=lambda message: (message.created_at, message.message_id))
            if limit is not None:
                values = values[-max(1, min(limit, 1000)):]
            return [message.model_copy(deep=True) for message in values]

    def append_artifact_ref(self, session_id: str, artifact: ArtifactRef) -> ArtifactRef:
        with self._lock:
            key = (session_id, artifact.artifact_id)
            existing = self._artifacts.get(key)
            if existing is not None:
                return existing.model_copy(deep=True)
            self._artifacts[key] = artifact.model_copy(deep=True)
            asset, source = asset_ref_from_artifact(artifact)
            self.register_asset(session_id, asset, source=source)
            return artifact.model_copy(deep=True)

    def list_artifact_refs(self, session_id: str, *, limit: Optional[int] = None) -> List[ArtifactRef]:
        with self._lock:
            values = list(self._artifacts.items())
            values = [artifact for (owner, _), artifact in values if owner == session_id]
            values.sort(key=lambda artifact: (artifact.created_at, artifact.artifact_id))
            if limit is not None:
                values = values[-max(1, min(limit, 1000)):]
            return [artifact.model_copy(deep=True) for artifact in values]

    def register_asset(
        self,
        session_id: str,
        asset: CatalogAssetRef,
        *,
        source: dict[str, Any],
    ) -> CatalogAssetRef:
        """Persist immutable asset versions and move only the Session current pin."""

        with self._lock:
            version_key = (session_id, asset.asset_version_id)
            existing = self._asset_versions.get(version_key)
            if existing is not None:
                self._asset_links[(session_id, asset.asset_id)] = existing.asset_version_id
                return existing.model_copy(deep=True)
            current_id = self._asset_links.get((session_id, asset.asset_id))
            current = self._asset_versions.get((session_id, current_id)) if current_id else None
            persisted = asset.model_copy(
                update={"version": (current.version + 1) if current is not None else 1},
                deep=True,
            )
            self._asset_versions[version_key] = persisted
            self._asset_sources[version_key] = deepcopy(source)
            self._asset_links[(session_id, asset.asset_id)] = persisted.asset_version_id
            return persisted.model_copy(deep=True)

    def list_asset_refs(self, session_id: str, *, limit: Optional[int] = None) -> List[CatalogAssetRef]:
        with self._lock:
            values = [
                self._asset_versions[(owner, version_id)]
                for (owner, _asset_id), version_id in self._asset_links.items()
                if owner == session_id and (owner, version_id) in self._asset_versions
            ]
            values.sort(key=lambda asset: (asset.created_at, asset.asset_id, asset.asset_version_id))
            if limit is not None:
                values = values[-max(1, min(limit, 1000)):]
            return [asset.model_copy(deep=True) for asset in values]

    def get_asset_ref(
        self,
        session_id: str,
        asset_id: str,
        *,
        asset_version_id: str | None = None,
    ) -> CatalogAssetRef | None:
        with self._lock:
            version_id = asset_version_id or self._asset_links.get((session_id, asset_id))
            asset = self._asset_versions.get((session_id, version_id)) if version_id else None
            if asset is None or asset.asset_id != asset_id:
                return None
            return asset.model_copy(deep=True)

    def get_asset_source(
        self,
        session_id: str,
        asset_id: str,
        *,
        asset_version_id: str | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            version_id = asset_version_id or self._asset_links.get((session_id, asset_id))
            source = self._asset_sources.get((session_id, version_id)) if version_id else None
            return deepcopy(source) if source is not None else None

    def save_crawler_dataset(self, session_id: str, dataset: CrawlerDataset) -> CrawlerDataset:
        with self._lock:
            if dataset.ref.quality is None:
                dataset = with_dataset_quality(dataset)
            key = (session_id, dataset.ref.dataset_id)
            existing = self._crawler_dataset_links.get(key)
            version_id = dataset.ref.dataset_version_id
            if not version_id:
                version_id = f"dataset_version_{dataset.ref.source_run_id}_{dataset.ref.dataset_id}"
                dataset = dataset.model_copy(
                    update={
                        "ref": dataset.ref.model_copy(
                            update={
                                "dataset_version_id": version_id,
                                "parent_dataset_version_id": existing.dataset_version_id if existing else None,
                            },
                            deep=True,
                        )
                    },
                    deep=True,
                )
            self._crawler_datasets[key] = dataset.model_copy(deep=True)
            if version_id:
                self._crawler_dataset_versions.setdefault((session_id, version_id), CrawlerDatasetVersion(
                    dataset_version_id=version_id,
                    dataset_id=dataset.ref.dataset_id,
                    parent_dataset_version_id=dataset.ref.parent_dataset_version_id,
                    source_run_id=dataset.ref.source_run_id,
                    dataset=dataset,
                ))
                self._crawler_dataset_links[key] = SessionCrawlerDatasetLink(
                    session_id=session_id,
                    dataset_id=dataset.ref.dataset_id,
                    dataset_version_id=version_id,
                )
            asset, source = asset_ref_from_dataset(dataset)
            self.register_asset(session_id, asset, source=source)
            return self._crawler_datasets[key].model_copy(deep=True)

    def list_crawler_datasets(self, session_id: str, *, limit: Optional[int] = None) -> List[CrawlerDataset]:
        with self._lock:
            values = [dataset for (owner, _), dataset in self._crawler_datasets.items() if owner == session_id]
            values.sort(key=lambda dataset: (dataset.ref.created_at, dataset.ref.dataset_id))
            if limit is not None:
                values = values[-max(1, min(limit, 1000)):]
            return [dataset.model_copy(deep=True) for dataset in values]

    def get_crawler_dataset(self, session_id: str, dataset_id: str) -> Optional[CrawlerDataset]:
        with self._lock:
            dataset = self._crawler_datasets.get((session_id, dataset_id))
            return dataset.model_copy(deep=True) if dataset is not None else None

    def list_crawler_dataset_versions(self, session_id: str, dataset_id: str) -> List[CrawlerDatasetVersion]:
        with self._lock:
            if (session_id, dataset_id) not in self._crawler_dataset_links:
                return []
            values = [
                item for (owner, _), item in self._crawler_dataset_versions.items()
                if owner == session_id and item.dataset_id == dataset_id
            ]
            values.sort(key=lambda item: (item.created_at, item.dataset_version_id))
            return [item.model_copy(deep=True) for item in values]

    def get_crawler_dataset_link(self, session_id: str, dataset_id: str) -> Optional[SessionCrawlerDatasetLink]:
        with self._lock:
            link = self._crawler_dataset_links.get((session_id, dataset_id))
            return link.model_copy(deep=True) if link is not None else None

    def list_pending_crawler_dataset_sync(self, *, limit: int = 20) -> List[tuple[CrawlerDatasetVersion, SessionCrawlerDatasetLink]]:
        del limit
        return []

    def mark_crawler_dataset_synced(self, dataset_version_id: str) -> None:
        del dataset_version_id

    def save_pinned_constraint(self, constraint: PinnedConstraint) -> PinnedConstraint:
        with self._lock:
            self._constraints[constraint.constraint_id] = constraint.model_copy(deep=True)
            return constraint.model_copy(deep=True)

    def list_pinned_constraints(self, session_id: str) -> List[PinnedConstraint]:
        with self._lock:
            values = [item for item in self._constraints.values() if item.session_id == session_id and item.active]
            values.sort(key=lambda item: (item.created_at, item.constraint_id))
            return [item.model_copy(deep=True) for item in values]

    def save_compaction(self, record: CompactionRecord) -> CompactionRecord:
        with self._lock:
            self._compactions[record.compaction_id] = record.model_copy(deep=True)
            return record.model_copy(deep=True)

    def list_compactions(self, session_id: str) -> List[CompactionRecord]:
        with self._lock:
            values = [item for item in self._compactions.values() if item.session_id == session_id]
            values.sort(key=lambda item: (item.created_at, item.compaction_id))
            return [item.model_copy(deep=True) for item in values]
