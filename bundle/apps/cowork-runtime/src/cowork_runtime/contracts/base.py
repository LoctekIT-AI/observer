"""Small base helpers for transport-neutral contract models."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ContractModel(BaseModel):
    """Base class for JSON contracts.

    Contracts intentionally forbid unknown fields so transport adapters fail
    fast when local and remote runtime versions drift.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
