"""Versioned delivery failure matrix shared by Runtime and renderers."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .delivery import DeliveryFacts, DeliveryNextAction, DeliveryOutcomeKind


DELIVERY_FAILURE_MATRIX_VERSION = "cowork.delivery-failure-matrix.v1"
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,119}$")
_FINGERPRINT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_DISCLOSURE = re.compile(
    r"(?:https?://|\b(?:bearer|api[_ -]?key|password|secret|authorization|cookie|stderr|stdout|traceback)\b|```)",
    re.IGNORECASE,
)


class _StrictMatrixModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeliveryMatrixPersistenceExpectation(_StrictMatrixModel):
    max_consumptions: int = Field(default=0, ge=0, le=3)
    max_continuations: int = Field(default=0, ge=0, le=1)
    max_notifications: int = Field(default=0, ge=0, le=1)
    max_billing_writes: int = Field(default=0, ge=0, le=1)
    max_patch_applies: int = Field(default=0, ge=0, le=1)
    workspace_unchanged: bool = True
    terminal_is_immutable: bool = True


class DeliveryMatrixVerificationExpectation(_StrictMatrixModel):
    verdict: Literal["accepted", "rejected", "waiting", "not_applicable"]
    reason_code: str = Field(min_length=1, max_length=120)
    parent_context_mode: Literal["verified_summary_only", "reason_code_only", "none"]

    @field_validator("reason_code")
    @classmethod
    def _reason_is_stable(cls, value: str) -> str:
        if value != value.strip() or not _IDENTIFIER.fullmatch(value):
            raise ValueError("matrix reason_code must be stable")
        return value


class DeliveryMatrixRendererExpectation(_StrictMatrixModel):
    settled: bool = True
    loading: bool = False
    raw_error_visible: bool = False
    summary: str = Field(min_length=1, max_length=1000)

    @field_validator("summary")
    @classmethod
    def _summary_is_safe(cls, value: str) -> str:
        if value != value.strip() or "\n" in value or "\r" in value or _DISCLOSURE.search(value):
            raise ValueError("renderer summary must be a safe single line")
        return value


class DeliveryFailureScenario(_StrictMatrixModel):
    scenario_id: str = Field(min_length=1, max_length=120)
    category: Literal[
        "model",
        "context",
        "tool",
        "artifact",
        "user_gate",
        "restart",
        "client_action",
        "transport",
        "projection",
        "cancel",
        "delegation",
        "candidate_patch",
    ]
    hosts: list[Literal["local", "server", "remote", "desktop", "web"]] = Field(min_length=1, max_length=5)
    fault_code: str = Field(min_length=1, max_length=120)
    delivery_facts: DeliveryFacts
    expected_outcome_kind: DeliveryOutcomeKind
    expected_next_action: DeliveryNextAction
    persistence: DeliveryMatrixPersistenceExpectation
    verification: DeliveryMatrixVerificationExpectation
    renderer: DeliveryMatrixRendererExpectation
    max_settle_ms: int = Field(ge=1, le=300_000)
    max_parent_context_chars: int = Field(default=4096, ge=0, le=65_536)
    max_receipt_latency_ms: int = Field(default=30_000, ge=1, le=300_000)
    max_join_latency_ms: int = Field(default=60_000, ge=1, le=300_000)
    hard_fail_on_mismatch: bool = True
    environment_gate: Literal["deterministic", "degraded", "blocked"] = "deterministic"

    @field_validator("scenario_id", "fault_code")
    @classmethod
    def _codes_are_stable(cls, value: str) -> str:
        if value != value.strip() or not _IDENTIFIER.fullmatch(value):
            raise ValueError("matrix identifiers must be stable lowercase codes")
        return value

    @field_validator("hosts")
    @classmethod
    def _hosts_are_unique(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("matrix hosts must be unique")
        return values


class DeliveryFailureMatrix(_StrictMatrixModel):
    schema_version: Literal["cowork.delivery-failure-matrix.v1"] = DELIVERY_FAILURE_MATRIX_VERSION
    matrix_id: str = Field(min_length=1, max_length=120)
    contract_fingerprint: str = Field(min_length=1, max_length=256)
    runtime_fingerprint: str = Field(min_length=1, max_length=256)
    renderer_fingerprint: str = Field(min_length=1, max_length=256)
    scenarios: list[DeliveryFailureScenario] = Field(min_length=1, max_length=128)

    @field_validator("matrix_id")
    @classmethod
    def _matrix_id_is_stable(cls, value: str) -> str:
        if value != value.strip() or not _IDENTIFIER.fullmatch(value):
            raise ValueError("matrix_id must be stable")
        return value

    @field_validator("contract_fingerprint", "runtime_fingerprint", "renderer_fingerprint")
    @classmethod
    def _fingerprints_are_stable(cls, value: str) -> str:
        if value != value.strip() or not _FINGERPRINT.fullmatch(value):
            raise ValueError("matrix fingerprints must be canonical")
        return value

    @model_validator(mode="after")
    def _scenario_ids_are_unique(self) -> "DeliveryFailureMatrix":
        ids = [scenario.scenario_id for scenario in self.scenarios]
        if len(set(ids)) != len(ids):
            raise ValueError("matrix scenario ids must be unique")
        return self


__all__ = [
    "DELIVERY_FAILURE_MATRIX_VERSION",
    "DeliveryFailureMatrix",
    "DeliveryFailureScenario",
    "DeliveryMatrixPersistenceExpectation",
    "DeliveryMatrixRendererExpectation",
    "DeliveryMatrixVerificationExpectation",
]
