"""Validated W3C Trace Context helpers used at Cowork host boundaries.

The context is intentionally limited to trace/span identifiers and flags.  It
must never carry bearer tokens, cookies, file paths, prompts, or tool payloads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from secrets import token_hex


TRACEPARENT_VERSION = "00"
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")


class TraceContextError(ValueError):
    """Raised when a propagated context is missing or malformed."""


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    span_id: str
    trace_flags: str = "01"

    def __post_init__(self) -> None:
        if not _TRACE_ID_RE.fullmatch(self.trace_id) or self.trace_id == "0" * 32:
            raise TraceContextError("trace_id must be a non-zero lowercase 32-hex value")
        if not _SPAN_ID_RE.fullmatch(self.span_id) or self.span_id == "0" * 16:
            raise TraceContextError("span_id must be a non-zero lowercase 16-hex value")
        if self.trace_flags not in {"00", "01"}:
            raise TraceContextError("trace_flags must be 00 or 01")

    @property
    def traceparent(self) -> str:
        return f"{TRACEPARENT_VERSION}-{self.trace_id}-{self.span_id}-{self.trace_flags}"

    def child(self) -> "TraceContext":
        return TraceContext(self.trace_id, token_hex(8), self.trace_flags)


def parse_traceparent(value: str | None) -> TraceContext:
    raw = str(value or "").strip()
    parts = raw.split("-")
    if len(parts) != 4 or parts[0] != TRACEPARENT_VERSION:
        raise TraceContextError("traceparent must use W3C version 00")
    return TraceContext(parts[1], parts[2], parts[3])


def new_trace_context() -> TraceContext:
    return TraceContext(token_hex(16), token_hex(8))


def safe_trace_diagnostic(
    *,
    status: str,
    host: str,
    transport: str,
    context: TraceContext | None = None,
    reason: str | None = None,
) -> dict[str, str | None]:
    """Return an allowlisted diagnostic suitable for events and envelopes."""

    return {
        "status": status,
        "host": host,
        "transport": transport,
        "trace_id": context.trace_id if context else None,
        "span_id": context.span_id if context else None,
        "reason": reason,
    }


__all__ = [
    "TRACEPARENT_VERSION",
    "TraceContext",
    "TraceContextError",
    "new_trace_context",
    "parse_traceparent",
    "safe_trace_diagnostic",
]
