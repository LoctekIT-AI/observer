"""Safe token-usage projection shared by local and server runtimes."""

from __future__ import annotations

from collections.abc import Mapping
import math
from numbers import Integral
from typing import Any


TOKEN_USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "cache_read_input_tokens",
    "reasoning_tokens",
    "total_tokens",
)


def public_token_usage(value: Any) -> dict[str, int]:
    """Return only non-negative provider token counts, never raw responses."""

    if not isinstance(value, Mapping):
        return {}
    usage: dict[str, int] = {}
    for field in TOKEN_USAGE_FIELDS:
        count = value.get(field)
        if isinstance(count, bool) or not isinstance(count, Integral) or count < 0:
            continue
        usage[field] = int(count)

    if "total_tokens" not in usage:
        input_tokens = usage.get("prompt_tokens", usage.get("input_tokens", 0))
        output_tokens = usage.get("completion_tokens", usage.get("output_tokens", 0))
        if input_tokens or output_tokens:
            usage["total_tokens"] = input_tokens + output_tokens
    if not any(usage.values()):
        return {}
    return usage


def public_usage_cost(value: Any) -> dict[str, float]:
    """Keep only finite, non-negative cost telemetry for trace compatibility."""

    if not isinstance(value, Mapping):
        return {}
    cost: dict[str, float] = {}
    for field in ("cost_usd", "total_cost_usd"):
        amount = value.get(field)
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            continue
        normalized = float(amount)
        if normalized < 0 or not math.isfinite(normalized):
            continue
        cost[field] = normalized
    return cost
