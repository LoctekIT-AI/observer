"""Create the deterministic, metadata-only local plugin bundle archive."""

from __future__ import annotations

import hashlib
import json
import os
import platform as platform_module
import struct
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .compatibility import load_bundle_manifest


_BUNDLE_FILES = (
    "packages/plugin-bundle/manifest.json",
    "packages/dsh-observer/package.json",
    "packages/dsh-observer/index.mjs",
    "packages/dsh-observer/cordis.patch.yml",
    "packages/dsh-observer/README.md",
    "scripts/bootstrap_observer_macos.sh",
    "scripts/observer_macos_layout.sh",
    "packages/plugin-bundle/requirements-observer.txt",
    "packages/plugin-bundle/observer-runtime-pins.env",
    "scripts/observer_windows.py",
    "scripts/observer_windows_common.ps1",
    "scripts/install_workbuddy_hooks.py",
    "apps/cowork-runtime/src/cowork_runtime/observer/locking.py",
    "scripts/dsh_observer_worker.py",
    "scripts/install_dsh_observer.py",
    "scripts/install_dsh_observer_launch_agent.py",
    "apps/cowork-runtime/src/cowork_runtime/adapters/dsh.py",
    "apps/cowork-runtime/src/cowork_runtime/observer/dsh_worker.py",

    ".codex-plugin/plugin.json",
    ".codebuddy-plugin/plugin.json",
    "hooks/hooks.json",
    "scripts/codex_skill_hook_bridge.py",
    "scripts/workbuddy_hook_bridge.py",
    "scripts/observer_collect.py",
    "scripts/observer_authorize.py",
    "scripts/codex_observer_worker.py",
    "scripts/claude_observer_worker.py",
    "scripts/install_observer_launch_agent.py",
    "scripts/install_claude_observer_launch_agent.py",
    "scripts/workbuddy_observer_worker.py",
    "scripts/install_workbuddy_observer_launch_agent.py",
    "scripts/run_observer_windows.ps1",
    "scripts/install_observer_windows.ps1",
    "scripts/register_observer_task_windows.ps1",
    "scripts/unregister_observer_task_windows.ps1",
    "tools/cowork/codex_app_server_runner.mjs",
    "apps/cowork-runtime/src/cowork_runtime/adapters/workbuddy_desktop.py",
    "apps/cowork-runtime/src/cowork_runtime/adapters/workbuddy_jsonl.py",
    "apps/cowork-runtime/src/cowork_runtime/adapters/__init__.py",
    "apps/cowork-runtime/src/cowork_runtime/adapters/codex.py",
    "apps/cowork-runtime/src/cowork_runtime/adapters/claude.py",
    "apps/cowork-runtime/src/cowork_runtime/adapters/claude_desktop.py",
    "apps/cowork-runtime/src/cowork_runtime/adapters/registry.py",
    "apps/cowork-runtime/src/cowork_runtime/adapters/delivery.py",
    "apps/cowork-runtime/src/cowork_runtime/telemetry/langfuse.py",
    "apps/cowork-runtime/src/cowork_runtime/telemetry/identity.py",
    "apps/cowork-runtime/src/cowork_runtime/telemetry/keychain.py",
    "apps/cowork-runtime/src/cowork_runtime/telemetry/observer_auth.py",
    "apps/cowork-runtime/src/cowork_runtime/telemetry/emitter.py",
    "apps/cowork-runtime/src/cowork_runtime/telemetry/redaction.py",
    "apps/cowork-runtime/src/cowork_runtime/telemetry/langfuse_outbox.py",
    "apps/cowork-runtime/src/cowork_runtime/telemetry/sdk.py",
    "apps/cowork-runtime/src/cowork_runtime/telemetry/__init__.py",
    "apps/cowork-runtime/src/cowork_runtime/telemetry/uploader.py",
    "apps/cowork-runtime/src/cowork_runtime/observer/__init__.py",
    "apps/cowork-runtime/src/cowork_runtime/observer/codex_worker.py",
    "apps/cowork-runtime/src/cowork_runtime/observer/claude_worker.py",
    "apps/cowork-runtime/src/cowork_runtime/observer/workbuddy_worker.py",
    "apps/cowork-runtime/src/cowork_runtime/dataset_quality.py",
    "scripts/observer_doctor.py",
    "scripts/install_observer_bundle.py",
    "packages/contracts/src/cowork_contracts/component_event.py",
    "packages/contracts/src/cowork_contracts/telemetry.py",
    "packages/contracts/src/cowork_contracts/telemetry_adapter.py",
)

