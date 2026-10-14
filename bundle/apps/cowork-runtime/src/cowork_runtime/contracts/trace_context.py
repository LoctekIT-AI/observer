"""Small dependency-free W3C Trace Context contract for Runtime boundaries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from secrets import token_hex


class TraceContextError(ValueError):
    pass


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    span_id: str
    trace_flags: str = "01"

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{32}", self.trace_id) or self.trace_id == "0" * 32:
            raise TraceContextError("trace_id must be a non-zero lowercase 32-hex value")
        if not re.fullmatch(r"[0-9a-f]{16}", self.span_id) or self.span_id == "0" * 16:
            raise TraceContextError("span_id must be a non-zero lowercase 16-hex value")
        if self.trace_flags not in {"00", "01"}:
            raise TraceContextError("trace_flags must be 00 or 01")

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{self.trace_flags}"

    def child(self) -> "TraceContext":
        return TraceContext(self.trace_id, token_hex(8), self.trace_flags)


def parse_traceparent(value: str | None) -> TraceContext:
    parts = str(value or "").strip().split("-")
    if len(parts) != 4 or parts[0] != "00":
        raise TraceContextError("traceparent must use W3C version 00")
    return TraceContext(parts[1], parts[2], parts[3])


def new_trace_context(trace_id: str | None = None) -> TraceContext:
    return TraceContext(trace_id or token_hex(16), token_hex(8))


__all__ = ["TraceContext", "TraceContextError", "new_trace_context", "parse_traceparent"]
