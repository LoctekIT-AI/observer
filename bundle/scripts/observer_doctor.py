#!/usr/bin/env python3
"""Report local customer observer readiness without reading transcript bodies."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import sys
import subprocess
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if not getattr(__import__("sys"), "frozen", False):
    for import_root in (SCRIPT_ROOT / "packages/contracts/src", SCRIPT_ROOT / "apps/cowork-runtime/src"):
        sys.path.insert(0, str(import_root))

from cowork_runtime.plugin_package.client_compatibility import (
    ClientCompatibilityError,
    detect_macos_bundle_version,
    normalize_client_version,
    validate_client_version,
)
from cowork_runtime.plugin_package.compatibility import load_bundle_manifest
from cowork_runtime.plugin_package.workbuddy_runtime import inspect_workbuddy_command
sys.path.insert(0, str(SCRIPT_ROOT / "scripts"))
from install_dsh_observer import macos_default_dsh  # noqa: E402
from cowork_runtime.telemetry.langfuse import LangfuseConfig, LangfuseExportError

HOSTS = ("dsh", "codex", "claude", "workbuddy-desktop")
# macOS apps whose Info.plist gives the client version. Apps can live in /Applications or
# ~/Applications (a per-user install); Claude is also looked up by bundle id via Spotlight.
_MACOS_APP_NAMES = {"claude": ("Claude.app", "com.anthropic.claudefordesktop"), "workbuddy": ("WorkBuddy.app", None)}


def macos_app_path(runtime: str, home: Path | None = None) -> Path | None:
    if runtime == "dsh":
        return macos_default_dsh()["app"]  # AI Hub when installed, else DSH Desktop
    entry = _MACOS_APP_NAMES.get(runtime)
    if entry is None:
        return None
    name, bundle_id = entry
    candidates = [Path("/Applications") / name, (home or Path.home()) / "Applications" / name]
    for candidate in candidates:
        if (candidate / "Contents" / "Info.plist").is_file():
            return candidate
    if bundle_id and sys.platform == "darwin" and Path("/usr/bin/mdfind").exists():
        try:
            found = subprocess.run(["/usr/bin/mdfind", f"kMDItemCFBundleIdentifier == '{bundle_id}'"],
                                   capture_output=True, text=True, check=False, timeout=5).stdout.splitlines()
        except (OSError, subprocess.SubprocessError):
            found = []
        for line in found:
            if line.endswith(".app") and (Path(line) / "Contents" / "Info.plist").is_file():
                return Path(line)
    return candidates[0]


def _compatibility_manifest() -> dict[str, object] | None:
    """Load the installed manifest, or the repository manifest during development."""

    candidates = (SCRIPT_ROOT / "manifest.json", SCRIPT_ROOT / "packages/plugin-bundle/manifest.json")
    for path in candidates:
        if not path.is_file():
            continue
        try:
            return load_bundle_manifest(path)
        except (OSError, ValueError):
            return None
    return None


_CODEX_EXECUTABLE_CANDIDATES = (
    Path("/Applications/Codex.app/Contents/Resources/codex"),
    Path("/Applications/Codex.app/Contents/MacOS/codex"),
    Path("/opt/homebrew/bin/codex"),
    Path("/usr/local/bin/codex"),
    Path.home() / ".local/bin/codex",
)


def _codex_executable() -> str | None:
    found = shutil.which("codex")
    if found:
        return found
    for candidate in _CODEX_EXECUTABLE_CANDIDATES:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _codex_rollout_cli_version(sessions: Path) -> str | None:
    """Read cli_version from the session_meta header of the newest rollout file."""
    try:
        rollouts = sorted(sessions.rglob("rollout-*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for rollout in rollouts[:20]:
        try:
            with rollout.open("rb") as handle:
                header = handle.readline(8 * 1024 * 1024)
            record = json.loads(header)
        except (OSError, ValueError):
            continue
        payload = record.get("payload") if isinstance(record, dict) else None
        if record.get("type") == "session_meta" and isinstance(payload, dict) and isinstance(payload.get("cli_version"), str):
            return payload["cli_version"]
    return None


def _compatibility_for_host(
    host: str,
    manifest: dict[str, object] | None,
    *,
    app_path: Path | None = None,
) -> dict[str, object]:
    """Return a version-table result without reading client transcript data."""

    if manifest is None:
        return {"status": "unavailable", "reason": "bundle manifest is unavailable"}
    connector_name = host
    connector = manifest.get("connectors", {}).get(connector_name) if isinstance(manifest.get("connectors"), dict) else None
    if not isinstance(connector, dict):
        return {"status": "unavailable", "reason": f"connector metadata is missing: {connector_name}"}
    runtime = connector.get("runtime")
    if not isinstance(runtime, str):
        return {"status": "unavailable", "reason": f"runtime metadata is missing: {connector_name}"}
    try:
        if runtime == "codex":
            executable = _codex_executable()
            if executable is not None:
                completed = subprocess.run(
                    [executable, "--version"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                if completed.returncode != 0:
                    return {"status": "unavailable", "reason": "codex version command failed"}
                raw_version = (completed.stdout or completed.stderr).strip()
            else:
                # Codex Desktop users usually have no `codex` on PATH. The version that
                # matters for parsing is the one that wrote the rollouts, recorded in the
                # newest rollout's session_meta (only cli_version is read).
                raw_version = _codex_rollout_cli_version(Path.home() / ".codex/sessions")
                if raw_version is None:
                    return {"status": "unavailable", "reason": "codex executable is unavailable and no rollout records a cli_version"}
            client_version = normalize_client_version(raw_version)
        else:
            if sys.platform != "darwin":
                return {"status": "unavailable", "reason": "macOS application version is unavailable"}
            bundle_path = app_path or macos_app_path(runtime)
            if bundle_path is None:
                return {"status": "unavailable", "reason": f"client bundle path is unavailable: {runtime}"}
            client_version = detect_macos_bundle_version(bundle_path)
        validate_client_version(
            runtime=runtime,
            client_version=client_version,
            minimum=connector.get("min_client_version"),
            maximum=connector.get("max_client_version"),
        )
    except ClientCompatibilityError as exc:
        reason = str(exc)
        return {
            "status": "unsupported" if "outside supported range" in reason else "unavailable",
            "reason": reason,
        }
    return {
        "status": "supported",
        "runtime": runtime,
        "client_version": client_version,
        "minimum": connector.get("min_client_version"),
        "maximum": connector.get("max_client_version"),
    }

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", action="append", choices=HOSTS)
    parser.add_argument("--launch-agent-label-prefix")
    parser.add_argument("--config-file", type=Path, default=Path.home() / "Library/Application Support/AIHub/observer/customer.env")
    parser.add_argument("--dsh-source", type=Path, default=Path.home() / ".aihub/observer/dsh")
    parser.add_argument("--dsh-profile-dir", type=Path, default=macos_default_dsh()["profile"])
    parser.add_argument("--dsh-plist", type=Path, default=Path.home() / "Library/LaunchAgents/com.observer.dsh-observer.plist")
    parser.add_argument("--dsh-state-dir", type=Path, default=Path.home() / "Library/Application Support/AIHub/dsh-observer")
    parser.add_argument("--codex-source", type=Path, default=Path.home() / ".codex/sessions")
    parser.add_argument("--claude-source", type=Path, default=Path.home() / "Library/Application Support/Claude/IndexedDB/https_claude.ai_0.indexeddb.leveldb")
    parser.add_argument("--workbuddy-source", type=Path, default=Path.home() / ".aihub/observer/workbuddy-hook-events.jsonl")
    parser.add_argument("--codex-plist", type=Path, default=Path.home() / "Library/LaunchAgents/com.aihub.codex-observer.plist")
    parser.add_argument("--claude-plist", type=Path, default=Path.home() / "Library/LaunchAgents/com.aihub.claude-observer.plist")
    parser.add_argument("--workbuddy-plist", type=Path, default=Path.home() / "Library/LaunchAgents/com.aihub.workbuddy-observer.plist")
    parser.add_argument("--codex-state-dir", type=Path, default=Path.home() / "Library/Application Support/AIHub/observer")
    parser.add_argument("--claude-state-dir", type=Path, default=Path.home() / "Library/Application Support/AIHub/claude-observer")
    parser.add_argument("--workbuddy-state-dir", type=Path, default=Path.home() / "Library/Application Support/AIHub/workbuddy-observer")
    return parser


def _source(path: Path) -> dict[str, object]:
    resolved = path.expanduser().resolve()
    return {"path": str(resolved), "exists": resolved.exists(), "readable": os.access(resolved, os.R_OK) if resolved.exists() else False}


def _bundle_metadata() -> dict[str, object]:
    manifest_path = next(
        (path for path in (SCRIPT_ROOT / "manifest.json", SCRIPT_ROOT / "packages/plugin-bundle/manifest.json") if path.is_file()),
        SCRIPT_ROOT / "manifest.json",
    )
    try:
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return {"status": "unavailable"}
    connectors = manifest.get("connectors", {}) if isinstance(manifest, dict) else {}
    return {
        "status": "ok",
        "bundle_version": manifest.get("bundle_version"),
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "contract_version": manifest.get("contract_version"),
        "schema_version": manifest.get("schema_version"),
        "cursor_version": manifest.get("cursor_version"),
        "adapter_versions": {
            name: value.get("adapter_version")
            for name, value in connectors.items()
            if isinstance(value, dict)
        },
    }


def _worker_heartbeat(state_dir: Path) -> dict[str, object]:
    status_path = state_dir.expanduser().resolve() / "status.json"
    result: dict[str, object] = {"status_file": str(status_path), "status": "missing"}
    try:
        raw = json.loads(status_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return result
    except (OSError, ValueError) as exc:
        result["status"] = "unreadable"
        result["reason"] = type(exc).__name__
        return result
    if not isinstance(raw, dict):
        result["status"] = "invalid"
        return result
    result["status"] = raw.get("status", "unknown")
    for key in ("pid", "launch_pid", "checked_at", "events_total", "outbox"):
        if key in raw:
            result[key] = raw[key]
    checked_at = raw.get("checked_at")
    if isinstance(checked_at, str):
        try:
            timestamp = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
            if timestamp.tzinfo is not None:
                result["age_seconds"] = max(0.0, (datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds())
        except ValueError:
            result["age_seconds"] = None
    return result


def _launchctl_registration(label: str) -> dict[str, object]:
    result: dict[str, object] = {"label": label, "registered": False, "running": False, "pid": None, "state": None}
    executable = shutil.which("launchctl")
    if executable is None or sys.platform != "darwin":
        result["status"] = "unavailable"
        result["reason"] = "macOS launchctl is unavailable"
        return result
    completed = subprocess.run(
        [executable, "print", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if completed.returncode != 0:
        result["status"] = "not_registered" if "Could not find service" in completed.stderr else "error"
        if result["status"] == "error":
            result["reason"] = (completed.stderr or completed.stdout).strip().splitlines()[0] if (completed.stderr or completed.stdout).strip() else "launchctl print failed"
        return result
    state_match = re.search(r"^\s*state = (.+)$", completed.stdout, re.MULTILINE)
    pid_match = re.search(r"^\s*pid = (\d+)$", completed.stdout, re.MULTILINE)
    state = state_match.group(1).strip() if state_match else None
    pid = int(pid_match.group(1)) if pid_match else None
    running = state == "running" and pid is not None
    if running:
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            running = False
    result.update({"registered": True, "running": running, "pid": pid, "state": state, "status": "running" if running else "registered"})
    return result


def _workbuddy_runtime_status() -> dict[str, object]:
    """Inspect the running desktop sidecar's MCP mode, never its config body."""

    executable = shutil.which("ps")
    if executable is None:
        return {"status": "unavailable", "reason": "ps is unavailable"}
    try:
        completed = subprocess.run(
            [executable, "-axo", "command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "unavailable", "reason": type(exc).__name__}
    for command in completed.stdout.splitlines():
        if "codebuddy" not in command or "--serve" not in command:
            continue
        parsed = inspect_workbuddy_command(command)
        parsed["status"] = "running"
        return parsed
    return {"status": "not_running", "observer_registration": "native_command_hooks"}


def _host_status(*, name: str, label: str, plist: Path, state_dir: Path) -> dict[str, object]:
    registration = _launchctl_registration(label)
    plist_path = plist.expanduser().resolve()
    worker = _worker_heartbeat(state_dir)
    result: dict[str, object] = {
        "host": name,
        "registration": registration,
        "plist": {"path": str(plist_path), "exists": plist_path.is_file()},
        "worker": worker,
    }
    if plist_path.is_file():
        try:
            with plist_path.open("rb") as handle:
                document = plistlib.load(handle)
            result["plist"]["program_arguments"] = document.get("ProgramArguments", [])
        except (OSError, plistlib.InvalidFileException, ValueError) as exc:
            result["plist"]["read_error"] = type(exc).__name__
    return result


def _dsh_profile(profile_dir: Path) -> dict[str, object]:
    root = profile_dir.expanduser().resolve()
    result: dict[str, object] = {"profile_dir": str(root), "status": "missing", "desktop_acceptance": "unverified"}
    try:
        manifest = json.loads((root / "package.json").read_text())
        package = "@observer/dsh-observer"
        dependency = package in manifest.get("dependencies", {})
        layer = package in manifest.get("dsh", {}).get("profile", {}).get("bundles", [])
        installed = json.loads((root / "node_modules/@observer/dsh-observer/package.json").read_text()) if dependency else {}
        result.update({"dependency_installed": dependency, "bundle_layer_enabled": layer, "plugin_version": installed.get("version"), "status": "configured" if dependency and layer else "not_enabled", "tested_desktop_version": "0.9.0", "tested_framework_version": "0.1.5-rc.2"})
    except FileNotFoundError:
        return result
    except (OSError, ValueError) as exc:
        result.update({"status": "invalid", "error_type": type(exc).__name__})
    return result


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    selected = tuple(dict.fromkeys(args.host or HOSTS))
    config_file = args.config_file.expanduser().resolve()
    compatibility_manifest = _compatibility_manifest()
    payload: dict[str, object] = {
        "status": "ok",
        "selected_hosts": list(selected),
        "upload_acceptance": "unverified",
        "reasons": [],
        "bundle": _bundle_metadata(),
        "config": {"path": str(config_file), "exists": config_file.is_file(), "consent": "unknown", "mode": "unknown", "receiver": "blocked", "observer": "blocked"},
        "sources": {},
        "hosts": {},
    }
    labels = {"dsh": "com.observer.dsh-observer", "codex": "com.aihub.codex-observer", "claude": "com.aihub.claude-observer", "workbuddy-desktop": "com.aihub.workbuddy-observer"}
    for host in selected:
        option = "workbuddy" if host == "workbuddy-desktop" else host
        payload["sources"][host] = _source(getattr(args, option + "_source"))
        host_status = _host_status(name="claude-desktop" if host == "claude" else host, label=(args.launch_agent_label_prefix + "." + host if args.launch_agent_label_prefix else labels[host]), plist=getattr(args, option + "_plist"), state_dir=getattr(args, option + "_state_dir"))
        host_status["compatibility"] = _compatibility_for_host(host, compatibility_manifest)
        if host == "workbuddy-desktop":
            host_status["mcp_runtime"] = _workbuddy_runtime_status()
        payload["hosts"][host] = host_status
    if "dsh" in selected:
        payload["dsh_profile"] = _dsh_profile(args.dsh_profile_dir)
    if not config_file.is_file():
        payload["status"] = "blocked"
        payload["config"]["reason"] = "customer telemetry configuration is missing"
        payload["reasons"].append("customer telemetry configuration is missing")
    else:
        os.environ["LANGFUSE_CONFIG_FILE"] = str(config_file)
        try:
            lines = config_file.read_text(encoding="utf-8").splitlines()
            values = dict(line.split("=", 1) for line in lines if "=" in line and not line.lstrip().startswith("#"))
            payload["config"]["consent"] = values.get("AIHUB_TELEMETRY_CONSENT", "unknown")
            has_observer_authorization = bool(values.get("OBSERVER_AUTH_TOKEN_REF"))
            payload["config"]["mode"] = "observer_oauth" if has_observer_authorization else "observer_required"
            if payload["config"]["consent"] != "granted":
                raise LangfuseExportError("telemetry consent is not granted")
            if not has_observer_authorization:
                raise LangfuseExportError("Observer authorization is required; legacy credential configuration is unsupported")
            LangfuseConfig.from_env()
            payload["config"]["receiver"] = "internal"
            payload["config"]["observer"] = "configured"
        except (OSError, ValueError, LangfuseExportError) as exc:
            payload["status"] = "blocked"
            payload["config"]["reason"] = str(exc)
            payload["reasons"].append(str(exc))
    if "dsh" in selected and payload["dsh_profile"]["status"] != "configured":
        payload["status"] = "blocked"
        payload["reasons"].append("DSH native profile Observer bundle is not configured")
    for host_name, host in payload["hosts"].items():
        registration = host["registration"]
        if registration.get("status") != "running":
            payload["status"] = "blocked"
            payload["reasons"].append(f"{host_name} observer is {registration.get('status')}")
        compatibility = host["compatibility"]
        if compatibility.get("status") != "supported":
            payload["status"] = "blocked"
            payload["reasons"].append(
                f"{host_name} client compatibility is {compatibility.get('status')}: {compatibility.get('reason', 'unknown reason')}"
            )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
