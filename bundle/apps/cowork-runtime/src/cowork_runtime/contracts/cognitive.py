"""Structured, evidence-linked cognitive records for Runtime traces.

The contract deliberately stores auditable summaries and digests only.  It is
not a container for model chain-of-thought, prompts, tool stdout, or file
contents.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import Field, field_validator, model_validator

from .base import ContractModel

COGNITIVE_RECORD_SCHEMA_VERSION = "cowork.cognitive.record.v1"
CognitiveRecordType = Literal[
    "goal_compiled",
    "plan_proposed",
    "decision",
    "hypothesis",
    "observation",
    "belief_update",
    "effect_expected",
    "effect_observed",
    "replan",
    "verdict",
]
CognitiveEvidenceKind = Literal["event", "span", "artifact", "context"]
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_ATTRIBUTE_KEYS = {
    "chain_of_thought",
    "cot",
    "hidden_reasoning",
    "internal_reasoning",
    "prompt",
    "system_prompt",
    "messages",
    "stdout",
    "stderr",
    "content",
    "content_base64",
    "file_contents",
}


class CognitiveEvidenceRef(ContractModel):
    """A same-run reference to an auditable Runtime fact."""

    kind: CognitiveEvidenceKind
    ref_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    digest: Optional[str] = None
    relation: Literal["supports", "contradicts", "verifies", "derived_from"] = "supports"

    @field_validator("digest")
    @classmethod
    def _valid_digest(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not _DIGEST_RE.fullmatch(value):
            raise ValueError("evidence digest must be a lowercase sha256 hex digest")
        return value


class CognitiveRecord(ContractModel):
    """A schema-aware cognitive summary with explicit evidence status."""

    schema_version: Literal["cowork.cognitive.record.v1"] = COGNITIVE_RECORD_SCHEMA_VERSION
    record_type: CognitiveRecordType
    record_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    span_id: Optional[str] = Field(default=None, min_length=1)
    record_key: str = Field(min_length=1)
    reason_code: Optional[str] = Field(default=None, min_length=1, max_length=80)
    confidence: Optional[float] = Field(default=None, ge=0, le=1)
    evidence_refs: List[CognitiveEvidenceRef] = Field(default_factory=list)
    unsupported: bool = False
    summary_digest: Optional[str] = None
    choice: Optional[str] = Field(default=None, min_length=1)
    alternatives: List[str] = Field(default_factory=list)
    statement_digest: Optional[str] = None
    fact_digest: Optional[str] = None
    source_ref: Optional[str] = Field(default=None, min_length=1)
    action_ref: Optional[str] = Field(default=None, min_length=1)
    expected_delta: Optional[str] = None
    actual_delta: Optional[str] = None
    criteria_results: Dict[str, Any] = Field(default_factory=dict)
    verifier_version: Optional[str] = Field(default=None, min_length=1)
    attributes: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("summary_digest", "statement_digest", "fact_digest")
    @classmethod
    def _digest_fields_are_hashes(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not _DIGEST_RE.fullmatch(value):
            raise ValueError("cognitive digest must be a lowercase sha256 hex digest")
        return value

    @model_validator(mode="after")
    def _validate_evidence_and_no_hidden_reasoning(self) -> "CognitiveRecord":
        for evidence in self.evidence_refs:
            if evidence.run_id != self.run_id:
                raise ValueError("cognitive evidence must belong to the same run")
        if self.record_type in {"decision", "verdict"} and not self.evidence_refs and not self.unsupported:
            raise ValueError("decision and verdict without evidence must be marked unsupported")
        if self.record_type == "decision" and not self.choice:
            raise ValueError("decision records require choice")
        if self.record_type == "verdict" and not self.criteria_results:
            raise ValueError("verdict records require criteria_results")
        _assert_safe_attributes(self.attributes)
        return self


def digest_value(value: Any) -> str:
    """Create a deterministic digest without retaining the source value."""

    if isinstance(value, bytes):
        raw = value
    else:
        raw = str(value).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _assert_safe_attributes(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).lower() in _FORBIDDEN_ATTRIBUTE_KEYS:
                raise ValueError(f"cognitive attributes must not contain {key}")
            _assert_safe_attributes(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_safe_attributes(nested)


__all__ = [
    "COGNITIVE_RECORD_SCHEMA_VERSION",
    "CognitiveEvidenceKind",
    "CognitiveEvidenceRef",
    "CognitiveRecord",
    "CognitiveRecordType",
    "digest_value",
]
