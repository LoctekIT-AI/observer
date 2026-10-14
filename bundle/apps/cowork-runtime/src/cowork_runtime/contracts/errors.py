"""Runtime contract error models and local exceptions."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Dict, Literal

from pydantic import Field

from .base import ContractModel
from .enums import RuntimeErrorCode

ErrorCategory = Literal["model_repairable", "transient", "fatal"]


class RuntimeContractError(Exception):
    """Raised when a contract object violates runtime invariants."""


class RuntimeSequenceError(RuntimeContractError):
    """Raised when RuntimeEvent seq order is not append-only."""


class RuntimeError(ContractModel):
    code: RuntimeErrorCode = RuntimeErrorCode.UNKNOWN
    message: str
    retryable: bool = False
    category: ErrorCategory = "fatal"
    details: Dict[str, Any] = Field(default_factory=dict)


def classify_runtime_error(error: RuntimeError) -> ErrorCategory:
    """Return the safe handling class for a Tool/Runtime error."""

    requested = error.details.get("category")
    if requested in {"model_repairable", "transient", "fatal"}:
        return requested
    if error.category != "fatal":
        return error.category
    if error.code == RuntimeErrorCode.POLICY_DENIED or error.code in {
        RuntimeErrorCode.CANCELLED,
        RuntimeErrorCode.APPROVAL_EXPIRED,
    }:
        return "fatal"
    if error.retryable:
        return "transient"
    if error.code in {RuntimeErrorCode.INVALID_REQUEST, RuntimeErrorCode.TOOL_FAILED}:
        return "model_repairable"
    return "fatal"


def error_signature(
    error: RuntimeError,
    *,
    tool_name: str = "",
    arguments: Dict[str, Any] | None = None,
) -> str:
    """Create a stable, secret-free signature for Tool Storm detection."""

    # Raw arguments never leave this function.  Including only their digest
    # prevents a parameter-guessing loop from being mistaken for a literal
    # same-call Tool Storm, while keeping secrets out of state and traces.
    argument_digest = sha256(
        json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    raw = f"{tool_name}|{argument_digest}|{error.code.value}|{error.message[:500]}"
    return sha256(raw.encode("utf-8")).hexdigest()[:24]
