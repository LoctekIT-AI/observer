"""Versioned contracts for requested and produced artifacts."""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import Field

from .base import ContractModel

ARTIFACT_INTENT_SCHEMA_VERSION = "cowork.runtime.artifact-intent.v1"
ARTIFACT_MANIFEST_SCHEMA_VERSION = "cowork.runtime.artifact-manifest.v1"


class ArtifactFormat(str, Enum):
    DOCX = "docx"
    MARKDOWN = "md"
    PDF = "pdf"
    XLSX = "xlsx"
    PPTX = "pptx"
    HTML = "html"
    CSV = "csv"
    TEXT = "txt"
    PNG = "png"
    JPEG = "jpg"


class ArtifactPurpose(str, Enum):
    REPORT = "report"
    ANALYSIS = "analysis"
    PRESENTATION = "presentation"
    SPREADSHEET = "spreadsheet"
    CALCULATION = "calculation"
    CHAT = "chat"


class ArtifactFormatSource(str, Enum):
    EXPLICIT = "explicit"
    INFERRED = "inferred"
    DEFAULT = "default"
    CONTINUATION = "continuation"


class ArtifactValidationStatus(str, Enum):
    VALIDATED = "validated"
    UNVALIDATED = "unvalidated"
    INVALID = "invalid"


class ArtifactIntent(ContractModel):
    schema_version: Literal["cowork.runtime.artifact-intent.v1"] = ARTIFACT_INTENT_SCHEMA_VERSION
    purpose: ArtifactPurpose
    primary_format: Optional[ArtifactFormat] = None
    acceptable_formats: list[ArtifactFormat] = Field(default_factory=list)
    supplementary_formats: list[ArtifactFormat] = Field(default_factory=list)
    forbidden_formats: list[ArtifactFormat] = Field(default_factory=list)
    format_source: ArtifactFormatSource
    require_artifact: bool = False
    interactive: bool = False


class ArtifactManifest(ContractModel):
    schema_version: Literal["cowork.runtime.artifact-manifest.v1"] = ARTIFACT_MANIFEST_SCHEMA_VERSION
    purpose: ArtifactPurpose
    format: ArtifactFormat
    is_primary: bool = True
    requested_by: ArtifactFormatSource
    validation_status: ArtifactValidationStatus = ArtifactValidationStatus.UNVALIDATED
    interactive: bool = False
    derived_from: list[str] = Field(default_factory=list)