_BUNDLE_PACKAGE_DIRS = (
    "packages/contracts/src/cowork_contracts",
    "apps/cowork-runtime/src/cowork_runtime/contracts",
    "apps/cowork-runtime/src/cowork_runtime/loop",
    "apps/cowork-runtime/src/cowork_runtime/persistence",
    "apps/cowork-runtime/src/cowork_runtime/plugin_package",
)


@dataclass(frozen=True)
class PluginBundleBuild:
    path: Path
    sha256: str
    files: tuple[str, ...]


class CustomerBundleBuildError(ValueError):
    """The customer bundle cannot be built from the supplied runtime."""


class PluginBundleBuilder:
    """Build a reproducible archive without collecting user sources or secrets."""

    def __init__(self, repo_root: str | Path) -> None:
        self.repo_root = Path(repo_root).expanduser().resolve()

    def build(self, destination: str | Path) -> PluginBundleBuild:
        output = Path(destination).expanduser().resolve()
        if output.is_relative_to(self.repo_root):
            raise ValueError("bundle destination must be outside the source tree")
        manifest_path = self.repo_root / "packages/plugin-bundle/manifest.json"
        manifest = load_bundle_manifest(manifest_path)
        files: dict[str, bytes] = {"manifest.json": self._manifest_bytes(manifest)}
        for name in ("README.md", "AGENTS.md"):
            files[name] = (self.repo_root / "packages/plugin-bundle" / name).read_bytes()
        for relative in _BUNDLE_FILES[1:]:
            source = self.repo_root / relative
            if not source.is_file():
                raise FileNotFoundError(f"bundle source file is missing: {relative}")
            files[relative] = source.read_bytes()
        for package_root in _BUNDLE_PACKAGE_DIRS:
            root = self.repo_root / package_root
            if not root.is_dir():
                raise FileNotFoundError(f"bundle source package is missing: {package_root}")
            for source in sorted(root.rglob("*")):
                if source.is_file() and source.suffix in {".py", ".json"}:
                    relative = source.relative_to(self.repo_root).as_posix()
                    files[relative] = source.read_bytes()
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for relative in sorted(files):
                entry = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, files[relative], compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        return PluginBundleBuild(output, digest, tuple(sorted(files)))

    def build_customer(
        self,
        destination: str | Path,
        *,
        runtime_executable: str | Path | None = None,
        platform: str | None = None,
    ) -> PluginBundleBuild:
        """Build the small Codex customer archive from a prepared native worker.

        The generic builder deliberately does not invoke a compiler or inspect the
        development environment.  Release tooling must prepare the executable
        first, then pass that exact artifact here.
        """
        if runtime_executable is None:
            raise CustomerBundleBuildError("customer bundle requires a prepared runtime executable")
        runtime = Path(runtime_executable).expanduser().resolve()
        if not runtime.is_file():
            raise CustomerBundleBuildError(f"runtime executable is not a regular file: {runtime}")
        if runtime.suffix.lower() in {".py", ".pyw", ".sh", ".ps1"} or runtime.read_bytes()[:2] == b"#!":
            raise CustomerBundleBuildError("customer runtime must be a native executable, not a source script")
        if not os.access(runtime, os.X_OK):
            raise CustomerBundleBuildError("runtime executable is not executable")
        executable_bytes = runtime.read_bytes()
        target_platform = platform or _host_platform()
        # 只接受已经完成的本机 Mach-O；源码脚本不能冒充独立运行时。
        executable_platform = None
        if len(executable_bytes) >= 8 and executable_bytes[:4] == b"\xcf\xfa\xed\xfe":
            executable_platform = {0x0100000C: "darwin-arm64", 0x01000007: "darwin-x64"}.get(struct.unpack("<I", executable_bytes[4:8])[0])
        if executable_platform is None:
            raise CustomerBundleBuildError("customer runtime must be a native executable (64-bit Mach-O)")
        if target_platform != executable_platform:
            raise CustomerBundleBuildError("runtime executable architecture does not match platform")
        if target_platform != _host_platform():
            raise CustomerBundleBuildError("customer runtime must be verified on its target platform")
        env = {key: value for key, value in os.environ.items() if not key.startswith(("PYTHON", "LANGFUSE_"))}
        with tempfile.TemporaryDirectory(prefix="codex-observer-runtime-check-") as clean:
            run = subprocess.run([str(runtime), "--runtime-info"], cwd=clean, env=env, capture_output=True, text=True, check=True)
        identity = json.loads(run.stdout)
        if identity.get("runtime") != "codex" or identity.get("contract_version") != "component_event.v1" or not isinstance(identity.get("version"), str):
            raise CustomerBundleBuildError("runtime executable returned invalid runtime identity")
        if identity.get("frozen") is not True or not isinstance(identity.get("python_version"), str) or not identity["python_version"]:
            raise CustomerBundleBuildError("customer runtime must contain a fixed frozen Python runtime")
        digest = hashlib.sha256(executable_bytes).hexdigest()
        source_manifest = load_bundle_manifest(self.repo_root / "packages/plugin-bundle/manifest.json")
        manifest = {
            "plugin_id": source_manifest["plugin_id"],
            "bundle_version": source_manifest["bundle_version"],
            "contract_version": source_manifest["contract_version"],
            "profile": "customer-codex",
            "supported_platforms": [target_platform],
            "connectors": {"codex": source_manifest["connectors"]["codex"]},
            "runtime": {
                "path": "bin/codex-observer",
                "sha256": digest,
                "platform": target_platform,
                "version": identity["version"],
                "python_version": identity["python_version"],
                "format": "pyinstaller-onefile",
            },
        }
        files = {
            "manifest.json": self._manifest_bytes(manifest),
            ".codex-plugin/plugin.json": self._manifest_bytes({
                "name": source_manifest["plugin_id"],
                "version": source_manifest["bundle_version"],
                "description": "Collects metadata-only Codex Desktop Skill/MCP lifecycle events.",
                "interface": {"displayName": "Skill/MCP Observer", "capabilities": ["Read"]},
            }),
            "bin/codex-observer": executable_bytes,
        }
        output = Path(destination).expanduser().resolve()
        if output.is_relative_to(self.repo_root):
            raise ValueError("bundle destination must be outside the source tree")
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for relative in sorted(files):
                entry = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.external_attr = (0o100755 if relative.startswith("bin/") else 0o100644) << 16
                archive.writestr(entry, files[relative], compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        return PluginBundleBuild(output, hashlib.sha256(output.read_bytes()).hexdigest(), tuple(sorted(files)))

    @staticmethod
    def _manifest_bytes(manifest: dict[str, object]) -> bytes:
        return (json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _host_platform() -> str:
    system = platform_module.system().lower()
    machine = platform_module.machine().lower()
    if system == "darwin":
        return "darwin-arm64" if machine in {"arm64", "aarch64"} else "darwin-x64"
    if system == "windows":
        return "win32-x64"
    return f"{system}-x64"



__all__ = ["CustomerBundleBuildError", "PluginBundleBuild", "PluginBundleBuilder"]
