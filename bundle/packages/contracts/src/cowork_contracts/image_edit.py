"""Versioned contract for Cowork's first-image editing workflow.

The browser sends only an artifact id, an editing instruction, and normalized
selection geometry.  Provider credentials, storage locations, model names, and
encoded image data stay on the server side of the contract boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    field_validator,
    model_validator,
)


IMAGE_EDIT_CONTRACT_VERSION = "cowork.image_edit.v1"


class _StrictImageEditModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NormalizedPoint(_StrictImageEditModel):
    """A point expressed as a fraction of the source image dimensions."""

    x: StrictFloat = Field(ge=0, le=1)
    y: StrictFloat = Field(ge=0, le=1)


class BrushStroke(_StrictImageEditModel):
    """One freehand stroke in normalized image coordinates."""

    kind: Literal["brush"]
    points: list[NormalizedPoint] = Field(min_length=2, max_length=2048)
    radius: StrictFloat = Field(gt=0, le=0.5)


class NormalizedRectangle(_StrictImageEditModel):
    """A positive rectangle fully contained inside the source image."""

    kind: Literal["rectangle"]
    x: StrictFloat = Field(ge=0, le=1)
    y: StrictFloat = Field(ge=0, le=1)
    width: StrictFloat = Field(gt=0, le=1)
    height: StrictFloat = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _fits_inside_image(self) -> "NormalizedRectangle":
        if self.x + self.width > 1:
            raise ValueError("rectangle x plus width must be at most 1")
        if self.y + self.height > 1:
            raise ValueError("rectangle y plus height must be at most 1")
        return self


class ImageRegionSelection(_StrictImageEditModel):
    """The normalized brush and rectangle geometry for a region edit."""

    strokes: list[BrushStroke] = Field(default_factory=list, max_length=64)
    rectangles: list[NormalizedRectangle] = Field(default_factory=list, max_length=32)


class ImageEditRequest(_StrictImageEditModel):
    """User-controlled input for whole-image and region-image edits."""

    schema_version: Literal[IMAGE_EDIT_CONTRACT_VERSION]
    source_artifact_id: UUID
    prompt: str = Field(min_length=1, max_length=4000)
    scope: Literal["whole", "region"]
    selection: ImageRegionSelection | None = None
    title: str | None = Field(default=None, min_length=1, max_length=256)

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

    @model_validator(mode="after")
    def _selection_matches_scope(self) -> "ImageEditRequest":
        if self.scope == "whole" and self.selection is not None:
            raise ValueError("whole edit must not include selection")
        if self.scope == "region" and self.selection is None:
            raise ValueError("region edit requires a non-empty selection")
        if self.scope == "region" and self.selection is not None:
            if not self.selection.strokes and not self.selection.rectangles:
                raise ValueError("region edit requires a non-empty selection")
        return self


def validate_image_edit_semantics(payload: Mapping[str, Any]) -> None:
    """Validate invariants that Draft 2020-12 cannot express arithmetically.

    The JSON Schema performs structural/type validation.  This executable
    validator is the shared semantic boundary for consumers that validate raw
    JSON before constructing :class:`ImageEditRequest`; in particular it
    enforces that every rectangle remains inside the normalized image.
    """

    selection = payload.get("selection")
    if not isinstance(selection, Mapping):
        return

    rectangles = selection.get("rectangles", [])
    if not isinstance(rectangles, list):
        return

    for rectangle in rectangles:
        if not isinstance(rectangle, Mapping):
            continue
        x = rectangle.get("x")
        y = rectangle.get("y")
        width = rectangle.get("width")
        height = rectangle.get("height")
        if not all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in (x, y, width, height)
        ):
            continue
        if x + width > 1:
            raise ValueError("rectangle x plus width must be at most 1")
        if y + height > 1:
            raise ValueError("rectangle y plus height must be at most 1")


__all__ = [
    "IMAGE_EDIT_CONTRACT_VERSION",
    "BrushStroke",
    "ImageEditRequest",
    "ImageRegionSelection",
    "NormalizedPoint",
    "NormalizedRectangle",
    "validate_image_edit_semantics",
]
