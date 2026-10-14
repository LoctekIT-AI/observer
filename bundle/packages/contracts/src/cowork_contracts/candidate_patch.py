"""Strict contracts for isolated Subagent candidate patches.

Candidate artifacts describe bounded replacement facts only.  File bytes stay
inside the Host-owned staging area and are never embedded in Parent context.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


BASE_MANIFEST_CONTRACT_VERSION = "cowork.candidate-base-manifest.v1"
PATCH_ARTIFACT_CONTRACT_VERSION = "cowork.patch-artifact.v1"
PATCH_VERIFICATION_CONTRACT_VERSION = "cowork.patch-verification.v1"
CANDIDATE_PATCH_RECEIPT_CONTRACT_VERSION = "cowork.candidate-patch-receipt.v1"

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


class _StrictCandidateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _identifier(value: str, field: str) -> str:
    normalized = value.strip()
    if normalized != value or not _IDENTIFIER.fullmatch(normalized):
        raise ValueError(f"{field} must be a canonical identifier")
    return normalized


def _relative_path(value: str) -> str:
    if value != value.strip() or not value:
        raise ValueError("candidate path must be non-empty and canonical")
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if normalized.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("candidate path must be a canonical relative path")
    if any(part.casefold() == ".git" for part in parts):
        raise ValueError("candidate path must not target Git metadata")
    return normalized


def _digest(value: str, field: str) -> str:
    if not _SHA256.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase sha256 digest")
    return value


def _reference(value: str, field: str) -> str:
    normalized = value.strip()
    if normalized != value or not _REFERENCE.fullmatch(normalized):
        raise ValueError(f"{field} must be a controlled reference")
    return normalized


def _safe_text(value: str, field: str, *, max_length: int) -> str:
    normalized = value.strip()
    if normalized != value or not normalized or len(normalized) > max_length:
        raise ValueError(f"{field} must be non-empty and bounded")
    if "\n" in normalized or "\r" in normalized or _DISCLOSURE.search(normalized):
        raise ValueError(f"{field} must not contain raw diagnostics or secrets")
    return normalized


def _aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return value.astimezone(timezone.utc)


class BaseManifestEntry(_StrictCandidateModel):
    relative_path: str = Field(min_length=1, max_length=512)
    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(ge=0, le=32 * 1024 * 1024)

    @field_validator("relative_path")
    @classmethod
    def _path_is_safe(cls, value: str) -> str:
        return _relative_path(value)

    @field_validator("sha256")
    @classmethod
    def _sha_is_valid(cls, value: str) -> str:
        return _digest(value, "base entry sha256")


class BaseManifest(_StrictCandidateModel):
    schema_version: Literal["cowork.candidate-base-manifest.v1"] = BASE_MANIFEST_CONTRACT_VERSION
    tenant_id: str = Field(min_length=1, max_length=256)
    parent_run_id: str = Field(min_length=1, max_length=256)
    delegation_id: str = Field(min_length=1, max_length=256)
    attempt: int = Field(ge=1, le=100)
    grant_id: str = Field(min_length=1, max_length=256)
    grant_version: str = Field(min_length=1, max_length=256)
    entries: list[BaseManifestEntry] = Field(min_length=1, max_length=32)
    generated_at: datetime

    @field_validator("tenant_id", "parent_run_id", "delegation_id", "grant_id", "grant_version")
    @classmethod
    def _ids_are_canonical(cls, value: str) -> str:
        return _identifier(value, "base manifest identifier")

    @field_validator("generated_at")
    @classmethod
    def _time_is_aware(cls, value: datetime) -> datetime:
        return _aware_datetime(value)

    @model_validator(mode="after")
    def _entries_are_unique(self) -> "BaseManifest":
        paths = [entry.relative_path for entry in self.entries]
        if len(set(paths)) != len(paths):
            raise ValueError("base manifest paths must be unique")
        return self


class PatchFileChange(_StrictCandidateModel):
    relative_path: str = Field(min_length=1, max_length=512)
    base_sha256: str = Field(min_length=64, max_length=64)
    candidate_sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(ge=0, le=32 * 1024 * 1024)

    @field_validator("relative_path")
    @classmethod
    def _path_is_safe(cls, value: str) -> str:
        return _relative_path(value)

    @field_validator("base_sha256", "candidate_sha256")
    @classmethod
    def _hash_is_valid(cls, value: str) -> str:
        return _digest(value, "patch file sha256")


class PatchTestEvidence(_StrictCandidateModel):
    test_id: str = Field(min_length=1, max_length=256)
    status: Literal["passed", "failed"]
    evidence_ref: str = Field(min_length=1, max_length=300)

    @field_validator("test_id")
    @classmethod
    def _test_id_is_canonical(cls, value: str) -> str:
        return _identifier(value, "test_id")

    @field_validator("evidence_ref")
    @classmethod
    def _evidence_ref_is_controlled(cls, value: str) -> str:
        return _reference(value, "test evidence ref")


class PatchArtifact(_StrictCandidateModel):
    schema_version: Literal["cowork.patch-artifact.v1"] = PATCH_ARTIFACT_CONTRACT_VERSION
    tenant_id: str = Field(min_length=1, max_length=256)
    parent_run_id: str = Field(min_length=1, max_length=256)
    delegation_id: str = Field(min_length=1, max_length=256)
    attempt: int = Field(ge=1, le=100)
    base_manifest_digest: str = Field(min_length=64, max_length=64)
    format: Literal["replacement-set-v1"] = "replacement-set-v1"
    changes: list[PatchFileChange] = Field(min_length=1, max_length=16)
    candidate_artifact_ref: str = Field(min_length=1, max_length=300)
    diff_digest: str = Field(min_length=64, max_length=64)
    tests: list[PatchTestEvidence] = Field(min_length=1, max_length=32)
    generator_version: str = Field(min_length=1, max_length=256)
    summary: str = Field(min_length=1, max_length=512)
    generated_at: datetime

    @field_validator("tenant_id", "parent_run_id", "delegation_id", "generator_version")
    @classmethod
    def _ids_are_canonical(cls, value: str) -> str:
        return _identifier(value, "patch artifact identifier")

    @field_validator("base_manifest_digest", "diff_digest")
    @classmethod
    def _digests_are_valid(cls, value: str) -> str:
        return _digest(value, "patch artifact digest")

    @field_validator("candidate_artifact_ref")
    @classmethod
    def _artifact_ref_is_controlled(cls, value: str) -> str:
        return _reference(value, "candidate artifact ref")

    @field_validator("summary")
    @classmethod
    def _summary_is_safe(cls, value: str) -> str:
        return _safe_text(value, "patch summary", max_length=512)

    @field_validator("generated_at")
    @classmethod
    def _time_is_aware(cls, value: datetime) -> datetime:
        return _aware_datetime(value)

    @model_validator(mode="after")
    def _changes_and_tests_are_unique(self) -> "PatchArtifact":
        paths = [item.relative_path for item in self.changes]
        if len(set(paths)) != len(paths):
            raise ValueError("patch change paths must be unique")
        test_ids = [item.test_id for item in self.tests]
        if len(set(test_ids)) != len(test_ids):
            raise ValueError("patch test ids must be unique")
        return self


class PatchVerification(_StrictCandidateModel):
    schema_version: Literal["cowork.patch-verification.v1"] = PATCH_VERIFICATION_CONTRACT_VERSION
    tenant_id: str = Field(min_length=1, max_length=256)
    parent_run_id: str = Field(min_length=1, max_length=256)
    delegation_id: str = Field(min_length=1, max_length=256)
    attempt: int = Field(ge=1, le=100)
    verdict: Literal["accepted", "rejected"]
    reason_code: str = Field(min_length=1, max_length=120)
    artifact_digest: str = Field(min_length=64, max_length=64)
    verified_paths: list[str] = Field(default_factory=list, max_length=16)
    verified_at: datetime

    @field_validator("tenant_id", "parent_run_id", "delegation_id")
    @classmethod
    def _ids_are_canonical(cls, value: str) -> str:
        return _identifier(value, "patch verification identifier")

    @field_validator("reason_code")
    @classmethod
    def _reason_is_stable(cls, value: str) -> str:
        if value != value.strip() or not _REASON_CODE.fullmatch(value):
            raise ValueError("reason_code must be a stable lowercase code")
        return value

    @field_validator("artifact_digest")
    @classmethod
    def _artifact_digest_is_valid(cls, value: str) -> str:
        return _digest(value, "verified artifact digest")

    @field_validator("verified_paths")
    @classmethod
    def _paths_are_safe(cls, values: list[str]) -> list[str]:
        normalized = [_relative_path(value) for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("verified paths must be unique")
        return normalized

    @field_validator("verified_at")
    @classmethod
    def _time_is_aware(cls, value: datetime) -> datetime:
        return _aware_datetime(value)

    @model_validator(mode="after")
    def _rejection_contains_no_accepted_scope(self) -> "PatchVerification":
        if self.verdict == "rejected" and self.verified_paths:
            raise ValueError("rejected patch verification cannot expose accepted paths")
        return self


class CandidatePatchReceipt(_StrictCandidateModel):
    """The only candidate child result admitted to the Parent by default."""

    schema_version: Literal["cowork.candidate-patch-receipt.v1"] = CANDIDATE_PATCH_RECEIPT_CONTRACT_VERSION
    tenant_id: str = Field(min_length=1, max_length=256)
    parent_run_id: str = Field(min_length=1, max_length=256)
    delegation_id: str = Field(min_length=1, max_length=256)
    attempt: int = Field(ge=1, le=100)
    child_run_id: str = Field(min_length=1, max_length=256)
    trace_ref: str = Field(min_length=1, max_length=300)
    status: Literal["candidate_ready", "failed", "cancelled"]
    reason_code: str = Field(min_length=1, max_length=120)
    patch_artifact_ref: str | None = Field(default=None, max_length=300)
    patch_artifact_digest: str | None = Field(default=None, max_length=64)
    summary: str = Field(min_length=1, max_length=512)
    test_evidence_refs: list[str] = Field(default_factory=list, max_length=32)
    completed_at: datetime

    @field_validator("tenant_id", "parent_run_id", "delegation_id", "child_run_id")
    @classmethod
    def _ids_are_canonical(cls, value: str) -> str:
        return _identifier(value, "candidate receipt identifier")

    @field_validator("trace_ref")
    @classmethod
    def _trace_ref_is_controlled(cls, value: str) -> str:
        return _reference(value, "candidate receipt trace ref")

    @field_validator("reason_code")
    @classmethod
    def _reason_is_stable(cls, value: str) -> str:
        if value != value.strip() or not _REASON_CODE.fullmatch(value):
            raise ValueError("reason_code must be a stable lowercase code")
        return value

    @field_validator("patch_artifact_ref")
    @classmethod
    def _patch_ref_is_controlled(cls, value: str | None) -> str | None:
        return _reference(value, "patch artifact ref") if value is not None else None

    @field_validator("patch_artifact_digest")
    @classmethod
    def _patch_digest_is_valid(cls, value: str | None) -> str | None:
        return _digest(value, "patch artifact digest") if value is not None else None

    @field_validator("summary")
    @classmethod
    def _summary_is_safe(cls, value: str) -> str:
        return _safe_text(value, "candidate receipt summary", max_length=512)

    @field_validator("test_evidence_refs")
    @classmethod
    def _test_refs_are_controlled(cls, values: list[str]) -> list[str]:
        normalized = [_reference(value, "candidate receipt test ref") for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("candidate receipt test refs must be unique")
        return normalized

    @field_validator("completed_at")
    @classmethod
    def _time_is_aware(cls, value: datetime) -> datetime:
        return _aware_datetime(value)

    @model_validator(mode="after")
    def _ready_receipt_contains_only_controlled_refs(self) -> "CandidatePatchReceipt":
        if self.status == "candidate_ready" and (
            self.patch_artifact_ref is None
            or self.patch_artifact_digest is None
            or not self.test_evidence_refs
        ):
            raise ValueError("candidate_ready receipt requires artifact and test evidence refs")
        if self.status != "candidate_ready" and (
            self.patch_artifact_ref is not None
            or self.patch_artifact_digest is not None
            or self.test_evidence_refs
        ):
            raise ValueError("non-ready candidate receipt cannot project candidate evidence")
        return self


def base_manifest_digest(manifest: BaseManifest) -> str:
    payload = json.dumps(manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def patch_artifact_digest(artifact: PatchArtifact) -> str:
    payload = json.dumps(artifact.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def patch_changes_digest(changes: list[PatchFileChange]) -> str:
    payload = json.dumps(
        [item.model_dump(mode="json") for item in changes],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "BASE_MANIFEST_CONTRACT_VERSION",
    "PATCH_ARTIFACT_CONTRACT_VERSION",
    "PATCH_VERIFICATION_CONTRACT_VERSION",
    "CANDIDATE_PATCH_RECEIPT_CONTRACT_VERSION",
    "BaseManifest",
    "BaseManifestEntry",
    "PatchArtifact",
    "PatchFileChange",
    "PatchTestEvidence",
    "PatchVerification",
    "CandidatePatchReceipt",
    "base_manifest_digest",
    "patch_artifact_digest",
    "patch_changes_digest",
]
