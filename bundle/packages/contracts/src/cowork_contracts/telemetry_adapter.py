"""Strict mapping boundary for external Agent telemetry sources."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Mapping, NotRequired, TypedDict

_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_COMPONENT_KINDS = frozenset({"plugin", "skill", "mcp_server", "mcp_tool", "tool", "function", "connector", "workflow", "agent", "builtin", "other", "unknown"})
_STAGES = frozenset({"dispatched", "policy_checked", "result"})
_STATUSES = frozenset({"pending", "running", "completed", "failed", "timeout", "cancelled", "rejected", "unknown"})
_SUBJECT_SCOPES = frozenset({"tenant", "workspace", "personal", "anonymous", "unknown"})
_SENSITIVE_KEYS = frozenset({"prompt", "output", "arguments", "input", "stdout", "stderr", "sql", "api_key", "token", "cookie", "password", "secret", "body"})


class SourceComponentMapping(TypedDict):
    source_component_type: str
    component_id: str
    kind: str
    version: NotRequired[str | None]
    manifest_hash: NotRequired[str | None]


class TelemetryMappingError(ValueError):
    """A source event cannot be mapped without inventing or leaking facts."""


def _scan_sensitive(value: Any, path: str = "source") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in _SENSITIVE_KEYS or any(part in normalized for part in ("password", "secret", "token", "api_key")):
                raise TelemetryMappingError(f"sensitive source field: {path}.{key}")
            _scan_sensitive(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _scan_sensitive(child, f"{path}[{index}]")


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TelemetryMappingError(f"{field} is required")
    return value.strip()


def _opaque(value: Any, field: str) -> str:
    candidate = _required_text(value, field)
    if len(candidate) > 256 or _OPAQUE_ID.fullmatch(candidate) is None:
        raise TelemetryMappingError(f"{field} must be an opaque identifier")
    return candidate


def _timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise TelemetryMappingError("occurred_at must be an RFC3339 timestamp") from exc
    else:
        raise TelemetryMappingError("occurred_at is required")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TelemetryMappingError("occurred_at must include a timezone")
    rendered = parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return rendered.replace(".000000Z", "Z")


def _producer_instance(producer: Mapping[str, Any]) -> str:
    candidate = producer.get("instance_pseudonym")
    if candidate is not None:
        return _opaque(candidate, "producer.instance_pseudonym")
    product = _required_text(producer.get("product"), "producer.product")
    runtime_kind = _required_text(producer.get("runtime_kind"), "producer.runtime_kind")
    return "external-instance-" + hashlib.sha256(f"{product}:{runtime_kind}".encode("utf-8")).hexdigest()[:32]


def _subject(source: Mapping[str, Any]) -> dict[str, Any]:
    raw = source.get("subject")
    if not isinstance(raw, Mapping):
        return {"scope": "anonymous"}
    scope = raw.get("scope", "anonymous")
    if not isinstance(scope, str) or scope not in _SUBJECT_SCOPES:
        raise TelemetryMappingError("subject.scope is invalid")
    result: dict[str, Any] = {"scope": scope}
    for key in ("tenant_pseudonym", "workspace_pseudonym", "user_pseudonym"):
        if raw.get(key) is not None:
            result[key] = _opaque(raw[key], f"subject.{key}")
    return result


def map_source_event(source: Mapping[str, Any], *, producer: Mapping[str, Any], mapping: SourceComponentMapping) -> dict[str, Any]:
    """Map one explicit external component event to metadata-only v1 telemetry."""
    if not isinstance(source, Mapping) or not isinstance(producer, Mapping) or not isinstance(mapping, Mapping):
        raise TypeError("source, producer and component mapping must be objects")
    _scan_sensitive(source)
    _required_text(mapping.get("source_component_type"), "source_component_type")
    component_id = _required_text(mapping.get("component_id"), "component_id")
    kind = _required_text(mapping.get("kind"), "kind")
    if kind not in _COMPONENT_KINDS:
        raise TelemetryMappingError(f"unsupported component kind: {kind}")
    version = mapping.get("version") or "unknown"
    if not isinstance(version, str) or len(version) > 128:
        raise TelemetryMappingError("version must be a string or omitted")
    manifest_hash = mapping.get("manifest_hash")
    if manifest_hash is not None and _DIGEST.fullmatch(_required_text(manifest_hash, "manifest_hash")) is None:
        raise TelemetryMappingError("manifest_hash must be a sha256 digest")
    source_event_id = _opaque(source.get("source_event_id", source.get("event_id")), "source_event_id")
    stage = _required_text(source.get("stage"), "stage")
    if stage not in _STAGES:
        raise TelemetryMappingError(f"unsupported component stage: {stage}")
    status = source.get("status", "unknown")
    if not isinstance(status, str) or status not in _STATUSES:
        raise TelemetryMappingError(f"unsupported component status: {status}")
    instance = _producer_instance(producer)
    idempotency_key = f"{instance}:{source_event_id}:{stage}"
    correlation: dict[str, Any] = {"source_event_id": source_event_id, "invocation_id": _opaque(source.get("invocation_id", source_event_id), "invocation_id")}
    if source.get("run_id") is not None:
        correlation["run_id"] = _opaque(source["run_id"], "run_id")
    component: dict[str, Any] = {"id": component_id, "kind": kind, "version": version}
    if manifest_hash is not None:
        component["manifest_hash"] = str(manifest_hash)
    payload: dict[str, Any] = {"component": component, "stage": stage, "status": status}
    for key in ("retry_count", "duration_ms"):
        if key in source:
            value = source[key]
            if not isinstance(value, int) or value < 0:
                raise TelemetryMappingError(f"{key} must be a non-negative integer")
            payload[key] = value
    if source.get("error_code") is not None:
        payload["error_code"] = _required_text(source["error_code"], "error_code")
    event_id = "telemetry-" + hashlib.sha256(f"component.lifecycle:{idempotency_key}".encode("utf-8")).hexdigest()
    return {"event_id": event_id, "event_type": "component.lifecycle", "occurred_at": _timestamp(source.get("occurred_at")), "idempotency_key": idempotency_key, "correlation": correlation, "subject": _subject(source), "privacy": {"capture_mode": "aggregate_only", "content_capture": "metadata_only"}, "payload": payload}


__all__ = ["SourceComponentMapping", "TelemetryMappingError", "map_source_event"]
