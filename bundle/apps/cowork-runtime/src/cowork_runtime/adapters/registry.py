"""Durable local registration, installation pairing, and adapter cursors."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cowork_contracts import (
    COMPONENT_EVENT_CONTRACT_VERSION,
    InstallationBindingV1,
    PluginManifestV1,
)

from ..persistence.ports import RuntimeRecordStore
from ..telemetry.identity import validate_pseudonym


PAIRING_CHALLENGE_TTL = timedelta(minutes=5)
_WORKBUDDY_DESKTOP_PLUGIN_ID = "workbuddy-desktop-adapter"
_WORKBUDDY_OFFICIAL_SOURCE_CAPABILITY = "source:official-workbuddy-command-hook"


class PluginRegistrationError(ValueError):
    """The requested plugin installation cannot be registered."""


class PluginNotActiveError(RuntimeError):
    """An adapter operation requires an enabled registered installation."""


class PairingError(ValueError):
    """A pairing challenge or identity binding is invalid."""


@dataclass(frozen=True)
class PairingChallenge:
    token: str
    installation_id: str
    expires_at: datetime


@dataclass(frozen=True)
class CodexCursor:
    installation_id: str
    source_path: str
    file_identity: str
    offset: int
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.installation_id.strip() or self.installation_id != self.installation_id.strip():
            raise ValueError("installation_id must be a non-empty canonical identifier")
        if not self.source_path.strip():
            raise ValueError("source_path must be non-empty")
        if not self.file_identity.strip():
            raise ValueError("file_identity must be non-empty")
        if self.offset < 0:
            raise ValueError("cursor offset must be non-negative")
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ValueError("cursor timestamp must include timezone")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return value.astimezone(timezone.utc)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class PluginRegistry:
    """Control-plane state for local adapter installations.

    The registry delegates all durable writes to the Runtime record store so
    the adapter layer never creates a second persistence ledger.
    """

    def __init__(self, store: RuntimeRecordStore) -> None:
        self.store = store

    def register(self, manifest: PluginManifestV1, *, now: datetime | None = None) -> PluginManifestV1:
        self._validate_workbuddy_manifest(manifest)
        current = self.store.get_adapter_plugin(manifest.plugin_id, manifest.installation_id)
        if current is not None and bool(current.enabled):
            raise PluginRegistrationError(
                f"active plugin installation already registered: {manifest.plugin_id}/{manifest.installation_id}"
            )
        timestamp = _as_utc(now or _utc_now())
        self.store.save_adapter_plugin(manifest, now=timestamp)
        return manifest.model_copy(deep=True)

    @staticmethod
    def _validate_workbuddy_manifest(manifest: PluginManifestV1) -> None:
        """Keep unverified WorkBuddy sources outside the official registry."""

        if manifest.runtime != "workbuddy":
            return
        capabilities = set(manifest.capabilities)
        if (
            manifest.plugin_id != _WORKBUDDY_DESKTOP_PLUGIN_ID
            or "desktop" not in capabilities
            or _WORKBUDDY_OFFICIAL_SOURCE_CAPABILITY not in capabilities
            or "web" in capabilities
        ):
            raise PluginRegistrationError(
                "only the official WorkBuddy Desktop command-hook source may be registered"
            )
        source_capabilities = {value for value in capabilities if value.startswith("source:")}
        if source_capabilities != {_WORKBUDDY_OFFICIAL_SOURCE_CAPABILITY}:
            raise PluginRegistrationError(
                "only the official WorkBuddy Desktop command-hook source may be registered"
            )

    def manifest(self, plugin_id: str, installation_id: str) -> PluginManifestV1:
        manifest = self.store.get_adapter_plugin(plugin_id, installation_id)
        if manifest is None:
            raise PluginNotActiveError(f"plugin installation is not registered: {plugin_id}/{installation_id}")
        return manifest.model_copy(deep=True)

    def set_enabled(
        self,
        plugin_id: str,
        installation_id: str,
        enabled: bool,
        *,
        now: datetime | None = None,
    ) -> PluginManifestV1:
        manifest = self.manifest(plugin_id, installation_id)
        timestamp = _as_utc(now or _utc_now())
        self.store.set_adapter_enabled(plugin_id, installation_id, enabled, now=timestamp)
        return manifest.model_copy(update={"enabled": enabled})

    def _require_active(self, installation_id: str) -> PluginManifestV1:
        rows = self.store.find_active_adapter_installation(installation_id)
        if rows is None:
            raise PluginNotActiveError(f"adapter installation is not active: {installation_id}")
        return rows

    def create_pairing_challenge(
        self,
        installation_id: str,
        *,
        now: datetime | None = None,
    ) -> PairingChallenge:
        self._require_active(installation_id)
        created_at = _as_utc(now or _utc_now())
        expires_at = created_at + PAIRING_CHALLENGE_TTL
        token = secrets.token_urlsafe(32)
        self.store.create_adapter_pairing_challenge(
            _digest(token),
            installation_id,
            expires_at,
            created_at=created_at,
        )
        return PairingChallenge(token=token, installation_id=installation_id, expires_at=expires_at)

    def pair(
        self,
        challenge: PairingChallenge,
        user_id: str,
        tenant_id: str,
        *,
        now: datetime | None = None,
    ) -> InstallationBindingV1:
        self._require_active(challenge.installation_id)
        if not user_id or user_id != user_id.strip() or not tenant_id or tenant_id != tenant_id.strip():
            raise PairingError("user_id and tenant_id must be non-empty canonical identifiers")
        current = _as_utc(now or _utc_now())
        row = self.store.pair_adapter_installation(
            _digest(challenge.token),
            challenge.installation_id,
            user_id,
            tenant_id,
            now=current,
        )
        if row is None:
            raise PairingError("pairing challenge is unknown")
        if row["installation_id"] != challenge.installation_id:
            raise PairingError("pairing challenge installation mismatch")
        state = row.get("state")
        if state == "consumed":
            raise PairingError("pairing challenge is already consumed")
        if state == "expired":
            raise PairingError("pairing challenge is expired")
        if state == "installation_mismatch":
            raise PairingError("pairing challenge installation mismatch")
        binding = InstallationBindingV1(
            installation_id=challenge.installation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            status="paired",
        )
        return binding.model_copy(deep=True)

    def unbind(self, installation_id: str, *, now: datetime | None = None) -> None:
        self._require_active(installation_id)
        self.store.clear_adapter_binding(installation_id, now=_as_utc(now or _utc_now()))

    def bind_pseudonyms(self, installation_id: str, *, tenant_pseudonym: str, user_pseudonym: str, now: datetime | None = None) -> InstallationBindingV1:
        """Bind a local installation to opaque customer identifiers."""
        self._require_active(installation_id)
        try:
            tenant_pseudonym = validate_pseudonym(tenant_pseudonym, prefix="tenant")
            user_pseudonym = validate_pseudonym(user_pseudonym, prefix="user")
        except ValueError as exc:
            raise PairingError(str(exc)) from exc
        binding = InstallationBindingV1(
            installation_id=installation_id,
            user_id=user_pseudonym,
            tenant_id=tenant_pseudonym,
            status="paired",
        )
        self.store.save_adapter_binding(binding, now=_as_utc(now or _utc_now()))
        return binding.model_copy(deep=True)

    def binding(self, installation_id: str) -> InstallationBindingV1 | None:
        binding = self.store.get_adapter_binding(installation_id)
        return binding.model_copy(deep=True) if binding is not None else None

    def cursor(self, installation_id: str, source_path: str) -> CodexCursor | None:
        self._require_active(installation_id)
        row = self.store.get_adapter_cursor(installation_id, str(Path(source_path)))
        if row is None:
            return None
        return CodexCursor(
            installation_id=str(row["installation_id"]),
            source_path=str(row["source_path"]),
            file_identity=str(row["file_identity"]),
            offset=int(row["offset"]),
            updated_at=_as_utc(datetime.fromisoformat(str(row["updated_at"]))),
        )

    def save_cursor(self, cursor: CodexCursor) -> None:
        self._require_active(cursor.installation_id)
        self.store.save_adapter_cursor(cursor)


__all__ = [
    "CodexCursor",
    "PAIRING_CHALLENGE_TTL",
    "PairingChallenge",
    "PairingError",
    "PluginNotActiveError",
    "PluginRegistrationError",
    "PluginRegistry",
]
