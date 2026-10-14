"""Versioned contract for reference-image recreation in Cowork."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, field_validator


IMAGE_TO_IMAGE_CONTRACT_VERSION = "cowork.image_to_image.v1"


class ImageToImageRequest(BaseModel):
    """User input for image-to-image; provider details stay server-side."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[IMAGE_TO_IMAGE_CONTRACT_VERSION]
    source_artifact_id: UUID
    prompt: str = Field(min_length=1, max_length=4000)
    title: str | None = Field(default=None, min_length=1, max_length=256)
    reference_strength: StrictFloat | None = Field(default=None, gt=0, le=1)

    @field_validator("prompt")
    @classmethod
    def _prompt_is_non_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("prompt must be non-blank")
        return normalized

    @field_validator("title")
    @classmethod
    def _title_is_non_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("title must be non-blank when provided")
        return normalized


__all__ = ["IMAGE_TO_IMAGE_CONTRACT_VERSION", "ImageToImageRequest"]
