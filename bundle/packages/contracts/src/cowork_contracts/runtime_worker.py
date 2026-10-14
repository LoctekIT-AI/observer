"""Authenticated IPC envelope for the isolated Runtime Session worker.

The API and the worker intentionally share only this small, dependency-free
contract.  Runtime code, database sessions, and user credentials never cross
the IPC boundary.  Frames are newline-delimited JSON and are authenticated
with an HMAC over the canonical envelope.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import UUID, uuid4

from .trace_context import TraceContextError, parse_traceparent


RUNTIME_WORKER_PROTOCOL = "cowork.runtime.python_worker.v1"
RUNTIME_WORKER_CONTRACT_VERSION = "1.0"
DEFAULT_ENVELOPE_TTL_SECONDS = 30
MAX_FRAME_BYTES = 2 * 1024 * 1024
# A health request is handled by the supervisor itself.  It must never create
# a Runtime child or evaluate user supplied Python; the API uses it for the
# readiness gate so a configured-but-dead worker cannot look healthy.
RUNTIME_WORKER_HEALTH_OPERATION = "health"


class RuntimeWorkerProtocolError(ValueError):
    """Raised when an IPC frame cannot be authenticated or validated."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _signature_payload(envelope: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in envelope.items() if key != "signature"}


def sign_envelope(envelope: Mapping[str, Any], token: str) -> str:
    secret = str(token or "").encode("utf-8")
    if len(secret) < 32:
        raise RuntimeWorkerProtocolError("runtime worker token must be at least 32 bytes")
    digest = hmac.new(secret, canonical_json(_signature_payload(envelope)), hashlib.sha256)
    return digest.hexdigest()


def build_envelope(
    payload: Mapping[str, Any],
    token: str,
    *,
    request_id: str | UUID | None = None,
    trace_id: str | None = None,
    traceparent: str | None = None,
    ttl_seconds: int = DEFAULT_ENVELOPE_TTL_SECONDS,
) -> dict[str, Any]:
    now = utc_now()
    request = str(request_id or uuid4())
    context = None
    if traceparent:
        try:
            context = parse_traceparent(traceparent)
        except TraceContextError as exc:
            raise RuntimeWorkerProtocolError(str(exc)) from exc
        if trace_id is not None and str(trace_id) != context.trace_id:
            raise RuntimeWorkerProtocolError("traceparent trace_id does not match trace_id")
    trace = context.trace_id if context is not None else str(trace_id or request)
    ttl = max(1, min(int(ttl_seconds), 300))
    envelope: dict[str, Any] = {
        "protocol": RUNTIME_WORKER_PROTOCOL,
        "contract_version": RUNTIME_WORKER_CONTRACT_VERSION,
        "request_id": request,
        "trace_id": trace,
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=ttl)).isoformat(),
        "payload": dict(payload),
    }
    if context is not None:
        envelope["traceparent"] = context.traceparent
    envelope["signature"] = sign_envelope(envelope, token)
    return envelope


def _parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeWorkerProtocolError(f"missing {field}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeWorkerProtocolError(f"invalid {field}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeWorkerProtocolError(f"{field} must include timezone")
    return parsed.astimezone(timezone.utc)


def verify_envelope(
    envelope: Mapping[str, Any],
    token: str,
    *,
    now: datetime | None = None,
    max_clock_skew_seconds: int = 5,
) -> dict[str, Any]:
    if not isinstance(envelope, Mapping):
        raise RuntimeWorkerProtocolError("IPC frame must be an object")
    if envelope.get("protocol") != RUNTIME_WORKER_PROTOCOL:
        raise RuntimeWorkerProtocolError("unsupported runtime worker protocol")
    if envelope.get("contract_version") != RUNTIME_WORKER_CONTRACT_VERSION:
        raise RuntimeWorkerProtocolError("unsupported runtime worker contract version")
    request_id = str(envelope.get("request_id") or "").strip()
    if not request_id or len(request_id) > 128:
        raise RuntimeWorkerProtocolError("invalid request_id")
    try:
        UUID(request_id)
    except ValueError as exc:
        raise RuntimeWorkerProtocolError("request_id must be a UUID") from exc
    issued = _parse_timestamp(envelope.get("issued_at"), "issued_at")
    expires = _parse_timestamp(envelope.get("expires_at"), "expires_at")
    current = (now or utc_now()).astimezone(timezone.utc)
    skew = timedelta(seconds=max(0, int(max_clock_skew_seconds)))
    if issued > current + skew:
        raise RuntimeWorkerProtocolError("issued_at is in the future")
    if expires <= issued or expires < current - skew:
        raise RuntimeWorkerProtocolError("runtime worker request expired")
    payload = envelope.get("payload")
    if not isinstance(payload, Mapping):
        raise RuntimeWorkerProtocolError("IPC payload must be an object")
    traceparent = envelope.get("traceparent")
    if traceparent is not None:
        try:
            context = parse_traceparent(str(traceparent))
        except TraceContextError as exc:
            raise RuntimeWorkerProtocolError(str(exc)) from exc
        if str(envelope.get("trace_id") or "") != context.trace_id:
            raise RuntimeWorkerProtocolError("traceparent trace_id does not match trace_id")
    signature = str(envelope.get("signature") or "")
    if not signature or not hmac.compare_digest(signature, sign_envelope(envelope, token)):
        raise RuntimeWorkerProtocolError("runtime worker signature mismatch")
    return dict(payload)


def frame_bytes(envelope: Mapping[str, Any]) -> bytes:
    data = canonical_json(envelope) + b"\n"
    if len(data) > MAX_FRAME_BYTES:
        raise RuntimeWorkerProtocolError("runtime worker frame exceeds size limit")
    return data


__all__ = [
    "DEFAULT_ENVELOPE_TTL_SECONDS",
    "MAX_FRAME_BYTES",
    "RUNTIME_WORKER_HEALTH_OPERATION",
    "RUNTIME_WORKER_CONTRACT_VERSION",
    "RUNTIME_WORKER_PROTOCOL",
    "RuntimeWorkerProtocolError",
    "build_envelope",
    "canonical_json",
    "frame_bytes",
    "sign_envelope",
    "utc_now",
    "verify_envelope",
]
