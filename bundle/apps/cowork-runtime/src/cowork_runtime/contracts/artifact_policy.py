"""Versioned contracts for proposed intent and Runtime-owned artifact policy."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field

from .artifact_intent import ArtifactFormat
from .base import ContractModel

ARTIFACT_INTENT_PROPOSAL_SCHEMA_VERSION = "cowork.runtime.artifact-intent-proposal.v1"
EFFECTIVE_ARTIFACT_POLICY_SCHEMA_VERSION = "cowork.runtime.effective-artifact-policy.v1"


class ArtifactIntentProposal(ContractModel):
    """Model/runtime proposal; never grants write permission by itself."""

    schema_version: Literal["cowork.runtime.artifact-intent-proposal.v1"] = (
        ARTIFACT_INTENT_PROPOSAL_SCHEMA_VERSION
    )
    action: Literal[
        "answer",
        "analyze",
        "export",
        "save_result",
        "revise_artifact",
    ]
    source: Literal[
        "current_answer",
        "current_analysis",
        "input_file",
        "dataset",
        "existing_artifact",
        "unknown",
    ] = "unknown"
    requested_format: Optional[ArtifactFormat] = None
    format_explicit: bool = False
    continuation_of: Optional[str] = None
    evidence: list[str] = Field(default_factory=list, max_length=8)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    needs_clarification: bool = False


class EffectiveArtifactPolicy(ContractModel):
    """Runtime decision compiled from a proposal and trusted session facts."""

    schema_version: Literal["cowork.runtime.effective-artifact-policy.v1"] = (
        EFFECTIVE_ARTIFACT_POLICY_SCHEMA_VERSION
    )
    require_artifact: bool = False
    allowed_formats: list[ArtifactFormat] = Field(default_factory=list)
    forbidden_formats: list[ArtifactFormat] = Field(default_factory=list)
    destination: Literal["authorized_workspace", "none"] = "none"
    write_mode: Literal["none", "create", "new_version"] = "none"
    approval_required: bool = False
    source: str = "unknown"
    reason_code: str = "artifact_not_required"
