"""Persistence ports and local implementations."""

from .in_memory import (
    InMemoryEventStore,
    InMemoryRuntimeRecordStore,
    InMemoryStateStore,
)
from .crawler_store import CrawlerJobStore, InMemoryCrawlerJobStore
from .ports import CloseableStore, EventStore, RuntimeRecordStore, StateStore
from .checkpoint_codec import checkpoint_from_dict, checkpoint_to_dict
from .asset_catalog import (
    AssetCatalogResolver,
    AssetCatalogStore,
    asset_context_message,
    asset_ref_from_attachment,
    ensure_legacy_asset_catalog,
)
from .session_store import InMemorySessionMessageStore, SessionMessageStore
from .task_context_store import InMemoryTaskContextStore, TaskContextStore
from .delegation_inbox import (
    DelegationChildIdentity,
    DelegationContinuationRequest,
    DelegationInbox,
    DelegationInboxConflict,
    DelegationTask,
    InMemoryDelegationInbox,
)
from .sqlite_store import (
    DEFAULT_DATABASE_NAME,
    SQLiteRuntimeStore,
    TelemetryOutboxItem,
    TelemetryOutboxStatus,
    TelemetryOutboxCapacityError,
    TelemetryOutboxLimits,
)

__all__ = [
    "CloseableStore",
    "checkpoint_from_dict",
    "checkpoint_to_dict",
    "AssetCatalogResolver",
    "AssetCatalogStore",
    "asset_context_message",
    "asset_ref_from_attachment",
    "ensure_legacy_asset_catalog",
    "DEFAULT_DATABASE_NAME",
    "CrawlerJobStore",
    "EventStore",
    "InMemoryEventStore",
    "InMemoryCrawlerJobStore",
    "InMemoryRuntimeRecordStore",
    "InMemoryStateStore",
    "RuntimeRecordStore",
    "SQLiteRuntimeStore",
    "TelemetryOutboxItem",
    "TelemetryOutboxStatus",
    "TelemetryOutboxCapacityError",
    "TelemetryOutboxLimits",
    "StateStore",
    "SessionMessageStore",
    "InMemorySessionMessageStore",
    "TaskContextStore",
    "InMemoryTaskContextStore",
    "DelegationChildIdentity",
    "DelegationContinuationRequest",
    "DelegationInbox",
    "DelegationInboxConflict",
    "DelegationTask",
    "InMemoryDelegationInbox",
]
