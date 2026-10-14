"""Artifact reference contract models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Literal, Optional

from pydantic import Field

from .base import ContractModel, utc_now
from .enums import ArtifactAccessKind
from .artifact_intent import ArtifactManifest


class ArtifactAccess(ContractModel):
    kind: ArtifactAccessKind
    uri: Optional[str] = None
    local_path: Optional[str] = None
    expires_at: Optional[datetime] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ArtifactRef(ContractModel):
    artifact_id: str
    run_id: str
    title: str
    artifact_type: str
    access: ArtifactAccess
    content_type: Optional[str] = None
    size_bytes: Optional[int] = Field(default=None, ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    manifest: Optional[ArtifactManifest] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ArtifactMetadata(ContractModel):
    """Durable metadata kept next to (not inside) an artifact body."""

    artifact_id: str
    run_id: str
    session_id: Optional[str] = None
    owner_id: Optional[str] = None
    title: str
    artifact_type: str
    content_type: Optional[str] = None
    size_bytes: int = Field(default=0, ge=0)
    sha256: str
    state: Literal["active", "expired", "deleted", "corrupt"] = "active"
    expires_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    permissions: list[str] = Field(default_factory=list)
    manifest: Optional[ArtifactManifest] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
