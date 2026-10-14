"""Deterministic progress evaluation for model-orchestrated crawling."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from cowork_runtime.contracts import ToolCall, ToolResult
from cowork_runtime.contracts.enums import ToolCallStatus


CRAWLER_PROGRESS_METADATA_KEY = "crawler_progress"


def evaluate_crawler_progress(
    call: ToolCall,
    result: ToolResult,
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return a monotonic, secret-free progress snapshot for crawler calls.

    A successful HTTP/tool response is not automatically progress.  New page,
    evidence, cursor, candidate or validated-record identities are required.
    """

    if not (call.tool_name.startswith("cowork.web") or call.tool_name.startswith("cowork.crawler")):
        return None
    output = result.output if isinstance(result.output, Mapping) else {}
    if not isinstance(output, Mapping):
        output = {}
    previous = previous if isinstance(previous, Mapping) else {}
    observed_identities = {
        "pages": _identities(output.get("pages"), ("url", "final_url", "content_hash", "page_id")),
        "evidence": _identities(output.get("evidence_refs"), ("ref_id", "evidence_ref", "id")),
        "cursors": _identities(output.get("next_page", output.get("pagination")), ("cursor", "next_cursor", "page_ref")),
        "candidates": _identities(output.get("candidates"), ("candidate_ref", "record_id", "id")),
        "records": _identities(output.get("extraction_records", output.get("record_samples")), ("record_id", "id")),
    }
    previous_identities = previous.get("identities") if isinstance(previous.get("identities"), Mapping) else {}
    # The provider may return only the latest page.  Keep the progress ledger
    # monotonic by unioning identities across tool results instead of treating
    # each response as a replacement snapshot.
    identities = {
        key: sorted(
            set(observed_identities[key])
            | {
                str(item)[:256]
                for item in (previous_identities.get(key) or [])
                if isinstance(item, str) and item.strip()
            }
        )
        for key in observed_identities
    }
    counts = {key: len(value) for key, value in identities.items()}
    old_counts = previous.get("counts") if isinstance(previous.get("counts"), Mapping) else {}
    new_items = sum(max(0, counts[key] - int(old_counts.get(key) or 0)) for key in counts)
    coverage = _coverage(output)
    old_coverage = float(previous.get("coverage") or 0.0)
    coverage_improved = coverage > old_coverage
    status = "advanced" if new_items > 0 or coverage_improved else "no_progress"
    snapshot = {
        "version": "crawler-progress.v1",
        "tool_name": call.tool_name,
        "last_tool_call_id": call.tool_call_id,
        "status": status,
        "counts": counts,
        "identities": identities,
        "new_items": new_items,
        "coverage": coverage,
        "coverage_improved": coverage_improved,
        "result_status": result.status.value,
        "fingerprint": _fingerprint(identities, coverage),
    }
    return snapshot


def _identities(value: object, fields: tuple[str, ...]) -> list[str]:
    if isinstance(value, Mapping):
        value = [value]
    if not isinstance(value, list):
        return []
    values: list[str] = []
    for item in value[:5000]:
        if isinstance(item, str) and item.strip():
            values.append(item.strip()[:256])
            continue
        if not isinstance(item, Mapping):
            continue
        for field in fields:
            candidate = item.get(field)
            if candidate is not None and str(candidate).strip():
                values.append(str(candidate).strip()[:256])
                break
    return sorted(set(values))


def _coverage(output: Mapping[str, Any]) -> float:
    for key in ("extraction_coverage", "coverage"):
        value = output.get(key)
        if isinstance(value, Mapping):
            ratio = value.get("ratio")
            if isinstance(ratio, (int, float)):
                return max(0.0, min(1.0, float(ratio)))
    return 0.0


def _fingerprint(identities: Mapping[str, Any], coverage: float) -> str:
    payload = json.dumps({"identities": identities, "coverage": coverage}, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
