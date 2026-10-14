"""Safe archive installation for the local-client plugin bundle."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import struct
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .compatibility import PluginBundleError, validate_bundle_manifest, validate_customer_bundle_manifest, validate_installed_bundle_manifest


_REQUIRED_MEMBERS = frozenset({
    "manifest.json",
    "packages/dsh-observer/package.json",
    "packages/dsh-observer/index.mjs",
    "packages/dsh-observer/cordis.patch.yml",
    "packages/dsh-observer/README.md",
    "scripts/dsh_observer_worker.py",
    "scripts/install_dsh_observer.py",
    "scripts/install_dsh_observer_launch_agent.py",
    "apps/cowork-runtime/src/cowork_runtime/adapters/dsh.py",
    "apps/cowork-runtime/src/cowork_runtime/observer/dsh_worker.py",

    ".codex-plugin/plugin.json",
    ".codebuddy-plugin/plugin.json",
    "hooks/hooks.json",
    "scripts/workbuddy_hook_bridge.py",
    "scripts/observer_collect.py",
    "scripts/codex_observer_worker.py",
    "scripts/claude_observer_worker.py",
    "scripts/workbuddy_observer_worker.py",
    "scripts/install_observer_launch_agent.py",
    "scripts/install_claude_observer_launch_agent.py",
    "scripts/install_workbuddy_observer_launch_agent.py",
    "apps/cowork-runtime/src/cowork_runtime/adapters/workbuddy_desktop.py",
    "apps/cowork-runtime/src/cowork_runtime/adapters/codex.py",
    "apps/cowork-runtime/src/cowork_runtime/adapters/claude.py",
    "apps/cowork-runtime/src/cowork_runtime/adapters/registry.py",
    "apps/cowork-runtime/src/cowork_runtime/adapters/delivery.py",
    "apps/cowork-runtime/src/cowork_runtime/telemetry/langfuse.py",
    "apps/cowork-runtime/src/cowork_runtime/telemetry/identity.py",
    "apps/cowork-runtime/src/cowork_runtime/telemetry/keychain.py",
    "apps/cowork-runtime/src/cowork_runtime/telemetry/emitter.py",
    "apps/cowork-runtime/src/cowork_runtime/telemetry/redaction.py",
    "apps/cowork-runtime/src/cowork_runtime/telemetry/sdk.py",
    "apps/cowork-runtime/src/cowork_runtime/telemetry/uploader.py",
    "apps/cowork-runtime/src/cowork_runtime/observer/__init__.py",
    "apps/cowork-runtime/src/cowork_runtime/observer/codex_worker.py",
    "apps/cowork-runtime/src/cowork_runtime/observer/claude_worker.py",
    "apps/cowork-runtime/src/cowork_runtime/observer/workbuddy_worker.py",
    "apps/cowork-runtime/src/cowork_runtime/dataset_quality.py",
    "packages/contracts/src/cowork_contracts/__init__.py",
    "apps/cowork-runtime/src/cowork_runtime/contracts/__init__.py",
    "apps/cowork-runtime/src/cowork_runtime/loop/__init__.py",
    "apps/cowork-runtime/src/cowork_runtime/persistence/__init__.py",
    "apps/cowork-runtime/src/cowork_runtime/plugin_package/__init__.py",
    "apps/cowork-runtime/src/cowork_runtime/plugin_package/compatibility.py",
    "apps/cowork-runtime/src/cowork_runtime/plugin_package/archive_installer.py",
    "scripts/observer_doctor.py",
    "scripts/install_observer_bundle.py",
    "packages/contracts/src/cowork_contracts/component_event.py",
    "packages/contracts/src/cowork_contracts/telemetry.py",
    "packages/contracts/src/cowork_contracts/telemetry_adapter.py",
})
_CUSTOMER_REQUIRED_MEMBERS = frozenset({"manifest.json", ".codex-plugin/plugin.json", "bin/codex-observer"})


class PluginArchiveInstallError(RuntimeError):
    """An archive cannot be safely installed without preserving the prior state."""


@dataclass(frozen=True)
class PluginArchiveInstallResult:
    changed: bool
    bundle_version: str
    install_root: Path


class PluginArchiveInstaller:
    """Validate, extract and atomically activate one bundle archive."""

    def __init__(self, install_root: str | Path) -> None:
        self.install_root = Path(install_root).expanduser().resolve()
        self.manifest_path = self.install_root / "manifest.json"
        self.previous_root = self.install_root.parent / f".{self.install_root.name}.previous"

    def current(self) -> dict[str, Any] | None:
        if not self.manifest_path.is_file():
            return None
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            manifest = validate_customer_bundle_manifest(raw) if isinstance(raw, dict) and raw.get("profile") == "customer-codex" else validate_installed_bundle_manifest(raw)
            if manifest.get("profile") == "customer-codex":
                self._validate_customer_runtime(self.install_root, manifest)
            return manifest
        except (OSError, json.JSONDecodeError, PluginBundleError) as exc:
            raise PluginArchiveInstallError("installed bundle manifest is invalid") from exc

    def install(self, archive_path: str | Path) -> PluginArchiveInstallResult:
        archive = Path(archive_path).expanduser().resolve()
        if not archive.is_file():
            raise PluginArchiveInstallError("bundle archive is not a regular file")
        with tempfile.TemporaryDirectory(prefix="skill-mcp-observer-install-") as raw_temp:
            staged = Path(raw_temp) / "bundle"
            try:
                with zipfile.ZipFile(archive) as bundle:
                    self._validate_members(bundle)
                    bundle.extractall(staged)
                    for item in bundle.infolist():
                        if item.filename == "bin/codex-observer":
                            (staged / item.filename).chmod(0o755)
            except (OSError, zipfile.BadZipFile) as exc:
                raise PluginArchiveInstallError("bundle archive cannot be read") from exc
            manifest = self._load_staged_manifest(staged)
            if manifest.get("profile") == "customer-codex":
                self._validate_customer_runtime(staged, manifest)
            current = self.current()
            if current == manifest:
                return PluginArchiveInstallResult(False, manifest["bundle_version"], self.install_root)
            self._activate(staged)
        return PluginArchiveInstallResult(True, manifest["bundle_version"], self.install_root)

    def rollback(self) -> PluginArchiveInstallResult:
        """Restore the last successfully activated bundle atomically.

        The previous bundle is kept only after a successful activation.  A
        rollback swaps the two directories, so a failed host registration can
        restore the exact files that were running before the attempted update.
        State directories (cursors and outboxes) live outside both roots and
        are therefore never moved or deleted.
        """
        if not self.install_root.is_dir() or not self.previous_root.is_dir():
            raise PluginArchiveInstallError("no previous bundle is available for rollback")
        current = self.current()
        previous_manifest_path = self.previous_root / "manifest.json"
        try:
            previous_raw = json.loads(previous_manifest_path.read_text(encoding="utf-8"))
            previous = (
                validate_customer_bundle_manifest(previous_raw)
                if isinstance(previous_raw, dict) and previous_raw.get("profile") == "customer-codex"
                else validate_installed_bundle_manifest(previous_raw)
            )
        except (OSError, json.JSONDecodeError, PluginBundleError) as exc:
            raise PluginArchiveInstallError("previous bundle manifest is invalid") from exc
        if current is None:
            raise PluginArchiveInstallError("current bundle manifest is missing")

        replacement = self.install_root.parent / f".{self.install_root.name}.rollback-replacement"
        backup = self.install_root.parent / f".{self.install_root.name}.rollback-backup"
        if replacement.exists() or backup.exists():
            raise PluginArchiveInstallError("existing unfinished rollback requires manual inspection")
        try:
            os.replace(self.previous_root, replacement)
            os.replace(self.install_root, backup)
            os.replace(replacement, self.install_root)
        except OSError as exc:
            if backup.exists():
                if self.install_root.exists():
                    shutil.rmtree(self.install_root)
                os.replace(backup, self.install_root)
            if replacement.exists() and not self.previous_root.exists():
                os.replace(replacement, self.previous_root)
            raise PluginArchiveInstallError("cannot rollback bundle archive") from exc
        finally:
            if replacement.exists():
                shutil.rmtree(replacement)
        os.replace(backup, self.previous_root)
        return PluginArchiveInstallResult(True, previous["bundle_version"], self.install_root)

    @staticmethod
    def _validate_members(bundle: zipfile.ZipFile) -> None:
        for item in bundle.infolist():
            member = Path(item.filename)
            if not item.filename or member.is_absolute() or ".." in member.parts:
                raise PluginArchiveInstallError("bundle contains an unsafe path")
            if item.is_dir():
                continue
            mode = item.external_attr >> 16
            if mode and (mode & 0o170000) == 0o120000:
                raise PluginArchiveInstallError("bundle contains a symbolic link")
        names = {item.filename.rstrip("/") for item in bundle.infolist()}
        if len(names) != len(bundle.infolist()):
            raise PluginArchiveInstallError("bundle contains duplicate archive paths")
        try:
            raw_manifest = json.loads(bundle.read("manifest.json"))
        except (KeyError, json.JSONDecodeError) as exc:
            raise PluginArchiveInstallError("bundle manifest is invalid") from exc
        required = _CUSTOMER_REQUIRED_MEMBERS if isinstance(raw_manifest, dict) and raw_manifest.get("profile") == "customer-codex" else _REQUIRED_MEMBERS
        missing = required - names
        if missing:
            raise PluginArchiveInstallError(f"bundle manifest or required files are missing: {sorted(missing)}")
        if required is _CUSTOMER_REQUIRED_MEMBERS:
            extra = names - required
            if extra:
                raise PluginArchiveInstallError(f"customer bundle contains undeclared files: {sorted(extra)}")
            if not bundle.getinfo("bin/codex-observer").external_attr >> 16 & 0o111:
                raise PluginArchiveInstallError("customer runtime executable has no executable permission")

    @staticmethod
    def _load_staged_manifest(staged: Path) -> dict[str, Any]:
        try:
            raw = json.loads((staged / "manifest.json").read_text(encoding="utf-8"))
            return validate_customer_bundle_manifest(raw) if isinstance(raw, dict) and raw.get("profile") == "customer-codex" else validate_bundle_manifest(raw)
        except (OSError, json.JSONDecodeError, PluginBundleError) as exc:
            raise PluginArchiveInstallError("bundle manifest is invalid") from exc

    @staticmethod
    def _validate_customer_runtime(staged: Path, manifest: dict[str, Any]) -> None:
        runtime = staged / manifest["runtime"]["path"]
        try:
            executable_bytes = runtime.read_bytes()
            digest = hashlib.sha256(executable_bytes).hexdigest()
        except OSError as exc:
            raise PluginArchiveInstallError("customer runtime executable cannot be read") from exc
        if digest != manifest["runtime"]["sha256"]:
            raise PluginArchiveInstallError("customer runtime executable hash mismatch")
        executable_platform = None
        if len(executable_bytes) >= 8 and executable_bytes[:4] == b"\xcf\xfa\xed\xfe":
            executable_platform = {0x0100000C: "darwin-arm64", 0x01000007: "darwin-x64"}.get(struct.unpack("<I", executable_bytes[4:8])[0])
        if executable_platform is None:
            raise PluginArchiveInstallError("customer runtime is not a native executable")
        if executable_platform != manifest["runtime"]["platform"]:
            raise PluginArchiveInstallError("customer runtime architecture does not match manifest")
        try:
            plugin = json.loads((staged / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PluginArchiveInstallError("customer plugin manifest is invalid") from exc
        if not isinstance(plugin, dict) or plugin.get("name") != manifest["plugin_id"] or plugin.get("version") != manifest["bundle_version"]:
            raise PluginArchiveInstallError("customer plugin manifest identity does not match bundle")
        if set(plugin) - {"name", "version", "description", "interface"}:
            raise PluginArchiveInstallError("customer plugin manifest contains undeclared capability")

    def _activate(self, staged: Path) -> None:
        parent = self.install_root.parent
        parent.mkdir(parents=True, exist_ok=True)
        replacement = parent / f".{self.install_root.name}.replacement"
        backup = parent / f".{self.install_root.name}.backup"
        if replacement.exists() or backup.exists():
            raise PluginArchiveInstallError("existing unfinished bundle installation requires manual inspection")
        try:
            os.replace(staged, replacement)
            if self.install_root.exists():
                os.replace(self.install_root, backup)
            os.replace(replacement, self.install_root)
        except OSError as exc:
            if backup.exists():
                # The activation may have failed before or after the new root
                # was placed.  Remove only the attempted root, then restore the
                # known-good backup.
                if self.install_root.exists():
                    shutil.rmtree(self.install_root)
                os.replace(backup, self.install_root)
            raise PluginArchiveInstallError("cannot activate bundle archive") from exc
        finally:
            if replacement.exists():
                shutil.rmtree(replacement)
        if backup.exists():
            # Keep one known-good version so a host registration failure can
            # roll back the package without touching customer state.
            if self.previous_root.exists():
                shutil.rmtree(self.previous_root)
            os.replace(backup, self.previous_root)


__all__ = [
    "PluginArchiveInstallError",
    "PluginArchiveInstallResult",
    "PluginArchiveInstaller",
]
