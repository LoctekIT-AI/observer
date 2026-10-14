"""Strict compatibility checks for the one-install local plugin bundle."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from .client_compatibility import ClientCompatibilityError, normalize_client_version


_PLATFORMS = frozenset({"darwin-arm64", "darwin-x64", "win32-x64"})
_MODES = frozenset({"native", "desktop"})
_RUNTIMES = frozenset({"dsh", "codex", "claude", "workbuddy"})


class PluginBundleError(ValueError):
    """The bundle manifest is malformed or declares an unsupported capability."""


class CompatibilityStatus(StrEnum):
    SUPPORTED = "supported"
    NEEDS_PERMISSION = "needs_permission"
    SOURCE_MISSING = "source_missing"
    CLIENT_UNSUPPORTED = "client_unsupported"
    CONTRACT_INCOMPATIBLE = "contract_incompatible"
    NOT_INSTALLED = "not_installed"


class PathStatus(StrEnum):
    AVAILABLE = "available"
    MISSING = "missing"
    NOT_CHECKED = "not_checked"


class PermissionStatus(StrEnum):
    GRANTED = "granted"
    DENIED = "denied"
    UNKNOWN = "unknown"
    NOT_CHECKED = "not_checked"


@dataclass(frozen=True)
class SourceDiagnostic:
    """Metadata-only result of checking one declared local source."""

    path: str
    path_status: PathStatus
    permission_status: PermissionStatus
    message: str


@dataclass(frozen=True)
class ConnectorCompatibility:
    status: CompatibilityStatus
    reason: str
    used_fallback: bool = False
    diagnostics: tuple[SourceDiagnostic, ...] = ()


@dataclass(frozen=True)
class CompatibilityReport:
    platform: str
    contract_status: CompatibilityStatus
    connectors: dict[str, ConnectorCompatibility]


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PluginBundleError(f"{field} must be a non-empty string")
    return value.strip()


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise PluginBundleError(f"{field} must be a list of non-empty strings")
    if len(value) != len(set(value)):
        raise PluginBundleError(f"{field} must not contain duplicates")
    return [item.strip() for item in value]


def _version(value: str) -> tuple[int, ...]:
    parts = value.strip().split(".")
    if not parts or any(not part.isdigit() for part in parts):
        raise PluginBundleError(f"client version is not numeric semver: {value}")
    return tuple(int(part) for part in parts)


def _version_in_range(value: str, minimum: str | None, maximum: str | None) -> bool:
    current = _version(value)
    if minimum is not None and current < _version(minimum):
        return False
    if maximum is not None and current > _version(maximum):
        return False
    return True


def _validate_connector(name: str, raw: Any, *, retired: bool = False) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PluginBundleError(f"connector {name} must be an object")
    if retired and name == "aihub":
        # 仅识别已发布 0.1.7 安装元数据，不允许它进入新包或活跃兼容性报告。
        published = {"runtime": "aihub", "mode": "native", "adapter_version": "0.1.0", "source_types": ["runtime_event_store"], "required_paths": [], "required_endpoints": [], "required_permissions": [], "min_client_version": None, "max_client_version": None}
        if raw != published:
            raise PluginBundleError("retired connector metadata does not match published v0.1.7")
        return dict(raw)
    runtime = _required_string(raw.get("runtime"), f"connectors.{name}.runtime")
    mode = _required_string(raw.get("mode"), f"connectors.{name}.mode")
    if runtime not in _RUNTIMES:
        raise PluginBundleError(f"connector {name} has unsupported runtime: {runtime}")
    if mode not in _MODES:
        raise PluginBundleError(f"connector {name} has unsupported mode: {mode}")
    adapter_version = _required_string(raw.get("adapter_version"), f"connectors.{name}.adapter_version")
    _version(adapter_version)
    for field in ("source_types", "required_paths", "required_endpoints", "required_permissions"):
        _string_list(raw.get(field), f"connectors.{name}.{field}")
    for field in ("min_client_version", "max_client_version"):
        value = raw.get(field)
        if value is not None:
            _version(_required_string(value, f"connectors.{name}.{field}"))
    minimum = raw.get("min_client_version")
    maximum = raw.get("max_client_version")
    if minimum is not None and maximum is not None and _version(minimum) > _version(maximum):
        raise PluginBundleError(f"connector {name} has inverted client version range")
    return {
        "runtime": runtime,
        "mode": mode,
        "adapter_version": adapter_version,
        "source_types": _string_list(raw["source_types"], f"connectors.{name}.source_types"),
        "required_paths": _string_list(raw["required_paths"], f"connectors.{name}.required_paths"),
        "required_endpoints": _string_list(raw["required_endpoints"], f"connectors.{name}.required_endpoints"),
        "required_permissions": _string_list(raw["required_permissions"], f"connectors.{name}.required_permissions"),
        "min_client_version": minimum,
        "max_client_version": maximum,
    }


def _validate_bundle_manifest(raw: Any, *, retired: bool = False) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PluginBundleError("bundle manifest must be an object")
    plugin_id = _required_string(raw.get("plugin_id"), "plugin_id")
    bundle_version = _required_string(raw.get("bundle_version"), "bundle_version")
    contract_version = _required_string(raw.get("contract_version"), "contract_version")
    schema_version = _required_string(raw.get("schema_version", contract_version), "schema_version")
    cursor_version = _required_string(raw.get("cursor_version", "observer.cursor.v1"), "cursor_version")
    _version(bundle_version)
    platforms = _string_list(raw.get("supported_platforms"), "supported_platforms")
    unknown_platforms = set(platforms) - _PLATFORMS
    if unknown_platforms:
        raise PluginBundleError(f"supported_platforms contains unsupported values: {sorted(unknown_platforms)}")
    connectors_raw = raw.get("connectors")
    if not isinstance(connectors_raw, dict) or not connectors_raw:
        raise PluginBundleError("connectors must be a non-empty object")
    connectors = {name: _validate_connector(name, value, retired=retired) for name, value in connectors_raw.items()}
    expected = {"dsh", "codex", "claude", "workbuddy-desktop"}
    if retired:
        expected = {"aihub", "codex", "claude", "workbuddy-desktop"} if "aihub" in connectors else {"codex", "claude", "workbuddy-desktop"}
    if set(connectors) != expected:
        raise PluginBundleError("bundle must declare exactly dsh, codex, claude and workbuddy-desktop connectors")
    if connectors["workbuddy-desktop"]["mode"] != "desktop":
        raise PluginBundleError("workbuddy connector must use desktop mode")
    return {
        "plugin_id": plugin_id,
        "bundle_version": bundle_version,
        "contract_version": contract_version,
        "schema_version": schema_version,
        "cursor_version": cursor_version,
        "supported_platforms": platforms,
        "connectors": connectors,
    }


def validate_bundle_manifest(raw: Any) -> dict[str, Any]:
    return _validate_bundle_manifest(raw)


def validate_installed_bundle_manifest(raw: Any) -> dict[str, Any]:
    """Read the exact previous release for upgrade/rollback; never admit it as a new bundle."""
    retired = isinstance(raw, dict) and raw.get("bundle_version") == "0.1.7" and isinstance(raw.get("connectors"), dict) and set(raw["connectors"]) in ({"aihub", "codex", "claude", "workbuddy-desktop"}, {"codex", "claude", "workbuddy-desktop"})
    return _validate_bundle_manifest(raw, retired=retired)


def validate_customer_bundle_manifest(raw: Any) -> dict[str, Any]:
    """Validate the intentionally smaller Codex customer profile."""
    if not isinstance(raw, dict) or raw.get("profile") != "customer-codex":
        raise PluginBundleError("customer bundle profile must be customer-codex")
    plugin_id = _required_string(raw.get("plugin_id"), "plugin_id")
    bundle_version = _required_string(raw.get("bundle_version"), "bundle_version")
    contract_version = _required_string(raw.get("contract_version"), "contract_version")
    _version(bundle_version)
    platforms = _string_list(raw.get("supported_platforms"), "supported_platforms")
    if len(platforms) != 1 or platforms[0] not in _PLATFORMS:
        raise PluginBundleError("customer bundle must declare one supported platform")
    connectors = raw.get("connectors")
    if not isinstance(connectors, dict) or set(connectors) != {"codex"}:
        raise PluginBundleError("customer bundle must declare exactly the codex connector")
    normalized_connector = _validate_connector("codex", connectors["codex"])
    runtime = raw.get("runtime")
    if not isinstance(runtime, dict):
        raise PluginBundleError("customer runtime metadata must be an object")
    if runtime.get("path") != "bin/codex-observer":
        raise PluginBundleError("customer runtime path is invalid")
    digest = runtime.get("sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise PluginBundleError("customer runtime sha256 is invalid")
    if runtime.get("platform") != platforms[0]:
        raise PluginBundleError("customer runtime platform does not match manifest")
    if not isinstance(runtime.get("version"), str) or not runtime["version"].strip():
        raise PluginBundleError("customer runtime version is invalid")
    if runtime.get("format") != "pyinstaller-onefile":
        raise PluginBundleError("customer runtime format must be pyinstaller-onefile")
    python_version = _required_string(runtime.get("python_version"), "runtime.python_version")
    _version(python_version)
    return {
        "plugin_id": plugin_id,
        "bundle_version": bundle_version,
        "contract_version": contract_version,
        "profile": "customer-codex",
        "supported_platforms": platforms,
        "connectors": {"codex": normalized_connector},
        "runtime": {"path": "bin/codex-observer", "sha256": digest, "platform": platforms[0], "version": runtime["version"], "python_version": python_version, "format": "pyinstaller-onefile"},
    }


def load_bundle_manifest(path: str | Path) -> dict[str, Any]:
    manifest_path = Path(path).expanduser().resolve()
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PluginBundleError(f"cannot read bundle manifest: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise PluginBundleError(f"bundle manifest is invalid JSON: {manifest_path}") from exc
    return validate_bundle_manifest(raw)


def _source_diagnostic(path: str | None, *, check: bool) -> SourceDiagnostic:
    """Inspect only source metadata; never open or read its contents."""

    if not path:
        return SourceDiagnostic(
            path="",
            path_status=PathStatus.MISSING if check else PathStatus.NOT_CHECKED,
            permission_status=PermissionStatus.UNKNOWN if check else PermissionStatus.NOT_CHECKED,
            message="required local source path is missing" if check else "local source was not checked",
        )
    source = Path(path).expanduser().resolve(strict=False)
    if not check:
        return SourceDiagnostic(
            path=str(source),
            path_status=PathStatus.NOT_CHECKED,
            permission_status=PermissionStatus.NOT_CHECKED,
            message="local source was not checked",
        )
    if not source.exists():
        return SourceDiagnostic(
            path=str(source),
            path_status=PathStatus.MISSING,
            permission_status=PermissionStatus.UNKNOWN,
            message="required local source is missing",
        )
    if not os.access(source, os.R_OK):
        return SourceDiagnostic(
            path=str(source),
            path_status=PathStatus.AVAILABLE,
            permission_status=PermissionStatus.DENIED,
            message="local source is not readable",
        )
    return SourceDiagnostic(
        path=str(source),
        path_status=PathStatus.AVAILABLE,
        permission_status=PermissionStatus.GRANTED,
        message="local source is readable",
    )


def check_compatibility(
    *,
    manifest: Mapping[str, Any],
    platform: str,
    contract_version: str = "component_event.v1",
    clients: Mapping[str, str] | None = None,
    source_paths: Mapping[str, str] | None = None,
    granted_permissions: Mapping[str, set[str]] | None = None,
    installed_connectors: Mapping[str, bool] | None = None,
) -> CompatibilityReport:
    normalized = validate_bundle_manifest(manifest)
    clients = clients or {}
    source_paths = source_paths or {}
    granted_permissions = granted_permissions or {}
    installed_connectors = installed_connectors or {}
    contract_status = (
        CompatibilityStatus.SUPPORTED
        if contract_version == normalized["contract_version"]
        else CompatibilityStatus.CONTRACT_INCOMPATIBLE
    )
    connector_reports: dict[str, ConnectorCompatibility] = {}
    for name, connector in normalized["connectors"].items():
        source_diagnostics = (
            (_source_diagnostic(source_paths.get(name), check=False),)
            if connector["required_paths"]
            else ()
        )
        if not installed_connectors.get(name, False):
            connector_reports[name] = ConnectorCompatibility(
                CompatibilityStatus.NOT_INSTALLED,
                "connector is not installed",
                diagnostics=source_diagnostics,
            )
            continue
        if contract_status is CompatibilityStatus.CONTRACT_INCOMPATIBLE:
            connector_reports[name] = ConnectorCompatibility(
                contract_status,
                f"bundle contract {normalized['contract_version']} does not match {contract_version}",
                diagnostics=source_diagnostics,
            )
            continue
        if platform not in _PLATFORMS or platform not in normalized["supported_platforms"]:
            connector_reports[name] = ConnectorCompatibility(
                CompatibilityStatus.CLIENT_UNSUPPORTED,
                f"bundle does not support platform {platform}",
                diagnostics=source_diagnostics,
            )
            continue
        if name == "dsh" and platform != "darwin-arm64":
            connector_reports[name] = ConnectorCompatibility(CompatibilityStatus.CLIENT_UNSUPPORTED, "DSH baseline verified only on darwin-arm64", diagnostics=source_diagnostics)
            continue
        client_key = name if name in clients else connector["runtime"]
        client_version = clients.get(client_key)
        if client_version is None:
            connector_reports[name] = ConnectorCompatibility(
                CompatibilityStatus.CLIENT_UNSUPPORTED,
                "client version is missing",
                diagnostics=source_diagnostics,
            )
            continue
        try:
            normalized_client_version = normalize_client_version(client_version)
        except ClientCompatibilityError:
            normalized_client_version = None
        if normalized_client_version is None or not _version_in_range(normalized_client_version, connector["min_client_version"], connector["max_client_version"]):
            connector_reports[name] = ConnectorCompatibility(
                CompatibilityStatus.CLIENT_UNSUPPORTED,
                f"client version {client_version} is outside the supported range",
                diagnostics=source_diagnostics,
            )
            continue
        source_diagnostics = (
            (_source_diagnostic(source_paths.get(name), check=True),)
            if connector["required_paths"]
            else ()
        )
        source_diagnostic = source_diagnostics[0] if source_diagnostics else None
        if source_diagnostic is not None and source_diagnostic.path_status is PathStatus.MISSING:
            connector_reports[name] = ConnectorCompatibility(
                CompatibilityStatus.SOURCE_MISSING,
                "required local source is missing",
                diagnostics=source_diagnostics,
            )
            continue
        required_permissions = set(connector["required_permissions"])
        if (
            not required_permissions.issubset(granted_permissions.get(name, set()))
            or source_diagnostic is not None
            and source_diagnostic.permission_status is PermissionStatus.DENIED
        ):
            connector_reports[name] = ConnectorCompatibility(
                CompatibilityStatus.NEEDS_PERMISSION,
                "required permissions are not granted",
                diagnostics=source_diagnostics,
            )
            continue
        connector_reports[name] = ConnectorCompatibility(
            CompatibilityStatus.SUPPORTED,
            "compatible",
            diagnostics=source_diagnostics,
        )
    return CompatibilityReport(platform, contract_status, connector_reports)


__all__ = [
    "CompatibilityReport",
    "CompatibilityStatus",
    "ConnectorCompatibility",
    "PathStatus",
    "PermissionStatus",
    "PluginBundleError",
    "SourceDiagnostic",
    "check_compatibility",
    "load_bundle_manifest",
    "validate_bundle_manifest",
    "validate_customer_bundle_manifest",
]
