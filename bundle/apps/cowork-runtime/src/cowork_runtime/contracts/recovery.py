"""Versioned, secret-free recovery guidance contracts."""

from __future__ import annotations

import re
from typing import Any, List, Literal, Optional

from pydantic import Field

from .base import ContractModel

RECOVERY_GUIDANCE_SCHEMA_VERSION = "cowork.runtime.recovery.v1"
RecoveryAction = Literal["replan", "switch_tool", "retry_same", "wait_for_user", "stop"]
RecoveryStage = Literal["nudge", "restrict", "terminate"]


class RecoveryGuidance(ContractModel):
    """A bounded instruction that can be shown to the model and persisted.

    The contract intentionally contains no raw arguments, output, stderr, or
    traceback.  A hash is enough for deterministic de-duplication and for the
    Runtime to enforce the same decision before a Host call.
    """

    schema_version: Literal["cowork.runtime.recovery.v1"] = RECOVERY_GUIDANCE_SCHEMA_VERSION
    reason_code: str = Field(min_length=1, max_length=120)
    action: RecoveryAction
    stage: RecoveryStage = "nudge"
    instruction: str = Field(min_length=1, max_length=2_000)
    suggested_tools: List[str] = Field(default_factory=list, max_length=8)
    do_not_retry: bool = False
    forbidden_call_signature: Optional[str] = Field(default=None, max_length=64)
    wait_seconds: Optional[float] = Field(default=None, ge=0, le=3_600)
    occurrence_count: int = Field(default=1, ge=1)


_SAFE_DETAIL_FIELDS = frozenset({"reason_code", "http_status", "script_failure_kind", "category", "do_not_retry"})
_SENSITIVE_ERROR_PATTERN = re.compile(
    r"(?i)\b(token|access[_-]?token|authorization|password|secret|api[_-]?key)\s*(?:[:=]\s*|bearer\s+)[^\s,;]+"
)
_FILE_URI_PATTERN = re.compile(r"file://[^\s,;]+", re.IGNORECASE)
_ABSOLUTE_PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9_])/(?:[^\s,;]+)")
_UNSAFE_OUTPUT_KEYS = frozenset({"stdout", "stderr", "traceback", "stack", "stacktrace", "code", "content", "content_base64"})


def safe_error_context(error: Any) -> dict[str, Any]:
    """Project a RuntimeError without retaining logs or credential-like text."""

    message = " ".join(str(getattr(error, "message", "tool execution failed")).split())
    if "traceback" in message.casefold() or "stderr" in message.casefold():
        message = "tool reported a sanitized execution failure; follow recovery guidance"
    else:
        message = _sanitize_safe_text(message)
    raw_details = getattr(error, "details", {})
    details = (
        {key: value for key, value in raw_details.items() if key in _SAFE_DETAIL_FIELDS}
        if isinstance(raw_details, dict)
        else {}
    )
    return {
        "code": getattr(getattr(error, "code", None), "value", "unknown"),
        "message": message or "tool execution failed",
        "retryable": bool(getattr(error, "retryable", False)),
        "category": str(getattr(error, "category", "fatal")),
        "details": details,
    }


def safe_failure_output_context(value: Any) -> Any:
    """Keep useful failure diagnostics without carrying raw execution logs."""

    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, nested in value.items():
            normalized = str(key).lower()
            if normalized in _UNSAFE_OUTPUT_KEYS:
                continue
            if normalized == "diagnostics":
                output[str(key)] = _safe_diagnostics(nested)
            else:
                output[str(key)] = safe_failure_output_context(nested)
        return output
    if isinstance(value, list):
        return [safe_failure_output_context(item) for item in value[:32]]
    if isinstance(value, str):
        return _sanitize_safe_text(" ".join(value.split()))
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(type(value).__name__)


def _safe_diagnostics(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return [
        _sanitize_safe_text(" ".join(str(item).split()))
        for item in values[:16]
    ]


def _sanitize_safe_text(value: str) -> str:
    sanitized = _SENSITIVE_ERROR_PATTERN.sub(r"\1=[REDACTED]", value)
    sanitized = _FILE_URI_PATTERN.sub("file://[REDACTED]", sanitized)
    return _ABSOLUTE_PATH_PATTERN.sub("[PATH_REDACTED]", sanitized)[:500]


__all__ = [
    "RECOVERY_GUIDANCE_SCHEMA_VERSION",
    "RecoveryAction",
    "RecoveryGuidance",
    "RecoveryStage",
    "safe_error_context",
    "safe_failure_output_context",
]
