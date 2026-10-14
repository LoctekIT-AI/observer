"""Versioned local asset references used by Desktop follow-up turns.

The reference intentionally contains no filesystem URI.  A provider can name
an ``asset_id`` and version, but only the Local Runtime can resolve it back to
an authorized local handle.  This prevents a quoted path from becoming an
implicit workspace grant.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ASSET_REF_CONTRACT_VERSION = "cowork.asset-ref.v1"
ASSET_RESOLUTION_CONTRACT_VERSION = "cowork.asset-resolution.v1"

AssetKind = Literal["file", "artifact", "dataset", "attachment", "tool_result"]
AssetState = Literal["ready", "superseded", "missing", "corrupt", "revoked"]
AssetCapability = Literal["read", "analyze", "export", "download"]
AssetResolutionStatus = Literal[
    "resolved",
    "ambiguous",
    "missing",
    "corrupt",
    "unauthorized",
    "superseded",
]


class _AssetModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AssetProvenance(_AssetModel):
    """Auditable creation lineage without exposing the underlying locator."""

    source_kind: AssetKind
    source_run_id: str | None = Field(default=None, min_length=1, max_length=160)
    parent_asset_id: str | None = Field(default=None, min_length=1, max_length=160)
    parent_asset_version_id: str | None = Field(default=None, min_length=1, max_length=200)


class AssetRef(_AssetModel):
    """A bounded, immutable local asset version selected by Runtime."""

    schema_version: Literal["cowork.asset-ref.v1"] = ASSET_REF_CONTRACT_VERSION
    asset_id: str = Field(min_length=1, max_length=160)
    asset_version_id: str = Field(min_length=1, max_length=200)
    version: int = Field(ge=1, le=1_000_000)
    kind: AssetKind
    title: str = Field(min_length=1, max_length=512)
    content_type: str | None = Field(default=None, max_length=256)
    size_bytes: int | None = Field(default=None, ge=0)
    sha256: str = Field(pattern="^[0-9a-f]{64}$")
    provenance: AssetProvenance
    capabilities: list[AssetCapability] = Field(default_factory=list, max_length=8)
    state: AssetState = "ready"
    summary: str = Field(default="", max_length=1_200)
    created_at: datetime

    @field_validator("asset_id", "asset_version_id", "title", "content_type", "summary")
    @classmethod
    def _canonical_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized and value != "":
            raise ValueError("asset text fields must not be blank")
        return normalized

    @field_validator("sha256")
    @classmethod
    def _canonical_hash(cls, value: str) -> str:
        return value.lower()

    @field_validator("capabilities")
    @classmethod
    def _unique_capabilities(cls, value: list[AssetCapability]) -> list[AssetCapability]:
        return list(dict.fromkeys(value))

    @field_validator("created_at")
    @classmethod
    def _timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("asset timestamp must include timezone")
        return value.astimezone(timezone.utc)


class AssetResolution(_AssetModel):
    """Typed continuation result; callers must not replace failures with guesses."""

    schema_version: Literal["cowork.asset-resolution.v1"] = ASSET_RESOLUTION_CONTRACT_VERSION
    status: AssetResolutionStatus
    reason_code: str = Field(min_length=1, max_length=128)
    asset: AssetRef | None = None
    candidates: list[AssetRef] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def _match_outcome_shape(self) -> "AssetResolution":
        if self.status == "resolved" and self.asset is None:
            raise ValueError("resolved asset outcome requires asset")
        if self.status == "ambiguous" and len(self.candidates) < 2:
            raise ValueError("ambiguous asset outcome requires multiple candidates")
        if self.status != "ambiguous" and self.candidates:
            raise ValueError("only ambiguous asset outcome may contain candidates")
        if self.status != "resolved" and self.asset is not None:
            raise ValueError("non-resolved asset outcome must not contain asset")
        return self


def parse_asset_ref(payload: object) -> AssetRef:
    return AssetRef.model_validate(payload)


def parse_asset_resolution(payload: object) -> AssetResolution:
    return AssetResolution.model_validate(payload)


__all__ = [
    "ASSET_REF_CONTRACT_VERSION",
    "ASSET_RESOLUTION_CONTRACT_VERSION",
    "AssetCapability",
    "AssetKind",
    "AssetProvenance",
    "AssetRef",
    "AssetResolution",
    "AssetResolutionStatus",
    "AssetState",
    "parse_asset_ref",
    "parse_asset_resolution",
]
