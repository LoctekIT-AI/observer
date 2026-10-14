"""Client version detection and fail-closed manifest range checks."""

from __future__ import annotations

import plistlib
import re
from pathlib import Path


class ClientCompatibilityError(ValueError):
    """A client version cannot be safely mapped to the adapter contract."""


_NUMERIC_VERSION = re.compile(r"(?<!\d)(\d+(?:\.\d+){1,2})(?!\d)")


def normalize_client_version(value: str) -> str:
    """Extract the numeric version prefix from a native client version string."""

    if not isinstance(value, str) or not value.strip():
        raise ClientCompatibilityError("client version is missing")
    match = _NUMERIC_VERSION.search(value)
    if match is None:
        raise ClientCompatibilityError(f"client version is not numeric: {value}")
    parts = tuple(int(part) for part in match.group(1).split("."))
    parts = (*parts, *([0] * (3 - len(parts))))
    return ".".join(str(part) for part in parts)


def _version_tuple(value: str) -> tuple[int, ...]:
    try:
        normalized = normalize_client_version(value)
    except ClientCompatibilityError:
        raise
    return tuple(int(part) for part in normalized.split("."))


def validate_client_version(
    *,
    runtime: str,
    client_version: str,
    minimum: str | None,
    maximum: str | None,
) -> str:
    """Validate a detected version against the bundle's declared range.

    The adapters must stop when a major client format is outside the table.  A
    caller can then surface the error and update the adapter deliberately,
    instead of silently recording incomplete events.
    """

    normalized = normalize_client_version(client_version)
    current = _version_tuple(normalized)
    if minimum is not None and current < _version_tuple(minimum):
        raise ClientCompatibilityError(
            f"{runtime} client version {normalized} is outside supported range"
        )
    if maximum is not None and current > _version_tuple(maximum):
        raise ClientCompatibilityError(
            f"{runtime} client version {normalized} is outside supported range"
        )
    return normalized


def detect_macos_bundle_version(app_path: str | Path) -> str:
    """Read the public macOS bundle version without opening client data files."""

    bundle = Path(app_path).expanduser()
    info_path = bundle / "Contents" / "Info.plist"
    try:
        with info_path.open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        raise ClientCompatibilityError(
            f"cannot read macOS client version: {info_path}"
        ) from exc
    for key in ("CFBundleShortVersionString", "CFBundleVersion"):
        value = info.get(key) if isinstance(info, dict) else None
        if isinstance(value, str) and value.strip():
            return normalize_client_version(value)
    raise ClientCompatibilityError(f"macOS bundle has no usable version: {info_path}")


__all__ = [
    "ClientCompatibilityError",
    "detect_macos_bundle_version",
    "normalize_client_version",
    "validate_client_version",
]
