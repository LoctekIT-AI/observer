"""Shared structured-intent parsing and resolution contracts.

The model may propose domain semantics, but this module never grants an
execution permission.  Domain code must still validate trusted facts and
compile its own policy after a successful parse.
"""

from __future__ import annotations

import json
import re
from typing import Any, Generic, Literal, Mapping, TypeVar

from pydantic import BaseModel, ConfigDict, Field


STRUCTURED_INTENT_RESOLUTION_VERSION = "cowork.structured-intent-resolution.v1"

IntentPayloadT = TypeVar("IntentPayloadT", bound=BaseModel)


class StructuredIntentResolution(BaseModel, Generic[IntentPayloadT]):
    """Transport-neutral result of one strict intent classification."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["cowork.structured-intent-resolution.v1"] = (
        STRUCTURED_INTENT_RESOLUTION_VERSION
    )
    domain: str = Field(min_length=1, max_length=96)
    status: Literal["resolved", "not_requested", "unresolved", "unavailable"]
    payload: IntentPayloadT | None = None
    reason_code: str = Field(min_length=1, max_length=160)
    source: Literal["model", "deterministic", "fallback"]


def parse_structured_intent(
    raw: object,
    *,
    domain: str,
    payload_model: type[IntentPayloadT],
    invalid_reason_code: str,
) -> StructuredIntentResolution[IntentPayloadT]:
    """Parse a strict JSON object into a domain payload.

    Markdown fences are accepted as transport decoration. Unknown or aliased
    business fields are not normalized: schema drift remains observable and
    resolves to ``unresolved``.
    """

    value = _json_object(raw)
    if value is None:
        return unresolved_intent(domain=domain, reason_code=invalid_reason_code)
    try:
        payload = payload_model.model_validate(value)
    except (TypeError, ValueError):
        return unresolved_intent(domain=domain, reason_code=invalid_reason_code)
    return StructuredIntentResolution(
        domain=domain,
        status="resolved",
        payload=payload,
        reason_code=f"{domain}.resolved",
        source="model",
    )


def unresolved_intent(
    *,
    domain: str,
    reason_code: str,
    source: Literal["model", "deterministic", "fallback"] = "model",
    unavailable: bool = False,
) -> StructuredIntentResolution[Any]:
    return StructuredIntentResolution(
        domain=domain,
        status="unavailable" if unavailable else "unresolved",
        payload=None,
        reason_code=reason_code,
        source=source,
    )


def _json_object(raw: object) -> Mapping[str, Any] | None:
    if isinstance(raw, Mapping):
        return raw
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, Mapping) else None


__all__ = [
    "STRUCTURED_INTENT_RESOLUTION_VERSION",
    "StructuredIntentResolution",
    "parse_structured_intent",
    "unresolved_intent",
]
