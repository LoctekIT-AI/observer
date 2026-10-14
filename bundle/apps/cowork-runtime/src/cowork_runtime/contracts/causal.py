"""Validated, rebuildable causal graph contracts for Runtime trace projections."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import Field, model_validator

from .base import ContractModel
from .cognitive import CognitiveEvidenceRef

CAUSAL_EDGE_SCHEMA_VERSION = "cowork.causal.edge.v1"
CAUSAL_NODE_SCHEMA_VERSION = "cowork.causal.node.v1"
CausalRelation = Literal[
    "supports",
    "contradicts",
    "causes",
    "prevents",
    "verifies",
    "derived_from",
    "replans",
]
CausalNodeKind = Literal["cognitive", "span", "event", "artifact", "effect"]


class CausalNodeRef(ContractModel):
    """A stable pointer to a projected node or an append-only event."""

    schema_version: Literal["cowork.causal.node.v1"] = CAUSAL_NODE_SCHEMA_VERSION
    kind: CausalNodeKind
    ref_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    trace_id: Optional[str] = Field(default=None, min_length=1)
    tenant_id: Optional[str] = Field(default=None, min_length=1)
    span_id: Optional[str] = Field(default=None, min_length=1)
    event_id: Optional[str] = Field(default=None, min_length=1)
    title: Optional[str] = Field(default=None, min_length=1)
    status: Optional[str] = Field(default=None, min_length=1)
    attributes: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_target_identity(self) -> "CausalNodeRef":
        if self.kind == "span" and self.span_id not in {None, self.ref_id}:
            raise ValueError("span node span_id must match ref_id")
        if self.kind == "event" and self.event_id not in {None, self.ref_id}:
            raise ValueError("event node event_id must match ref_id")
        return self


class CausalEdge(ContractModel):
    """An explicit causal assertion, never inferred from timestamp order."""

    schema_version: Literal["cowork.causal.edge.v1"] = CAUSAL_EDGE_SCHEMA_VERSION
    edge_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    from_ref: CausalNodeRef
    to_ref: CausalNodeRef
    relation: CausalRelation
    strength: Optional[float] = Field(default=None, ge=0, le=1)
    evidence_refs: List[CognitiveEvidenceRef] = Field(default_factory=list)
    attributes: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_scope(self) -> "CausalEdge":
        if self.from_ref.run_id != self.run_id or self.to_ref.run_id != self.run_id:
            raise ValueError("causal edge node references must belong to the edge run")
        if self.from_ref.trace_id not in {None, self.trace_id} or self.to_ref.trace_id not in {None, self.trace_id}:
            raise ValueError("causal edge node references must belong to the edge trace")
        if self.from_ref.tenant_id and self.to_ref.tenant_id and self.from_ref.tenant_id != self.to_ref.tenant_id:
            raise ValueError("causal edge node references must belong to the same tenant")
        for evidence in self.evidence_refs:
            if evidence.run_id != self.run_id:
                raise ValueError("causal edge evidence must belong to the same run")
        return self


class CausalDiagnostic(ContractModel):
    """Non-fatal projection diagnostics surfaced to governance tooling."""

    code: str = Field(min_length=1)
    severity: Literal["warning", "error"] = "warning"
    message: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    ref_id: Optional[str] = Field(default=None, min_length=1)
    edge_id: Optional[str] = Field(default=None, min_length=1)
    details: Dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "CAUSAL_EDGE_SCHEMA_VERSION",
    "CAUSAL_NODE_SCHEMA_VERSION",
    "CausalDiagnostic",
    "CausalEdge",
    "CausalNodeKind",
    "CausalNodeRef",
    "CausalRelation",
]
