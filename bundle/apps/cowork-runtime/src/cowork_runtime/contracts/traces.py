"""Versioned diagnostic trace contracts derived from Runtime facts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import Field, model_validator

from .base import ContractModel
from .causal import CausalDiagnostic, CausalEdge, CausalNodeRef
from .cognitive import CognitiveRecord

RUNTIME_TRACE_V2_SCHEMA_VERSION = "cowork.runtime.trace.v2"

TraceSpanKind = Literal[
    "run",
    "turn",
    "plan",
    "step",
    "attempt",
    "revision",
    "child_run",
    "transport",
    "context",
    "model",
    "policy",
    "tool",
    "approval",
    "verification",
    "artifact",
]
TraceSpanStatus = Literal[
    "running",
    "waiting",
    "ok",
    "error",
    "cancelled",
    "blocked",
]


class TraceLink(ContractModel):
    """Connect one projected span to the ledger event that supports it."""

    trace_id: str = Field(min_length=1)
    span_id: str = Field(min_length=1)
    event_id: Optional[str] = Field(default=None, min_length=1)
    event_seq: Optional[int] = Field(default=None, ge=1)
    target_span_id: Optional[str] = Field(default=None, min_length=1)
    relationship: Literal["derived_from", "depends_on", "revises", "spawned_by"] = "derived_from"

    @model_validator(mode="after")
    def _validate_target(self) -> "TraceLink":
        if self.relationship == "derived_from":
            if self.event_id is None or self.event_seq is None or self.target_span_id is not None:
                raise ValueError("derived_from link requires one event target")
        elif self.target_span_id is None or self.event_id is not None or self.event_seq is not None:
            raise ValueError("span relationship link requires one target_span_id")
        return self


class RuntimeSpan(ContractModel):
    """One node in the direct Runtime trace tree."""

    trace_id: str = Field(min_length=1)
    span_id: str = Field(min_length=1)
    parent_span_id: Optional[str] = None
    run_id: str = Field(min_length=1)
    turn_id: Optional[str] = None
    step_id: Optional[str] = None
    kind: TraceSpanKind
    name: str = Field(min_length=1)
    started_at: datetime
    ended_at: Optional[datetime] = None
    duration_ms: Optional[float] = Field(default=None, ge=0)
    status: TraceSpanStatus
    error: Optional[Dict[str, Any]] = None
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    attributes: Dict[str, Any] = Field(default_factory=dict)
    links: List[TraceLink] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_interval_and_links(self) -> "RuntimeSpan":
        if self.ended_at is not None and self.ended_at < self.started_at:
            raise ValueError("span ended_at must not precede started_at")
        if self.ended_at is None and self.duration_ms is not None:
            raise ValueError("open span must not declare duration_ms")
        if self.ended_at is not None:
            expected = max(0.0, (self.ended_at - self.started_at).total_seconds() * 1000)
            if self.duration_ms is None or abs(self.duration_ms - expected) > 0.001:
                raise ValueError("span duration_ms must match its timestamps")
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("span total_tokens must equal input_tokens + output_tokens")
        if any(link.trace_id != self.trace_id or link.span_id != self.span_id for link in self.links):
            raise ValueError("span links must reference their owning trace and span")
        return self


class RuntimeTraceV2(ContractModel):
    """A validated tree projection for one direct Runtime Run."""

    schema_version: Literal["cowork.runtime.trace.v2"] = RUNTIME_TRACE_V2_SCHEMA_VERSION
    trace_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    run: Dict[str, Any]
    spans: List[RuntimeSpan] = Field(min_length=1)
    cognitive_records: List[CognitiveRecord] = Field(default_factory=list)
    causal_nodes: List[CausalNodeRef] = Field(default_factory=list)
    causal_edges: List[CausalEdge] = Field(default_factory=list)
    causal_diagnostics: List[CausalDiagnostic] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_tree(self) -> "RuntimeTraceV2":
        by_id = {span.span_id: span for span in self.spans}
        if len(by_id) != len(self.spans):
            raise ValueError("trace span_id values must be unique")
        roots = [span for span in self.spans if span.parent_span_id is None]
        if len(roots) != 1 or roots[0].kind != "run":
            raise ValueError("trace must contain exactly one run root span")
        root_id = roots[0].span_id
        causal_node_ids = {(node.kind, node.ref_id) for node in self.causal_nodes}
        if len(causal_node_ids) != len(self.causal_nodes):
            raise ValueError("causal node references must be unique")
        for node in self.causal_nodes:
            if node.run_id != self.run_id or node.trace_id not in {None, self.trace_id}:
                raise ValueError("causal nodes must belong to the exported trace and run")
        for edge in self.causal_edges:
            if edge.run_id != self.run_id or edge.trace_id != self.trace_id:
                raise ValueError("causal edges must belong to the exported trace and run")
            for node in (edge.from_ref, edge.to_ref):
                if (node.kind, node.ref_id) not in causal_node_ids:
                    raise ValueError("causal edge target node must exist in the trace")
        for span in self.spans:
            if span.trace_id != self.trace_id or span.run_id != self.run_id:
                raise ValueError("all spans must belong to the exported trace and run")
            if span.parent_span_id is not None and span.parent_span_id not in by_id:
                raise ValueError("non-root span parent must exist in the trace")
            for link in span.links:
                if link.target_span_id is not None and link.target_span_id not in by_id:
                    raise ValueError("span relationship link target must exist in the trace")
            if span.parent_span_id is not None:
                parent = by_id[span.parent_span_id]
                if span.started_at < parent.started_at:
                    raise ValueError("child span must not start before its parent")
                if parent.ended_at is not None and span.ended_at is None:
                    raise ValueError("open child span must not outlive a closed parent")
                if (
                    parent.ended_at is not None
                    and span.ended_at is not None
                    and span.ended_at > parent.ended_at
                ):
                    raise ValueError("child span must not end after its parent")
            seen: set[str] = set()
            current = span
            while current.parent_span_id is not None:
                if current.span_id in seen:
                    raise ValueError("trace span parents must be acyclic")
                seen.add(current.span_id)
                current = by_id[current.parent_span_id]
            if current.span_id != root_id:
                raise ValueError("every span must be connected to the root")
        return self
