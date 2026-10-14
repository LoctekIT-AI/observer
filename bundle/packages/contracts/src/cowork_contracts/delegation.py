"""Strict, data-only contracts for same-host Subagent delegation.

The contracts deliberately carry references and bounded summaries instead of
transcripts, tool output, workspace paths, or prompt history. Persistence and
join semantics stay in the Runtime; this module is shared by Local and Server
adapters so they cannot silently invent different wire shapes.
"""

from __future__ import annotations

import re
import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


DELEGATION_CONTRACT_VERSION = "cowork.delegation.v1"
DELEGATION_RECEIPT_CONTRACT_VERSION = "cowork.delegation-receipt.v1"
DELEGATION_AGGREGATE_CONTRACT_VERSION = "cowork.delegation-aggregate.v1"
DELEGATION_VERIFICATION_CONTRACT_VERSION = "cowork.delegation-verification.v1"
DELEGATION_VERIFICATION_AGGREGATE_CONTRACT_VERSION = "cowork.delegation-verification-aggregate.v1"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_REFERENCE = re.compile(
    r"^(?:artifact|evidence|trace_digest|file_digest):[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$"
)
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,119}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_DISCLOSURE = re.compile(
    r"(?:\b(?:bearer|api[_ -]?key|password|secret|authorization)\b\s*[:=]|\bsk[-_][A-Za-z0-9_-]{12,}|```|traceback|\bstderr\b)",
    re.IGNORECASE,
)


class _StrictDelegationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class DelegationTaskStatus(str, Enum):
    """Durable child-attempt lifecycle, separate from Parent delivery state."""

    PLANNED = "planned"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    RECEIPT_READY = "receipt_ready"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class DelegationJoinKind(str, Enum):
    ALL_REQUIRED = "all_required"
    ANY_VALID = "any_valid"
    QUORUM = "quorum"
    DEADLINE = "deadline"


