"""Degraded ("inferred") call records for client formats the adapters cannot read yet.

Desktop AI clients change their private storage and hook formats with their own
updates. When an adapter sees that a tool call happened but cannot tell which
Skill / MCP tool it was, it records one ``unknown`` / ``inferred`` call instead
of dropping it or stopping the whole source. The call is still counted per
employee; once the collector is updated, re-reading the native source adds the
exact record next to it (see the replay in the workers).

These records never carry content: only the client, a short format hint, the
time and stable identifiers.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

from cowork_contracts import ComponentEventV1

UNRECOGNIZED_PREFIX = "unrecognized:"
_HINT = re.compile(r"[^A-Za-z0-9_.:\-]+")


def _hint(value: object) -> str:
    text = _HINT.sub("_", str(value or "call")).strip("_") or "call"
    return text[:96]


def degraded_event(
    *,
    runtime: str,
    plugin_id: str,
    installation_id: str,
    session_id: str,
    run_id: str,
    native_call: str,
    hint: object,
    occurred_at: datetime,
    source_type: str,
    evidence_source: str,
    binding: Any = None,
) -> ComponentEventV1:
    """One terminal, low-evidence call record for an unreadable native call."""
    digest = hashlib.sha256(f"{installation_id}:{session_id}:{native_call}".encode()).hexdigest()
    return ComponentEventV1(
        schema_version="component_event.v1",
        event_id=f"{runtime}-degraded:{digest}",
        invocation_id=f"degraded:{digest[:48]}",
        run_id=run_id,
        runtime=runtime,
        plugin_id=plugin_id,
        component_kind="unknown",
        component_id=UNRECOGNIZED_PREFIX + _hint(hint),
        stage="completed",
        occurred_at=occurred_at,
        started_at=None,
        ended_at=occurred_at,
        observation_kind="unrecognized_call",
        evidence_source=evidence_source,
        evidence_level="inferred",
        installation_id=installation_id,
        tenant_id=binding.tenant_id if binding is not None else None,
        user_id=binding.user_id if binding is not None else None,
        source_type=source_type,
    )


def is_degraded(event: ComponentEventV1) -> bool:
    return event.component_kind == "unknown" and event.evidence_level == "inferred"


__all__ = ["UNRECOGNIZED_PREFIX", "degraded_event", "is_degraded"]
