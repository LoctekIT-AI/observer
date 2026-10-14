"""Durable context-window projection used by compaction and recovery."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, MutableMapping

from cowork_runtime.contracts.ids import new_contract_id


CONTEXT_WINDOW_METADATA_KEY = "context_window"


def record_compaction_window(
    metadata: MutableMapping[str, Any],
    *,
    compaction_id: str | None,
    tokens_before: int,
    tokens_after: int,
) -> dict[str, Any]:
    """Advance the window only when a new compaction was actually applied."""

    current = metadata.get(CONTEXT_WINDOW_METADATA_KEY)
    current = current if isinstance(current, dict) else {}
    last_compaction = current.get("last_compaction_id")
    if not compaction_id or compaction_id == last_compaction:
        return dict(current)
    previous_id = current.get("window_id")
    window_number = max(0, int(current.get("window_number") or 0)) + 1
    state = {
        "version": "context-window.v1",
        "window_number": window_number,
        "first_window_id": current.get("first_window_id") or new_contract_id("ctxwin"),
        "previous_window_id": previous_id,
        "window_id": new_contract_id("ctxwin"),
        "last_compaction_id": compaction_id,
        "prefill_input_tokens": max(0, int(tokens_after)),
        "tokens_before": max(0, int(tokens_before)),
        "tokens_after": max(0, int(tokens_after)),
        "auto_compact_fallback_claimed": bool(current.get("auto_compact_fallback_claimed", False)),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    metadata[CONTEXT_WINDOW_METADATA_KEY] = state
    return state
