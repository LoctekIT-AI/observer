"""Deterministic redaction for persisted diagnostics and trace exports."""

from __future__ import annotations

import hashlib
import json
from typing import Any

SECRET_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "bearer",
    "cookie",
    "password",
    "refresh_token",
    "secret",
    "token",
}
CONTENT_KEYS = {
    "content",
    "content_base64",
    "data_base64",
    "script",
    "stderr",
    "stdout",
    "text",
}


def redact_sensitive(
    value: Any,
    *,
    key: str | None = None,
    _parent_key: str | None = None,
) -> Any:
    normalized_key = (key or "").lower()
    if normalized_key in SECRET_KEYS or any(part in normalized_key for part in ("password", "secret", "token", "api_key")):
        return "[REDACTED]"
    if normalized_key == "code" and _parent_key != "error" and value is not None and value != "":
        return _content_digest(value)
    if normalized_key in CONTENT_KEYS and value is not None and value != "":
        return _content_digest(value)
    if isinstance(value, dict):
        return {
            str(item_key): redact_sensitive(
                item_value,
                key=str(item_key),
                _parent_key=normalized_key or _parent_key,
            )
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive(item) for item in value]
    return value


def trace_event_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    redacted = redact_sensitive(payload)
    if not isinstance(redacted, dict):
        return {}
    # Trace exports describe flow and outcome. User-visible prose remains in the
    # event API but is represented only by a digest in diagnostic exports.
    if event_type == "turn.assistant_text" and "content" in payload:
        redacted["content"] = _content_digest(payload.get("content"))
    return redacted


def _content_digest(value: Any) -> dict[str, Any]:
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode("utf-8")
    else:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return {
        "redacted": True,
        "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
