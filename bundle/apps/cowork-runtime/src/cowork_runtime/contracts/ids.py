"""Identifier helpers for runtime contracts."""

from __future__ import annotations

from uuid import uuid4


def new_contract_id(prefix: str) -> str:
    normalized = prefix.strip().replace("-", "_")
    if not normalized:
        raise ValueError("id prefix must not be empty")
    return f"{normalized}_{uuid4().hex}"
