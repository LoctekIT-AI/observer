"""Validated, local-client plugin bundle contracts."""

from .compatibility import (
    CompatibilityReport,
    CompatibilityStatus,
    ConnectorCompatibility,
    PluginBundleError,
    check_compatibility,
    load_bundle_manifest,
    validate_customer_bundle_manifest,
)
from .installer_manifest import PluginInstallError, PluginInstallResult, PluginInstaller
from .builder import CustomerBundleBuildError, PluginBundleBuild, PluginBundleBuilder
from .archive_installer import PluginArchiveInstallError, PluginArchiveInstallResult, PluginArchiveInstaller

__all__ = [
    "CompatibilityReport",
    "CompatibilityStatus",
    "ConnectorCompatibility",
    "PluginBundleError",
    "check_compatibility",
    "load_bundle_manifest",
    "validate_customer_bundle_manifest",
    "PluginInstallError",
    "PluginInstallResult",
    "PluginInstaller",
    "PluginBundleBuild",
    "PluginBundleBuilder",
    "CustomerBundleBuildError",
    "PluginArchiveInstallError",
    "PluginArchiveInstallResult",
    "PluginArchiveInstaller",
]
