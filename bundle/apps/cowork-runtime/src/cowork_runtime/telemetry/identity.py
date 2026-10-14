"""Protected configuration boundary for non-identifying tenant/user pseudonyms."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


_PSEUDONYM = re.compile(r"^(tenant|user)_[A-Za-z0-9_-]{16,120}$")
_KEYS = ("AIHUB_TENANT_PSEUDONYM", "AIHUB_USER_PSEUDONYM")


def validate_pseudonym(value: str, *, prefix: str) -> str:
    normalized = value.strip()
    if not _PSEUDONYM.fullmatch(normalized) or not normalized.startswith(f"{prefix}_"):
        raise ValueError(f"{prefix} pseudonym must use the {prefix}_<opaque-token> format")
    return normalized


def is_pseudonym(value: object, *, prefix: str | None = None) -> bool:
    if not isinstance(value, str) or _PSEUDONYM.fullmatch(value) is None:
        return False
    return prefix is None or value.startswith(f"{prefix}_")


def _values_from_config() -> dict[str, str]:
    path = os.environ.get("LANGFUSE_CONFIG_FILE", "").strip()
    if not path:
        return {}
    try:
        lines = Path(path).expanduser().read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"observer identity configuration is unavailable: {type(exc).__name__}") from exc
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() in _KEYS:
            values[key.strip()] = value.strip()
    for key in _KEYS:
        direct = os.environ.get(key, "").strip()
        if direct:
            values[key] = direct
    return values


@dataclass(frozen=True)
class ObserverIdentity:
    tenant_pseudonym: str
    user_pseudonym: str

    @classmethod
    def from_env(cls) -> "ObserverIdentity | None":
        values = _values_from_config()
        tenant = values.get("AIHUB_TENANT_PSEUDONYM", "")
        user = values.get("AIHUB_USER_PSEUDONYM", "")
        if not tenant and not user:
            return None
        if not tenant or not user:
            raise ValueError("observer identity requires both tenant and user pseudonyms")
        return cls(
            tenant_pseudonym=validate_pseudonym(tenant, prefix="tenant"),
            user_pseudonym=validate_pseudonym(user, prefix="user"),
        )


__all__ = ["ObserverIdentity", "is_pseudonym", "validate_pseudonym"]
