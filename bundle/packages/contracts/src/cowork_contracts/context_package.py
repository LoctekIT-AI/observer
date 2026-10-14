"""Explicit, bounded context transfer for independent cloud jobs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .assets import AssetRef


CONTEXT_PACKAGE_CONTRACT_VERSION = "cowork.context-package.v1"


class ContextPackage(BaseModel):
    """A verified hand-off, never an inferred continuation of a Desktop session."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["cowork.context-package.v1"] = CONTEXT_PACKAGE_CONTRACT_VERSION
    package_id: str = Field(min_length=1, max_length=160)
    source_conversation_id: str = Field(min_length=1, max_length=160)
    source_task_id: str = Field(min_length=1, max_length=160)
    source_snapshot_id: str = Field(min_length=1, max_length=160)
    assets: list[AssetRef] = Field(default_factory=list, max_length=32)
    purpose: str = Field(min_length=1, max_length=500)
    created_at: datetime
    expires_at: datetime

    @field_validator("created_at", "expires_at")
    @classmethod
    def _timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("context package timestamps must include timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _valid_transfer(self) -> "ContextPackage":
        if self.expires_at <= self.created_at:
            raise ValueError("context package must expire after it is created")
        if any(asset.state != "ready" for asset in self.assets):
            raise ValueError("context package accepts ready assets only")
        if any("read" not in asset.capabilities for asset in self.assets):
            raise ValueError("context package assets require read capability")
        return self


def parse_context_package(payload: object) -> ContextPackage:
    return ContextPackage.model_validate(payload)


__all__ = ["CONTEXT_PACKAGE_CONTRACT_VERSION", "ContextPackage", "parse_context_package"]
