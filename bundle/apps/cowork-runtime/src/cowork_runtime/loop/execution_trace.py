"""Bounded, append-only execution trace projection for Loop recovery.

Runtime events remain the public event ledger.  This compact projection is
kept in the checkpoint so a resumed Run can recover its last lifecycle and
idempotency facts without replaying unbounded model content.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, MutableMapping
from dataclasses import dataclass


MAX_TRACE_RECORDS = 256
TRACE_METADATA_KEY = "execution_trace"


@dataclass(frozen=True)
class TraceProjection:
    """Safe facts reconstructed from the bounded trace projection."""

    last_kind: str | None
    last_status: str | None
    completed_tool_call_ids: tuple[str, ...]
    failed_tool_call_ids: tuple[str, ...]
    latest_seq: int


def reconstruct_projection(metadata: MutableMapping[str, Any] | Mapping[str, Any]) -> TraceProjection:
    """Rebuild execution facts without trusting model messages or free text.

    The projection is intentionally conservative: it reports completed and
    failed tool IDs but never invents a pending call.  Pending calls remain
    owned by the typed checkpoint fields and must be explicitly persisted by
    the interruption path.
    """

    raw = metadata.get(TRACE_METADATA_KEY)
    records = raw if isinstance(raw, list) else []
    completed: list[str] = []
    failed: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        call_id = payload.get("tool_call_id") if isinstance(payload, dict) else None
        if not isinstance(call_id, str) or not call_id:
            continue
        status = str(record.get("status") or "")
        if status in {"succeeded", "completed"}:
            completed.append(call_id)
        elif status in {"failed", "cancelled"}:
            failed.append(call_id)
    last = records[-1] if records and isinstance(records[-1], dict) else {}
    return TraceProjection(
        last_kind=str(last.get("kind")) if last.get("kind") else None,
        last_status=str(last.get("status")) if last.get("status") else None,
        completed_tool_call_ids=tuple(dict.fromkeys(completed)),
        failed_tool_call_ids=tuple(dict.fromkeys(failed)),
        latest_seq=int(last.get("seq") or 0),
    )


def append_trace(
    metadata: MutableMapping[str, Any],
    *,
    kind: str,
    run_id: str,
    turn_id: str,
    payload: dict[str, Any] | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Append one bounded record and return the normalized record."""

    raw = metadata.get(TRACE_METADATA_KEY)
    records = raw if isinstance(raw, list) else []
    seq = (int(records[-1].get("seq") or 0) + 1) if records and isinstance(records[-1], dict) else 1
    record = {
        "seq": seq,
        "kind": kind,
        "run_id": run_id,
        "turn_id": turn_id,
        "status": status,
        "payload": _safe_payload(payload or {}),
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    records.append(record)
    metadata[TRACE_METADATA_KEY] = records[-MAX_TRACE_RECORDS:]
    return record


def _safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep trace projections JSON-safe and prevent accidental secret storage."""

    blocked = {"authorization", "cookie", "token", "password", "secret", "api_key", "access_token"}
    result: dict[str, Any] = {}
    for key, value in payload.items():
        normalized = str(key).lower()
        if any(item in normalized for item in blocked):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            result[str(key)] = value if not isinstance(value, str) else value[:2048]
        elif isinstance(value, dict):
            result[str(key)] = _safe_payload(value)
        elif isinstance(value, list):
            result[str(key)] = [item for item in value[:32] if isinstance(item, (str, int, float, bool))]
    return result
