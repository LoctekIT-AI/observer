"""Strict local adapter contracts for component observations."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


COMPONENT_EVENT_CONTRACT_VERSION = "component_event.v1"
SKILLHUB_CATALOG_CONTRACT_VERSION = "skillhub.catalog.v1"

RuntimeName = Literal["dsh", "codex", "claude", "workbuddy", "unknown"]
ComponentKind = Literal["skill", "mcp_server", "mcp_tool", "tool", "unknown"]
ComponentStage = Literal["started", "completed", "failed", "timeout", "cancelled"]
EvidenceLevel = Literal["verified", "partial", "inferred", "unknown"]
BindingStatus = Literal["paired", "unpaired"]


def _canonical_identifier(value: str) -> str:
    if value != value.strip():
        raise ValueError("identifiers must not contain surrounding whitespace")
    return value


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return value.astimezone(timezone.utc)


class _StrictContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SkillHubCatalogRefV1(_StrictContractModel):
    """Explicit SkillHub source/version identity supplied by trusted metadata."""

    contract_version: Literal[SKILLHUB_CATALOG_CONTRACT_VERSION] = SKILLHUB_CATALOG_CONTRACT_VERSION
    source_id: str = Field(min_length=1, max_length=256)
    version_id: str = Field(min_length=1, max_length=256)
    ref_source: str = Field(min_length=1, max_length=128)

    @field_validator("source_id", "version_id", "ref_source")
    @classmethod
    def ref_identifiers_are_canonical(cls, value: str) -> str:
        return _canonical_identifier(value)


class ComponentEventV1(_StrictContractModel):
    """Minimal, versioned component event shared by local adapters."""

    schema_version: Literal[COMPONENT_EVENT_CONTRACT_VERSION]
    event_id: str = Field(min_length=1, max_length=256)
    invocation_id: str = Field(min_length=1, max_length=256)
    run_id: str = Field(min_length=1, max_length=256)
    runtime: RuntimeName
    plugin_id: str = Field(min_length=1, max_length=256)
    component_kind: ComponentKind
    component_id: str = Field(min_length=1, max_length=512)
    component_version: str | None = Field(default=None, max_length=128)
    catalog_ref: SkillHubCatalogRefV1 | None = None
    stage: ComponentStage
    occurred_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    retry_count: int | None = Field(default=None, ge=0)
    error_code: str | None = Field(default=None, min_length=1, max_length=128)
    observation_kind: str = Field(min_length=1, max_length=128)
    evidence_source: str = Field(min_length=1, max_length=256)
    evidence_level: EvidenceLevel
    parent_invocation_id: str | None = Field(default=None, min_length=1, max_length=256)
    installation_id: str = Field(min_length=1, max_length=256)
    tenant_id: str | None = Field(default=None, min_length=1, max_length=256)
    user_id: str | None = Field(default=None, min_length=1, max_length=256)
    source_type: str = Field(min_length=1, max_length=128)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    user_input: str | None = Field(default=None, max_length=1_000_000)
    model_output: str | None = Field(default=None, max_length=1_000_000)
    tool_result: str | None = Field(default=None, max_length=1_000_000)

    @field_validator(
        "event_id",
        "invocation_id",
        "run_id",
        "plugin_id",
        "component_id",
        "component_version",
        "observation_kind",
        "evidence_source",
        "parent_invocation_id",
        "installation_id",
        "tenant_id",
        "user_id",
        "source_type",
        "error_code",
    )
    @classmethod
    def identifiers_are_canonical(cls, value: str | None) -> str | None:
        return _canonical_identifier(value) if value is not None else None

    @field_validator("occurred_at", "started_at", "ended_at")
    @classmethod
    def timestamps_are_aware_utc(cls, value: datetime | None) -> datetime | None:
        return _utc_datetime(value) if value is not None else None


class PluginManifestV1(_StrictContractModel):
    """Registration metadata for one installed adapter instance."""

    plugin_id: str = Field(min_length=1, max_length=256)
    runtime: RuntimeName
    contract_version: str = Field(min_length=1, max_length=128)
    installation_id: str = Field(min_length=1, max_length=256)
    enabled: bool
    capabilities: list[str] = Field(min_length=1, max_length=64)

    @field_validator("plugin_id", "contract_version", "installation_id")
    @classmethod
    def manifest_identifiers_are_canonical(cls, value: str) -> str:
        return _canonical_identifier(value)

    @field_validator("capabilities")
    @classmethod
    def capabilities_are_canonical(cls, values: list[str]) -> list[str]:
        if any(not value or value != value.strip() for value in values):
            raise ValueError("capabilities must be non-empty canonical identifiers")
        if len(set(values)) != len(values):
            raise ValueError("capabilities must be unique")
        return values


class InstallationBindingV1(_StrictContractModel):
    """Authenticated local identity bound to an adapter installation."""

    installation_id: str = Field(min_length=1, max_length=256)
    user_id: str = Field(min_length=1, max_length=256)
    tenant_id: str = Field(min_length=1, max_length=256)
    status: BindingStatus

    @field_validator("installation_id", "user_id", "tenant_id")
    @classmethod
    def binding_identifiers_are_canonical(cls, value: str) -> str:
        return _canonical_identifier(value)


__all__ = [
    "BindingStatus",
    "COMPONENT_EVENT_CONTRACT_VERSION",
    "ComponentEventV1",
    "ComponentKind",
    "ComponentStage",
    "EvidenceLevel",
    "InstallationBindingV1",
    "PluginManifestV1",
    "SKILLHUB_CATALOG_CONTRACT_VERSION",
    "SkillHubCatalogRefV1",
    "RuntimeName",
]
