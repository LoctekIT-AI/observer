"""Atomic local installation-state manager for the plugin bundle manifest."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .compatibility import PluginBundleError, validate_bundle_manifest, validate_installed_bundle_manifest


class PluginInstallError(RuntimeError):
    """The requested installation transition could not be completed."""


@dataclass(frozen=True)
class PluginInstallResult:
    changed: bool
    bundle_version: str | None


class PluginInstaller:
    """Manage one private installation manifest without touching source evidence."""

    def __init__(self, install_root: str | Path) -> None:
        self.install_root = Path(install_root).expanduser().resolve()
        self.manifest_path = self.install_root / "manifest.json"

    def current(self) -> dict[str, Any] | None:
        if not self.manifest_path.exists():
            return None
        try:
            return validate_installed_bundle_manifest(json.loads(self.manifest_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, PluginBundleError) as exc:
            raise PluginInstallError("installed plugin manifest is invalid") from exc

    def install(self, manifest: Mapping[str, Any]) -> PluginInstallResult:
        try:
            normalized = validate_bundle_manifest(dict(manifest))
        except PluginBundleError as exc:
            raise PluginInstallError(f"plugin manifest is invalid: {exc}") from exc
        current = self.current()
        if current == normalized:
            return PluginInstallResult(changed=False, bundle_version=normalized["bundle_version"])
        self.install_root.mkdir(parents=True, exist_ok=True)
        self._atomic_write(normalized)
        return PluginInstallResult(changed=True, bundle_version=normalized["bundle_version"])

    def uninstall(self) -> PluginInstallResult:
        if not self.manifest_path.exists():
            return PluginInstallResult(changed=False, bundle_version=None)
        try:
            self.manifest_path.unlink()
        except OSError as exc:
            raise PluginInstallError("cannot remove installation manifest") from exc
        try:
            self.install_root.rmdir()
        except OSError:
            # A non-empty directory could contain only installer-owned future
            # state; it is intentionally left intact rather than deleting any
            # unknown file or source evidence.
            pass
        return PluginInstallResult(changed=True, bundle_version=None)

    def _atomic_write(self, manifest: Mapping[str, Any]) -> None:
        descriptor, raw_path = tempfile.mkstemp(prefix=".manifest-", suffix=".json", dir=self.install_root)
        temporary = Path(raw_path)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.manifest_path)
        except OSError as exc:
            raise PluginInstallError("cannot write installation manifest") from exc
        finally:
            if temporary.exists():
                temporary.unlink()


__all__ = ["PluginInstallError", "PluginInstallResult", "PluginInstaller"]