class DelegationAggregateStatus(str, Enum):
    WAITING = "waiting"
    READY_FOR_VERIFICATION = "ready_for_verification"
    DEADLINE_REACHED = "deadline_reached"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class DelegationVerificationVerdict(str, Enum):
    """Whether a receipt is admissible as a Parent fact, not whether it succeeded."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"


class DelegationVerificationAggregateStatus(str, Enum):
    """The only safe hand-off states exposed to the Parent continuation."""

    READY_FOR_PARENT = "ready_for_parent"
    WAITING_FOR_PARENT = "waiting_for_parent"


class DelegationJoinPolicy(_StrictDelegationModel):
    """Fixed join semantics. Receipt verification remains an ISSUE-065 concern."""

    kind: DelegationJoinKind = DelegationJoinKind.ALL_REQUIRED
    quorum: int | None = Field(default=None, ge=1, le=3)

    @model_validator(mode="after")
    def _quorum_matches_kind(self) -> "DelegationJoinPolicy":
        if self.kind is DelegationJoinKind.QUORUM and self.quorum is None:
            raise ValueError("quorum join policy requires quorum")
        if self.kind is not DelegationJoinKind.QUORUM and self.quorum is not None:
            raise ValueError("only quorum join policy may include quorum")
        return self


class DelegationAggregate(_StrictDelegationModel):
    """Parent-safe replay snapshot; never carries child text, logs, or evidence."""

    schema_version: Literal["cowork.delegation-aggregate.v1"] = DELEGATION_AGGREGATE_CONTRACT_VERSION
    parent_run_id: str = Field(min_length=1, max_length=256)
    join_group: str = Field(min_length=1, max_length=128)
    revision: int = Field(default=0, ge=0, le=10_000)
    status: DelegationAggregateStatus
    dispatched_count: int = Field(ge=0, le=3)
    receipt_count: int = Field(ge=0, le=3)
    awaiting_verification_count: int = Field(ge=0, le=3)
    required_count: int = Field(ge=0, le=3)
    deadline_at: datetime
    reason_code: str = Field(min_length=1, max_length=120)
    generated_at: datetime

    @field_validator("parent_run_id", "join_group")
    @classmethod
    def _aggregate_ids_are_canonical(cls, value: str) -> str:
        return _bounded_identifier(value, "aggregate identifier")

    @field_validator("reason_code")
    @classmethod
    def _aggregate_reason_is_stable(cls, value: str) -> str:
        normalized = value.strip()
        if normalized != value or not _REASON_CODE.fullmatch(normalized):
            raise ValueError("reason_code must be a stable lowercase code")
        return normalized

    @field_validator("deadline_at", "generated_at")
    @classmethod
    def _aggregate_times_are_timezone_aware(cls, value: datetime) -> datetime:
        return _timezone_datetime(value)

    @model_validator(mode="after")
    def _aggregate_counts_are_ordered(self) -> "DelegationAggregate":
        if self.receipt_count > self.dispatched_count:
            raise ValueError("receipt_count cannot exceed dispatched_count")
        if self.awaiting_verification_count > self.receipt_count:
            raise ValueError("awaiting_verification_count cannot exceed receipt_count")
        if self.required_count > self.dispatched_count:
            raise ValueError("required_count cannot exceed dispatched_count")
        return self


def _bounded_identifier(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip()
    if normalized != value or not _IDENTIFIER.fullmatch(normalized):
        raise ValueError(f"{field} must be a canonical identifier")
    return normalized


def _bounded_reference(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip()
    if normalized != value or not _REFERENCE.fullmatch(normalized):
        raise ValueError(f"{field} must be a controlled evidence reference")
    return normalized


def _bounded_text(value: object, field: str, *, max_length: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip()
    if normalized != value or (not allow_empty and not normalized):
        raise ValueError(f"{field} must be non-empty and canonical")
    if len(normalized) > max_length or "\n" in normalized or "\r" in normalized:
        raise ValueError(f"{field} exceeds its single-line bound")
    if _DISCLOSURE.search(normalized):
        raise ValueError(f"{field} must not contain secrets or raw diagnostics")
    return normalized


def _timezone_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return value.astimezone(timezone.utc)


def _sha256(value: object, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if allow_empty and value == "":
        return value
    if not _SHA256.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase sha256 digest")
    return value


class DelegationWorkspaceScope(_StrictDelegationModel):
    """A trusted grant id narrowed to safe relative paths only."""

    grant_id: str = Field(min_length=1, max_length=256)
    relative_paths: list[str] = Field(min_length=1, max_length=32)

    @field_validator("grant_id")
    @classmethod
    def _grant_id_is_canonical(cls, value: str) -> str:
        return _bounded_identifier(value, "grant_id")

    @field_validator("relative_paths")
    @classmethod
    def _paths_are_relative_and_unique(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError("workspace scope paths must be non-empty strings")
            candidate = value.replace("\\", "/")
            parts = candidate.split("/")
            if candidate.startswith("/") or any(part in {"", ".", ".."} for part in parts):
                raise ValueError("workspace scope paths must be canonical relative paths")
            if any(_DISCLOSURE.search(part) for part in parts):
                raise ValueError("workspace scope paths must not contain sensitive text")
            normalized.append(candidate)
        if len(set(normalized)) != len(normalized):
            raise ValueError("workspace scope paths must be unique")
        return normalized


class DelegationBudget(_StrictDelegationModel):
    max_model_turns: int = Field(ge=1, le=32)
    max_tool_calls: int = Field(ge=0, le=128)
    deadline_at: datetime

    @field_validator("deadline_at")
    @classmethod
    def _deadline_is_timezone_aware(cls, value: datetime) -> datetime:
        return _timezone_datetime(value)


class DelegationUsage(_StrictDelegationModel):
    model_turns: int = Field(ge=0, le=32)
    tool_calls: int = Field(ge=0, le=128)
    duration_ms: int = Field(ge=0, le=86_400_000)


class DelegationCriterionResult(_StrictDelegationModel):
    criterion_id: str = Field(min_length=1, max_length=256)
    status: Literal["passed", "failed", "not_covered"]
    evidence_refs: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("criterion_id")
    @classmethod
    def _criterion_id_is_canonical(cls, value: str) -> str:
        return _bounded_identifier(value, "criterion_id")

    @field_validator("evidence_refs")
    @classmethod
    def _criterion_refs_are_controlled(cls, values: list[str]) -> list[str]:
        normalized = [_bounded_reference(value, "criterion evidence ref") for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("criterion evidence refs must be unique")
        return normalized

    @model_validator(mode="after")
    def _passed_criteria_need_evidence(self) -> "DelegationCriterionResult":
        if self.status == "passed" and not self.evidence_refs:
            raise ValueError("passed criterion requires controlled evidence")
        return self


class DelegationSpec(_StrictDelegationModel):
    """The minimum, parent-approved input allowed to create one child Run."""

    schema_version: Literal["cowork.delegation.v1"] = DELEGATION_CONTRACT_VERSION
    delegation_id: str = Field(min_length=1, max_length=256)
    parent_run_id: str = Field(min_length=1, max_length=256)
    parent_step_id: str = Field(min_length=1, max_length=256)
    # The Local Runtime uses an opaque local tenant until a server-issued
    # SubjectContext is available. It is still carried in the immutable Spec
    # so a receipt cannot cross a Parent tenant boundary silently.
    tenant_id: str = Field(default="desktop-local", min_length=1, max_length=256)
    attempt: int = Field(default=1, ge=1, le=100)
    mode: Literal["read_only"] = "read_only"
    objective: str = Field(min_length=1, max_length=2048)
    input_refs: list[str] = Field(default_factory=list, max_length=20)
    # Server remote-only delegation has no local workspace grant. Individual
    # scopes remain strict whenever a delegation does include local paths.
    workspace_scope: list[DelegationWorkspaceScope] = Field(max_length=16)
    capability_allowlist: list[str] = Field(min_length=1, max_length=64)
    acceptance: list[str] = Field(min_length=1, max_length=32)
    budget: DelegationBudget
    join_group: str = Field(default="default", min_length=1, max_length=128)
    join_policy: DelegationJoinPolicy = Field(default_factory=DelegationJoinPolicy)
    result_policy: Literal["required", "optional"] = "required"
    idempotency_key: str = Field(min_length=1, max_length=256)

    @field_validator("delegation_id", "parent_run_id", "parent_step_id", "tenant_id", "idempotency_key")
    @classmethod
    def _ids_are_canonical(cls, value: str) -> str:
        return _bounded_identifier(value, "delegation identifier")

    @field_validator("objective")
    @classmethod
    def _objective_is_bounded(cls, value: str) -> str:
        return _bounded_text(value, "objective", max_length=2048)

    @field_validator("input_refs")
    @classmethod
    def _input_refs_are_controlled(cls, values: list[str]) -> list[str]:
        normalized = [_bounded_reference(value, "input ref") for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("input refs must be unique")
        return normalized

    @field_validator("workspace_scope")
    @classmethod
    def _scope_grants_are_unique(cls, values: list[DelegationWorkspaceScope]) -> list[DelegationWorkspaceScope]:
        if len({value.grant_id for value in values}) != len(values):
            raise ValueError("workspace scope grant ids must be unique")
        return values

    @field_validator("capability_allowlist")
    @classmethod
    def _capabilities_are_canonical(cls, values: list[str]) -> list[str]:
        normalized = [_bounded_identifier(value, "capability id") for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("capability allowlist must be unique")
        return normalized

    @field_validator("acceptance")
    @classmethod
    def _acceptance_is_bounded(cls, values: list[str]) -> list[str]:
        normalized = [_bounded_text(value, "acceptance criterion", max_length=512) for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("acceptance criteria must be unique")
        return normalized

    @field_validator("join_group")
    @classmethod
    def _join_group_is_canonical(cls, value: str) -> str:
        return _bounded_identifier(value, "join_group")


class DelegationReceipt(_StrictDelegationModel):
    """The only child output visible to a parent by default."""

    schema_version: Literal["cowork.delegation-receipt.v1"] = DELEGATION_RECEIPT_CONTRACT_VERSION
    delegation_id: str = Field(min_length=1, max_length=256)
    parent_run_id: str = Field(min_length=1, max_length=256)
    parent_step_id: str = Field(min_length=1, max_length=256)
    tenant_id: str = Field(default="desktop-local", min_length=1, max_length=256)
    attempt: int = Field(ge=1, le=100)
    idempotency_key: str = Field(min_length=1, max_length=256)
    child_run_id: str = Field(min_length=1, max_length=256)
    # These are correlation commitments derived from the immutable Spec, not
    # claims inferred from child text. Empty values remain parse-compatible
    # with pre-verifier receipts but are rejected by DelegationReceiptVerifier.
    objective_hash: str = Field(default="", max_length=64)
    spec_hash: str = Field(default="", max_length=64)
    capability_snapshot: list[str] = Field(default_factory=list, max_length=64)
    trace_ref: str = Field(min_length=1, max_length=300)
    status: Literal["completed", "partial", "blocked", "failed", "cancelled", "expired"]
    reason_code: str = Field(min_length=1, max_length=120)
    fact_summary: str = Field(default="", max_length=2048)
    criteria: list[DelegationCriterionResult] = Field(default_factory=list, max_length=32)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)
    candidate_refs: list[str] = Field(default_factory=list, max_length=20)
    usage: DelegationUsage
    recommended_next_action: Literal["accept", "verify", "replan", "wait_for_user", "stop"]
    completed_at: datetime

    @field_validator("delegation_id", "parent_run_id", "parent_step_id", "tenant_id", "idempotency_key", "child_run_id")
    @classmethod
    def _receipt_ids_are_canonical(cls, value: str) -> str:
        return _bounded_identifier(value, "receipt identifier")

    @field_validator("trace_ref")
    @classmethod
    def _trace_ref_is_controlled(cls, value: str) -> str:
        return _bounded_reference(value, "receipt trace ref")

    @field_validator("objective_hash", "spec_hash")
    @classmethod
    def _receipt_hashes_are_bounded(cls, value: str) -> str:
        return _sha256(value, "receipt hash", allow_empty=True)

    @field_validator("capability_snapshot")
    @classmethod
    def _receipt_capabilities_are_canonical(cls, values: list[str]) -> list[str]:
        normalized = [_bounded_identifier(value, "receipt capability") for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("capability snapshot must be unique")
        return normalized

    @field_validator("reason_code")
    @classmethod
    def _reason_code_is_stable(cls, value: str) -> str:
        normalized = value.strip()
        if normalized != value or not _REASON_CODE.fullmatch(normalized):
            raise ValueError("reason_code must be a stable lowercase code")
        return normalized

    @field_validator("fact_summary")
    @classmethod
    def _summary_is_safe(cls, value: str) -> str:
        return _bounded_text(value, "fact_summary", max_length=2048, allow_empty=True)

    @field_validator("evidence_refs", "candidate_refs")
    @classmethod
    def _receipt_refs_are_controlled(cls, values: list[str]) -> list[str]:
        normalized = [_bounded_reference(value, "receipt evidence ref") for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("receipt refs must be unique")
        return normalized

    @field_validator("completed_at")
    @classmethod
    def _completed_at_is_timezone_aware(cls, value: datetime) -> datetime:
        return _timezone_datetime(value)

    @model_validator(mode="after")
    def _completed_receipt_requires_evidence(self) -> "DelegationReceipt":
        criterion_ids = [criterion.criterion_id for criterion in self.criteria]
        if len(set(criterion_ids)) != len(criterion_ids):
            raise ValueError("receipt criterion ids must be unique")
        if self.status == "completed":
            if not self.fact_summary or not self.evidence_refs:
                raise ValueError("completed receipt requires summary and evidence")
            if not any(criterion.status == "passed" for criterion in self.criteria):
                raise ValueError("completed receipt requires a passed criterion")
            if self.trace_ref not in self.evidence_refs:
                raise ValueError("completed receipt requires its child trace evidence")
        if self.candidate_refs:
            raise ValueError("read-only receipt cannot contain candidate refs")
        return self


class DelegationReceiptVerification(_StrictDelegationModel):
    """A durable, redacted verdict for one immutable receipt attempt."""

    schema_version: Literal["cowork.delegation-verification.v1"] = DELEGATION_VERIFICATION_CONTRACT_VERSION
    parent_run_id: str = Field(min_length=1, max_length=256)
    delegation_id: str = Field(min_length=1, max_length=256)
    attempt: int = Field(ge=1, le=100)
    join_group: str = Field(min_length=1, max_length=128)
    revision: int = Field(default=0, ge=0, le=10_000)
    result_policy: Literal["required", "optional"]
    verdict: DelegationVerificationVerdict
    reason_code: str = Field(min_length=1, max_length=120)
    receipt_status: Literal["completed", "partial", "blocked", "failed", "cancelled", "expired"]
    fact_summary: str = Field(default="", max_length=2048)
    criteria: list[DelegationCriterionResult] = Field(default_factory=list, max_length=32)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)
    evidence_digest: str = Field(min_length=64, max_length=64)
    verified_at: datetime

    @field_validator("parent_run_id", "delegation_id", "join_group")
    @classmethod
    def _verification_ids_are_canonical(cls, value: str) -> str:
        return _bounded_identifier(value, "verification identifier")

    @field_validator("reason_code")
    @classmethod
    def _verification_reason_is_stable(cls, value: str) -> str:
        normalized = value.strip()
        if normalized != value or not _REASON_CODE.fullmatch(normalized):
            raise ValueError("reason_code must be a stable lowercase code")
        return normalized

    @field_validator("fact_summary")
    @classmethod
    def _verification_summary_is_safe(cls, value: str) -> str:
        return _bounded_text(value, "verification fact_summary", max_length=2048, allow_empty=True)

    @field_validator("evidence_refs")
    @classmethod
    def _verification_refs_are_controlled(cls, values: list[str]) -> list[str]:
        normalized = [_bounded_reference(value, "verification evidence ref") for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("verification evidence refs must be unique")
        return normalized

    @field_validator("evidence_digest")
    @classmethod
    def _verification_digest_is_sha256(cls, value: str) -> str:
        return _sha256(value, "evidence_digest")

    @field_validator("verified_at")
    @classmethod
    def _verification_time_is_timezone_aware(cls, value: datetime) -> datetime:
        return _timezone_datetime(value)

    @model_validator(mode="after")
    def _verification_does_not_project_rejected_child_content(self) -> "DelegationReceiptVerification":
        if self.verdict is DelegationVerificationVerdict.REJECTED and (
            self.fact_summary or self.criteria or self.evidence_refs
        ):
            raise ValueError("rejected verification cannot project child facts or evidence")
        return self


class DelegationVerificationFact(_StrictDelegationModel):
    """The only child-derived fact shape permitted in Parent Context."""

    delegation_id: str = Field(min_length=1, max_length=256)
    attempt: int = Field(ge=1, le=100)
    result_policy: Literal["required", "optional"]
    receipt_status: Literal["completed", "partial", "blocked", "failed", "cancelled", "expired"]
    reason_code: str = Field(min_length=1, max_length=120)
    fact_summary: str = Field(default="", max_length=2048)
    criteria: list[DelegationCriterionResult] = Field(default_factory=list, max_length=32)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("delegation_id")
    @classmethod
    def _fact_id_is_canonical(cls, value: str) -> str:
        return _bounded_identifier(value, "delegation fact identifier")

    @field_validator("reason_code")
    @classmethod
    def _fact_reason_is_stable(cls, value: str) -> str:
        normalized = value.strip()
        if normalized != value or not _REASON_CODE.fullmatch(normalized):
            raise ValueError("reason_code must be a stable lowercase code")
        return normalized

    @field_validator("fact_summary")
    @classmethod
    def _fact_summary_is_safe(cls, value: str) -> str:
        return _bounded_text(value, "delegation fact_summary", max_length=2048, allow_empty=True)

    @field_validator("evidence_refs")
    @classmethod
    def _fact_refs_are_controlled(cls, values: list[str]) -> list[str]:
        normalized = [_bounded_reference(value, "delegation fact evidence ref") for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("delegation fact evidence refs must be unique")
        return normalized


class DelegationVerificationAggregate(_StrictDelegationModel):
    """A bounded verification result; it is never a Parent delivery verdict."""

    schema_version: Literal["cowork.delegation-verification-aggregate.v1"] = DELEGATION_VERIFICATION_AGGREGATE_CONTRACT_VERSION
    continuation_id: str = Field(min_length=1, max_length=256)
    parent_run_id: str = Field(min_length=1, max_length=256)
    join_group: str = Field(min_length=1, max_length=128)
    revision: int = Field(default=0, ge=0, le=10_000)
    status: DelegationVerificationAggregateStatus
    reason_code: str = Field(min_length=1, max_length=120)
    required_count: int = Field(ge=0, le=3)
    required_accepted_count: int = Field(ge=0, le=3)
    accepted_count: int = Field(ge=0, le=3)
    rejected_count: int = Field(ge=0, le=3)
    accepted_facts: list[DelegationVerificationFact] = Field(default_factory=list, max_length=3)
    rejection_reason_codes: list[str] = Field(default_factory=list, max_length=3)
    generated_at: datetime

    @field_validator("continuation_id", "parent_run_id", "join_group")
    @classmethod
    def _verification_aggregate_ids_are_canonical(cls, value: str) -> str:
        return _bounded_identifier(value, "verification aggregate identifier")

    @field_validator("reason_code", "rejection_reason_codes")
    @classmethod
    def _verification_aggregate_codes_are_stable(cls, value):
        values = value if isinstance(value, list) else [value]
        normalized: list[str] = []
        for item in values:
            if not isinstance(item, str) or item != item.strip() or not _REASON_CODE.fullmatch(item):
                raise ValueError("reason codes must be stable lowercase codes")
            normalized.append(item)
        if len(set(normalized)) != len(normalized):
            raise ValueError("reason codes must be unique")
        return normalized if isinstance(value, list) else normalized[0]

    @field_validator("generated_at")
    @classmethod
    def _verification_aggregate_time_is_timezone_aware(cls, value: datetime) -> datetime:
        return _timezone_datetime(value)

    @model_validator(mode="after")
    def _verification_aggregate_counts_are_consistent(self) -> "DelegationVerificationAggregate":
        if self.required_accepted_count > self.required_count:
            raise ValueError("required accepted count cannot exceed required count")
        if self.accepted_count != len(self.accepted_facts):
            raise ValueError("accepted facts must match accepted count")
        if self.status is DelegationVerificationAggregateStatus.READY_FOR_PARENT:
            if self.required_accepted_count != self.required_count:
                raise ValueError("ready aggregate requires all required receipts accepted")
        return self


def delegation_objective_hash(spec: DelegationSpec) -> str:
    """Return the immutable objective commitment used by receipt verification."""

    return hashlib.sha256(spec.objective.encode("utf-8")).hexdigest()


def delegation_spec_hash(spec: DelegationSpec) -> str:
    """Hash the canonical parent-approved Spec without trusting child data."""

    payload = json.dumps(spec.model_dump(mode="json"), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def delegation_criterion_id(index: int) -> str:
    """Derive stable receipt ids while v1 acceptance remains a text list."""

    if index < 1:
        raise ValueError("acceptance index must start at one")
    return f"acceptance-{index}"


__all__ = [
    "DELEGATION_CONTRACT_VERSION",
    "DELEGATION_RECEIPT_CONTRACT_VERSION",
    "DELEGATION_AGGREGATE_CONTRACT_VERSION",
    "DELEGATION_VERIFICATION_CONTRACT_VERSION",
    "DELEGATION_VERIFICATION_AGGREGATE_CONTRACT_VERSION",
    "DelegationAggregate",
    "DelegationAggregateStatus",
    "DelegationVerificationAggregate",
    "DelegationVerificationAggregateStatus",
    "DelegationVerificationFact",
    "DelegationReceiptVerification",
    "DelegationVerificationVerdict",
    "DelegationBudget",
    "DelegationCriterionResult",
    "DelegationJoinKind",
    "DelegationJoinPolicy",
    "DelegationReceipt",
    "DelegationSpec",
    "DelegationTaskStatus",
    "DelegationUsage",
    "DelegationWorkspaceScope",
    "delegation_criterion_id",
    "delegation_objective_hash",
    "delegation_spec_hash",
]
