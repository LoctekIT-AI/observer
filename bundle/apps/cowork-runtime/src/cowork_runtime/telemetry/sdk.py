"""Strict metadata-only telemetry event builders."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping


_SENSITIVE_KEYS = frozenset({"prompt", "output", "arguments", "input", "stdout", "stderr", "sql", "api_key", "token", "cookie", "password", "secret", "body"})


class TelemetrySensitiveFieldError(ValueError):
    """A payload contains a field that cannot enter standard telemetry."""


def _scan(value: Any, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in _SENSITIVE_KEYS or any(part in normalized for part in ("password", "secret", "token", "api_key")):
                raise TelemetrySensitiveFieldError(f"sensitive telemetry field: {path}.{key}")
            _scan(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _scan(child, f"{path}[{index}]")


def redact_for_telemetry(value: Mapping[str, Any], *, profile: str = "telemetry-default-v1") -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("telemetry payload must be an object")
    _scan(value)
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def canonical_payload_hash(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _timestamp(value: datetime | None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("telemetry timestamp must include timezone")
    normalized = current.astimezone(timezone.utc)
    rendered = normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return rendered.replace(".000000Z", "Z")


def build_event(
    *,
    event_type: str,
    producer: Mapping[str, Any],
    correlation: Mapping[str, Any],
    subject: Mapping[str, Any],
    privacy: Mapping[str, Any],
    payload: Mapping[str, Any],
    occurred_at: datetime | None = None,
    source_event_id: str | None = None,
) -> dict[str, Any]:
    safe_payload = redact_for_telemetry(payload)
    if not isinstance(producer, Mapping) or not isinstance(correlation, Mapping) or not isinstance(subject, Mapping) or not isinstance(privacy, Mapping):
        raise TypeError("telemetry envelope sections must be objects")
    instance = str(producer.get("instance_pseudonym") or "").strip()
    invocation = str(correlation.get("invocation_id") or source_event_id or "").strip()
    stage = str(safe_payload.get("stage") or "event").strip()
    if not instance or not invocation:
        raise ValueError("telemetry producer instance and invocation/source event are required")
    occurred = _timestamp(occurred_at)
    key = f"{instance}:{invocation}:{stage}"
    event_id = "telemetry:" + hashlib.sha256(f"{event_type}:{key}".encode("utf-8")).hexdigest()
    return {
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": occurred,
        "idempotency_key": key,
        "correlation": dict(correlation),
        "subject": dict(subject),
        "privacy": dict(privacy),
        "payload": safe_payload,
    }


__all__ = ["TelemetrySensitiveFieldError", "build_event", "canonical_payload_hash", "redact_for_telemetry"]
