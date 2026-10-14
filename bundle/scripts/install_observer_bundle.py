#!/usr/bin/env python3
"""Install and inspect the four-host metadata-only observer bundle on macOS."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if not getattr(sys, "frozen", False):
    for import_root in (SCRIPT_ROOT / "packages/contracts/src", SCRIPT_ROOT / "apps/cowork-runtime/src"):
        sys.path.insert(0, str(import_root))

sys.path.insert(0, str(SCRIPT_ROOT / "scripts"))
import install_workbuddy_hooks as workbuddy_hooks

from cowork_runtime.plugin_package import PluginArchiveInstallError, PluginArchiveInstaller
from cowork_runtime.telemetry.langfuse import LangfuseConfig, LangfuseExportError, LangfuseExporter
from cowork_runtime.telemetry.langfuse_outbox import LangfuseContentOutbox


DEFAULT_INSTALL_ROOT = Path.home() / "Library/Application Support/AIHub/observer/bundle"
DEFAULT_CONFIG_FILE = Path.home() / "Library/Application Support/AIHub/observer/customer.env"
DEFAULT_CODEX_SOURCE = Path.home() / ".codex/sessions"
DEFAULT_CLAUDE_SOURCE = Path.home() / "Library/Application Support/Claude/IndexedDB/https_claude.ai_0.indexeddb.leveldb"
DEFAULT_WORKBUDDY_SOURCE = Path.home() / ".aihub/observer/workbuddy-hook-events.jsonl"
DEFAULT_CODEX_STATE = Path.home() / "Library/Application Support/AIHub/observer"
DEFAULT_CLAUDE_STATE = Path.home() / "Library/Application Support/AIHub/claude-observer"
DEFAULT_WORKBUDDY_STATE = Path.home() / "Library/Application Support/AIHub/workbuddy-observer"
HOSTS = ("dsh", "codex", "claude", "workbuddy-desktop")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("command", choices=("install", "status", "uninstall"))
    parser.add_argument("archive", nargs="?", type=Path, help="bundle ZIP required by install")
    parser.add_argument("--host", action="append", choices=HOSTS)
    parser.add_argument("--install-root", type=Path, default=DEFAULT_INSTALL_ROOT)
    parser.add_argument("--config-file", type=Path, default=DEFAULT_CONFIG_FILE)
    parser.add_argument("--runtime-root", type=Path, help="dedicated managed runtime preserved across lifecycle")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--launch-agent-label-prefix", help="explicit isolated registration prefix")
    parser.add_argument("--launch-agent-dir", type=Path, help="explicit isolated plist directory")
    parser.add_argument("--dsh-source", type=Path, default=Path.home() / ".aihub/observer/dsh")
    parser.add_argument("--dsh-profile-dir", type=Path, default=Path.home() / "Library/Application Support/dsh-desktop/harness/profiles/web")
    parser.add_argument("--dsh-cli", type=Path, default=Path("/Applications/DSH Desktop.app/Contents/Resources/app/node_modules/@deepseek-ai/dsh/lib/bin.js"))
    parser.add_argument("--dsh-state-dir", type=Path, default=Path.home() / "Library/Application Support/AIHub/dsh-observer")
    parser.add_argument("--codex-source", type=Path, default=DEFAULT_CODEX_SOURCE)
    parser.add_argument("--claude-source", type=Path, default=DEFAULT_CLAUDE_SOURCE)
    parser.add_argument("--workbuddy-source", type=Path, default=DEFAULT_WORKBUDDY_SOURCE)
    parser.add_argument("--workbuddy-settings", type=Path, default=Path.home() / ".workbuddy/settings.json")
    parser.add_argument("--workbuddy-policy-settings", type=Path, action="append", default=[])
    parser.add_argument("--codex-state-dir", type=Path, default=DEFAULT_CODEX_STATE)
    parser.add_argument("--claude-state-dir", type=Path, default=DEFAULT_CLAUDE_STATE)
    parser.add_argument("--workbuddy-state-dir", type=Path, default=DEFAULT_WORKBUDDY_STATE)
    parser.add_argument(
        "--outbox-action",
        choices=("block", "keep", "flush", "delete"),
        default="block",
        help="uninstall handling for pending Observer upload rows (default: block)",
    )
    return parser


def _absolute(value: Path) -> Path:
    return Path(os.path.abspath(value.expanduser()))


def _selected_hosts(args: argparse.Namespace) -> tuple[str, ...]:
    return tuple(dict.fromkeys(args.host or HOSTS))


def _common_arguments(args: argparse.Namespace) -> dict[str, list[str]]:
    root = _absolute(args.install_root)
    config = _absolute(args.config_file)
    python = _absolute(args.python)
    entrypoints = {
        "dsh": ("install_dsh_observer_launch_agent.py", "dsh_observer_worker.py"),
        "codex": ("install_observer_launch_agent.py", "codex_observer_worker.py"),
        "claude": ("install_claude_observer_launch_agent.py", "claude_observer_worker.py"),
        "workbuddy-desktop": ("install_workbuddy_observer_launch_agent.py", "workbuddy_observer_worker.py"),
    }
    commands: dict[str, list[str]] = {}
    for host in _selected_hosts(args):
        installer, worker = entrypoints[host]
        option = "workbuddy" if host == "workbuddy-desktop" else host
        commands[host] = [
            sys.executable, str(root / "scripts" / installer), "install",
            "--worker", str(root / "scripts" / worker), "--python", str(python),
            "--source-root", str(_absolute(getattr(args, option + "_source"))),
            "--state-dir", str(_absolute(getattr(args, option + "_state_dir"))),
            "--config-file", str(config),
        ]
    return commands


def _uninstall_arguments(args: argparse.Namespace) -> dict[str, list[str]]:
    root = _absolute(args.install_root)
    entrypoints = {
        "dsh": "install_dsh_observer_launch_agent.py",
        "codex": "install_observer_launch_agent.py",
        "claude": "install_claude_observer_launch_agent.py",
        "workbuddy-desktop": "install_workbuddy_observer_launch_agent.py",
    }
    return {host: [sys.executable, str((SCRIPT_ROOT if host == "workbuddy-desktop" else root) / "scripts" / entrypoints[host]), "uninstall"] for host in _selected_hosts(args)}


def _run_host(command: list[str]) -> dict[str, Any]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=45)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "blocked", "error_type": type(exc).__name__, "reason": str(exc)}
    output = result.stdout.strip().splitlines()
    if output:
        try:
            payload = json.loads(output[-1])
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            pass
    return {
        "status": "blocked",
        "returncode": result.returncode,
        "reason": (result.stderr or result.stdout).strip().splitlines()[0] if (result.stderr or result.stdout).strip() else "host installer returned no status",
    }


def _doctor_command(args: argparse.Namespace) -> list[str]:
    root = _absolute(args.install_root)
    command = [
        sys.executable,
        str(root / "scripts/observer_doctor.py"),
        "--config-file", str(_absolute(args.config_file)),
    ]
    for host in _selected_hosts(args):
        option = "workbuddy" if host == "workbuddy-desktop" else host
        command += ["--host", host, f"--{option}-source", str(_absolute(getattr(args, option + "_source"))), f"--{option}-state-dir", str(_absolute(getattr(args, option + "_state_dir")))]
        if host == "dsh":
            command += ["--dsh-profile-dir", str(_absolute(args.dsh_profile_dir))]
        if args.launch_agent_dir:
            command += [f"--{option}-plist", str(_absolute(args.launch_agent_dir) / (host + ".plist"))]
    if args.launch_agent_label_prefix:
        command += ["--launch-agent-label-prefix", args.launch_agent_label_prefix]
    return command


def _run_doctor(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    try:
        result = subprocess.run(_doctor_command(args), capture_output=True, text=True, check=False, timeout=45)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 2, {"status": "blocked", "error_type": type(exc).__name__, "reason": str(exc)}
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return 2, {"status": "blocked", "returncode": result.returncode, "reason": (result.stderr or result.stdout).strip()}
    return result.returncode, payload if isinstance(payload, dict) else {"status": "blocked", "reason": "doctor returned a non-object"}


def _emit(payload: dict[str, Any]) -> int:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload.get("status") == "ok" else 2


def _report(args: argparse.Namespace, payload: dict[str, Any]) -> int:
    return _emit({**payload, "selected_hosts": list(_selected_hosts(args)), "upload_acceptance": "unverified"})


def _installed_membership(install_root: Path) -> dict[str, list[str]]:
    path = install_root / "installed-hosts.json"
    if not path.exists():
        # 旧包没有成员账本，显式保留未知端，不能把它们冒充成已验收安装。
        return {"hosts": [], "untracked_hosts": list(HOSTS) if install_root.is_dir() else []}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("installed host membership must be an object")
    result: dict[str, list[str]] = {}
    for key in ("hosts", "untracked_hosts"):
        values = raw.get(key)
        if not isinstance(values, list) or any(not isinstance(host, str) or host not in HOSTS for host in values) or len(set(values)) != len(values):
            raise ValueError(f"installed host membership has invalid {key}")
        result[key] = values
    if set(result["hosts"]) & set(result["untracked_hosts"]):
        raise ValueError("installed and untracked host membership overlap")
    return result


def _write_membership(install_root: Path, membership: dict[str, list[str]]) -> None:
    # 原子替换避免中断留下半份账本；归档升级后重新写入保留的成员。
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=".installed-hosts-", dir=install_root, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(membership, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink()
            raise
    try:
        os.replace(temporary, install_root / "installed-hosts.json")
    finally:
        if temporary.exists():
            temporary.unlink()


def _bundle_diagnostics(install_root: Path) -> dict[str, Any]:
    manifest_path = install_root / "manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "blocked", "reason": f"manifest unavailable: {type(exc).__name__}"}
    connectors = manifest.get("connectors", {}) if isinstance(manifest, dict) else {}
    return {
        "status": "ok",
        "bundle_version": manifest.get("bundle_version"),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "contract_version": manifest.get("contract_version"),
        "schema_version": manifest.get("schema_version"),
        "cursor_version": manifest.get("cursor_version"),
        "adapter_versions": {
            name: value.get("adapter_version")
            for name, value in connectors.items()
            if isinstance(value, dict)
        },
    }


def _state_dirs(args: argparse.Namespace) -> dict[str, Path]:
    return {
        host: _absolute(getattr(args, ("workbuddy" if host == "workbuddy-desktop" else host) + "_state_dir"))
        for host in _selected_hosts(args)
    }


def _outbox_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    """Read outbox state without creating or changing any state database."""
    by_host: dict[str, Any] = {}
    total_pending = 0
    for host, state_dir in _state_dirs(args).items():
        database = state_dir / "langfuse-content-outbox.sqlite3"
        report: dict[str, Any] = {"path": str(database), "exists": database.is_file(), "counts": {}, "pending": 0}
        if database.is_file():
            uri = f"file:{database}?mode=ro"
            try:
                with sqlite3.connect(uri, uri=True, timeout=5) as connection:
                    rows = connection.execute(
                        "SELECT status,COUNT(*) FROM langfuse_content_outbox GROUP BY status"
                    ).fetchall()
                counts = {str(status): int(count) for status, count in rows}
                pending = sum(count for status, count in counts.items() if status != "delivered")
                report.update({"counts": counts, "pending": pending})
                total_pending += pending
            except (OSError, sqlite3.Error) as exc:
                report.update({"status": "blocked", "reason": f"outbox unreadable: {type(exc).__name__}"})
        by_host[host] = report
    return {"pending": total_pending, "hosts": by_host}


def _flush_outboxes(args: argparse.Namespace) -> dict[str, Any]:
    config_file = _absolute(args.config_file)
    if not config_file.is_file():
        return {"status": "blocked", "reason": "cannot flush outbox without customer telemetry configuration"}
    os.environ["LANGFUSE_CONFIG_FILE"] = str(config_file)
    try:
        config = LangfuseConfig.from_env()
        exporter = LangfuseExporter(config)
    except (LangfuseExportError, OSError, ValueError) as exc:
        return {"status": "blocked", "error_type": type(exc).__name__, "reason": str(exc)}

    reports: dict[str, Any] = {}
    for host, state_dir in _state_dirs(args).items():
        database = state_dir / "langfuse-content-outbox.sqlite3"
        if not database.is_file():
            reports[host] = {"delivered": 0, "failed": 0, "exists": False}
            continue
        outbox = LangfuseContentOutbox(database)
        try:
            result = outbox.flush(exporter.export, owner_id=f"observer-uninstall-{os.getpid()}-{host}")
            reports[host] = {"delivered": result.delivered, "failed": result.failed, "exists": True}
        finally:
            outbox.close()
    snapshot = _outbox_snapshot(args)
    return {
        "status": "ok" if snapshot["pending"] == 0 else "blocked",
        "reports": reports,
        "after": snapshot,
    }


def _delete_outboxes(args: argparse.Namespace) -> dict[str, Any]:
    removed: dict[str, list[str]] = {}
    for host, state_dir in _state_dirs(args).items():
        paths = [state_dir / "langfuse-content-outbox.sqlite3", state_dir / "langfuse-content-outbox.sqlite3-wal", state_dir / "langfuse-content-outbox.sqlite3-shm"]
        host_removed: list[str] = []
        for path in paths:
            try:
                if path.exists():
                    path.unlink()
                    host_removed.append(str(path))
            except OSError as exc:
                return {"status": "blocked", "error_type": type(exc).__name__, "reason": f"cannot delete outbox {path}"}
        removed[host] = host_removed
    return {"status": "ok", "removed": removed, "after": _outbox_snapshot(args)}


def _agent_arguments(args: argparse.Namespace, host: str) -> list[str]:
    options = []
    if args.launch_agent_label_prefix:
        options += ["--label", args.launch_agent_label_prefix + "." + host]
    if args.launch_agent_dir:
        options += ["--plist", str(_absolute(args.launch_agent_dir) / (host + ".plist"))]
    return options


def _worker_snapshot(args: argparse.Namespace, host: str) -> dict[str, Any]:
    defaults = {"dsh": "com.observer.dsh-observer", "codex": "com.aihub.codex-observer", "claude": "com.aihub.claude-observer", "workbuddy-desktop": "com.aihub.workbuddy-observer"}
    label = (args.launch_agent_label_prefix + "." + host) if args.launch_agent_label_prefix else defaults[host]
    plist = _absolute(args.launch_agent_dir) / (host + ".plist") if args.launch_agent_dir else Path.home() / "Library/LaunchAgents" / (defaults[host] + ".plist")
    target = f"gui/{os.getuid()}/{label}"
    result = subprocess.run(["launchctl", "print", target], capture_output=True, text=True, check=False)
    return {"target": target, "plist": plist, "contents": plist.read_bytes() if plist.is_file() else None, "registered": result.returncode == 0}


def _stop_dsh_worker(args: argparse.Namespace) -> dict[str, Any]:
    return _run_host(_uninstall_arguments(args)["dsh"] + _agent_arguments(args, "dsh"))


def _restore_dsh_worker(previous: dict[str, Any]) -> dict[str, Any]:
    try:
        if previous["contents"] is not None:
            previous["plist"].parent.mkdir(parents=True, exist_ok=True)
            previous["plist"].write_bytes(previous["contents"])
        if previous["registered"]:
            result = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(previous["plist"])], capture_output=True, text=True, check=False)
            if result.returncode:
                raise RuntimeError((result.stderr or result.stdout).strip())
            check = subprocess.run(["launchctl", "print", previous["target"]], capture_output=True, text=True, check=False)
            if check.returncode:
                raise RuntimeError("previous worker registration was not restored")
        return {"status": "ok", "registered": previous["registered"], "plist_restored": previous["contents"] is not None}
    except (OSError, RuntimeError) as exc:
        return {"status": "blocked", "reason": str(exc), "error_type": type(exc).__name__}


def _dsh_profile_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    manifest = _absolute(args.dsh_profile_dir) / "package.json"
    raw = json.loads(manifest.read_text()) if manifest.is_file() else {}
    bundles = raw.get("dsh", {}).get("profile", {}).get("bundles", [])
    return {"dependency": _dsh_dependency(args), "bundle_index": bundles.index("@observer/dsh-observer") if "@observer/dsh-observer" in bundles else None}


def _dsh_dependency(args: argparse.Namespace) -> str | None:
    manifest = _absolute(args.dsh_profile_dir) / "package.json"
    if not manifest.is_file():
        return None
    raw = json.loads(manifest.read_text())
    dependency = raw.get("dependencies", {}).get("@observer/dsh-observer")
    if dependency is not None and not isinstance(dependency, str):
        raise ValueError("DSH Observer dependency must be a package spec")
    return dependency


def _restore_dsh_plugin(args: argparse.Namespace, previous_dependency: str | None) -> dict[str, Any]:
    """Restore only our native dependency after a failed worker registration."""
    root = _absolute(args.install_root)
    if previous_dependency is None:
        return _run_host([sys.executable, str(root / "scripts/install_dsh_observer.py"), "uninstall", "--profile-dir", str(_absolute(args.dsh_profile_dir)), "--dsh-cli", str(_absolute(args.dsh_cli))])
    node = shutil.which("node")
    if node is None:
        return {"status": "blocked", "reason": "Node executable is unavailable for native profile rollback"}
    profile = _absolute(args.dsh_profile_dir)
    spec = previous_dependency
    if spec.startswith("file:"):
        spec = "file:" + str((profile / spec[5:]).resolve())
    command = [node, str(_absolute(args.dsh_cli)), "plugin", "--profile", profile.name, "add", "@observer/dsh-observer@" + spec]
    result = subprocess.run(command, cwd=profile, env={**os.environ, "DSH_HOME": str(profile.parent.parent)}, capture_output=True, text=True, check=False)
    return {"status": "ok" if result.returncode == 0 else "blocked", "returncode": result.returncode, "reason": "previous native Observer dependency restored" if result.returncode == 0 else (result.stderr or result.stdout).strip()}


def _workbuddy_options(args: argparse.Namespace) -> dict[str, Any]:
    return {"settings": _absolute(args.workbuddy_settings),
        "receipt": _absolute(args.workbuddy_state_dir) / "workbuddy-hooks-owned.json"}


def _workbuddy_install(args: argparse.Namespace) -> dict[str, Any]:
    return workbuddy_hooks.install(**_workbuddy_options(args), python=_absolute(args.python),
        bridge=_absolute(args.install_root) / "scripts/workbuddy_hook_bridge.py",
        event_file=_absolute(args.workbuddy_source), policy_settings=tuple(args.workbuddy_policy_settings))


def _layout_canonical_path(path: Path) -> Path:
    """Use the actual directory spelling, including a missing path's ancestor."""
    ancestor = path.resolve()
    missing = []
    while not ancestor.is_dir():
        missing.append(ancestor.name)
        ancestor = ancestor.parent
    # macOS 的 realpath 保留大小写，系统 pwd 才读取目录的实际身份/拼写。
    result = subprocess.run(["/bin/pwd", "-P"], cwd=ancestor, capture_output=True, text=True, check=False)
    if result.returncode:
        raise OSError(result.stderr.strip() or "native directory identity lookup failed")
    return Path(result.stdout.removesuffix("\n")).joinpath(*reversed(missing))


def _validate_layout(args: argparse.Namespace) -> None:
    """Before native/archive changes, protect only shared and selected inputs."""
    bundle = _layout_canonical_path(_absolute(args.install_root))
    roots = [bundle, *(bundle.parent / ("." + bundle.name + "." + suffix) for suffix in ("previous", "replacement", "backup", "rollback-replacement", "rollback-backup"))]
    protected = [_absolute(args.config_file), _absolute(args.python), Path(sys.executable)]
    if args.runtime_root:
        protected.append(_absolute(args.runtime_root))
    # venv 的启动路径与实际基础解释器都要保留，比较时才解析软链接。
    for python in (_absolute(args.python), Path(sys.executable)):
        if python.parent.name == "bin" and python.parent.parent.name == "venv":
            protected.append(python.parent.parent.parent)
    if args.command == "install" and args.archive is not None:
        protected.append(_absolute(args.archive))
    defaults = {"dsh": "com.observer.dsh-observer", "codex": "com.aihub.codex-observer", "claude": "com.aihub.claude-observer", "workbuddy-desktop": "com.aihub.workbuddy-observer"}
    for host, state in _state_dirs(args).items():
        option = "workbuddy" if host == "workbuddy-desktop" else host
        protected.append(_absolute(getattr(args, option + "_source")))
        # Codex 默认 state 是 bundle 的祖先，保护实际文件而不是禁止合法父目录。
        names = ["status.json", "installation.json", option + "-observer.lock"]
        for database in ("observer.sqlite3", "langfuse-content-outbox.sqlite3"):
            names.extend((database, database + "-wal", database + "-shm"))
        protected.extend(state / name for name in names)
        protected.append(_absolute(args.launch_agent_dir) / (host + ".plist") if args.launch_agent_dir else Path.home() / "Library/LaunchAgents" / (defaults[host] + ".plist"))
        if host == "dsh":
            protected.extend((_absolute(args.dsh_profile_dir), _absolute(args.dsh_cli)))
        if host == "workbuddy-desktop":
            protected.extend((*_workbuddy_options(args).values(), *(_absolute(path) for path in args.workbuddy_policy_settings)))
    for path in protected:
        for root in roots:
            for candidate in (_absolute(path), _layout_canonical_path(path)):
                if candidate.is_relative_to(root) or root.is_relative_to(candidate):
                    raise ValueError("Observer bundle must not overlap protected runtime, private or selected native paths")
            # 真实硬链接不会被 resolve 归一化，检查 inode 防止删除同一私有文件。
            if path.is_file() and root.is_dir():
                for member in root.rglob("*"):
                    if member.is_file() and member.samefile(path):
                        raise ValueError("Observer bundle must not overlap a protected hardlinked file")


def _install(args: argparse.Namespace) -> int:
    _validate_layout(args)
    if args.archive is None:
        return _report(args, {"status": "blocked", "reason": "install requires a bundle archive"})
    install_root = _absolute(args.install_root)
    archive_installer = PluginArchiveInstaller(install_root)
    had_previous = install_root.is_dir()
    previous_membership = _installed_membership(install_root)
    workbuddy_selected = "workbuddy-desktop" in _selected_hosts(args)
    hooks_before = workbuddy_hooks.snapshot(**_workbuddy_options(args)) if workbuddy_selected else None
    hooks_after = None
    hook_setup = None
    dsh_selected = "dsh" in _selected_hosts(args)
    previous_dsh_profile = _dsh_profile_snapshot(args) if dsh_selected else None
    previous_workers = {host: _worker_snapshot(args, host) for host in _common_arguments(args)}
    previous_worker = previous_workers.get("dsh")
    try:
        archive_result = archive_installer.install(_absolute(args.archive))
    except (OSError, PluginArchiveInstallError) as exc:
        return _report(args, {"status": "blocked", "phase": "archive", "error_type": type(exc).__name__, "reason": str(exc)})
    native_plugin = _run_host([sys.executable, str(install_root / "scripts/install_dsh_observer.py"), "--profile-dir", str(_absolute(args.dsh_profile_dir)), "--dsh-cli", str(_absolute(args.dsh_cli)), "--source-root", str(_absolute(args.dsh_source))]) if dsh_selected else None
    if workbuddy_selected:
        try:
            hook_setup = _workbuddy_install(args)
            hooks_after = workbuddy_hooks.snapshot(**_workbuddy_options(args))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            hook_setup = {"status": "blocked", "error_type": type(exc).__name__, "reason": str(exc)}
    hosts = {}
    for name, command in _common_arguments(args).items():
        prerequisite = native_plugin if name == "dsh" else hook_setup if name == "workbuddy-desktop" else None
        hosts[name] = prerequisite if prerequisite is not None and prerequisite.get("status") != "ok" else _run_host(command + _agent_arguments(args, name))
    host_ok = all(report.get("status") == "ok" and report.get("enabled") is True for report in hosts.values())
    rollback: dict[str, Any] | None = None
    host_restore: dict[str, Any] | None = None
    native_restore = None
    changed_workers = {}
    worker_stop = None
    worker_restore = None
    if not host_ok:
        # A failed native command may already have mutated package metadata.
        try:
            if dsh_selected and _dsh_profile_snapshot(args) != previous_dsh_profile:
                native_restore = _restore_dsh_plugin(args, previous_dsh_profile["dependency"])
                if native_restore.get("status") == "ok":
                    manifest = _absolute(args.dsh_profile_dir) / "package.json"
                    raw = json.loads(manifest.read_text())
                    bundles = raw.setdefault("dsh", {}).setdefault("profile", {}).setdefault("bundles", [])
                    bundles[:] = [item for item in bundles if item != "@observer/dsh-observer"]
                    if previous_dsh_profile["bundle_index"] is not None:
                        bundles.insert(previous_dsh_profile["bundle_index"], "@observer/dsh-observer")
                    manifest.write_text(json.dumps(raw, indent=2) + "\n")
                    if _dsh_profile_snapshot(args) != previous_dsh_profile:
                        native_restore = {"status": "blocked", "reason": "native Observer profile restoration postcondition failed"}
        except (OSError, RuntimeError, ValueError) as exc:
            native_restore = {"status": "blocked", "reason": str(exc), "error_type": type(exc).__name__}
        if native_plugin is not None and native_plugin.get("status") == "ok":
            worker_stop = _stop_dsh_worker(args)
        for host in hosts:
            if host == "dsh":
                continue
            if _worker_snapshot(args, host) != previous_workers[host]:
                changed_workers[host] = _run_host(_uninstall_arguments(args)[host] + _agent_arguments(args, host))
        can_rollback = (worker_stop is None or worker_stop.get("status") == "ok") and all(report.get("status") == "ok" for report in changed_workers.values())
    else:
        can_rollback = False
    hook_restore = None
    if not host_ok and hooks_after is not None and can_rollback:
        try:
            workbuddy_hooks.restore(hooks_before, hooks_after)
            hook_restore = {"status": "ok"}
        except (OSError, ValueError) as exc:
            hook_restore = {"status": "blocked", "reason": str(exc)}
            can_rollback = False
    if not host_ok and archive_result.changed and can_rollback:
        if had_previous:
            try:
                rolled_back = archive_installer.rollback()
                rollback = {"status": "ok", "bundle_version": rolled_back.bundle_version}
                # Exact native snapshots avoid executing retired archive installers.
                host_restore = {}
            except (OSError, PluginArchiveInstallError) as exc:
                rollback = {"status": "blocked", "error_type": type(exc).__name__, "reason": str(exc)}
        else:
            try:
                shutil.rmtree(install_root)
                rollback = {"status": "ok", "bundle_removed": True}
            except OSError as exc:
                rollback = {"status": "blocked", "error_type": type(exc).__name__, "reason": str(exc)}
    if not host_ok:
        host_restore = {host: _restore_dsh_worker(previous_workers[host]) for host, report in changed_workers.items() if report.get("status") == "ok"}
    if not host_ok and worker_stop is not None and worker_stop.get("status") == "ok":
        worker_restore = _restore_dsh_worker(previous_worker)
    if host_ok:
        membership = {"hosts": list(dict.fromkeys([*previous_membership["hosts"], *_selected_hosts(args)])), "untracked_hosts": [host for host in previous_membership["untracked_hosts"] if host not in _selected_hosts(args)]}
        _write_membership(install_root, membership)
    doctor_code, doctor = _run_doctor(args) if host_ok else (2, {"status": "blocked", "reason": "host registration failed; doctor not run"})
    status = "ok" if host_ok and doctor_code == 0 else "blocked"
    return _report(args, {
        "status": status,
        "archive": {"changed": archive_result.changed, "bundle_version": archive_result.bundle_version, "install_root": str(install_root), "diagnostics": _bundle_diagnostics(install_root)},
        "hosts": hosts,
        **({"dsh_plugin": native_plugin, "dsh_plugin_restore": native_restore, "dsh_worker_stop": worker_stop, "dsh_worker_restore": worker_restore} if dsh_selected else {}),
        **({"workbuddy_hooks": hook_setup, "workbuddy_hooks_restore": hook_restore} if workbuddy_selected else {}),
        "host_stop": changed_workers,
        "rollback": rollback,
        "host_restore": host_restore,
        "doctor": doctor,
        "membership": _installed_membership(install_root),
    })


def _status(args: argparse.Namespace) -> int:
    install_root = _absolute(args.install_root)
    manifest = install_root / "manifest.json"
    if not manifest.is_file():
        return _report(args, {"status": "blocked", "reason": "observer bundle is not installed", "install_root": str(install_root)})
    hook_status = None
    if "workbuddy-desktop" in _selected_hosts(args):
        hook_status = workbuddy_hooks.status(**_workbuddy_options(args), policy_settings=tuple(args.workbuddy_policy_settings))
    code, doctor = _run_doctor(args)
    current = PluginArchiveInstaller(install_root).current()
    previous_manifest = install_root.parent / f".{install_root.name}.previous" / "manifest.json"
    previous_version = None
    if previous_manifest.is_file():
        try:
            previous_version = json.loads(previous_manifest.read_text(encoding="utf-8")).get("bundle_version")
        except (OSError, json.JSONDecodeError):
            previous_version = "invalid"
    return _report(args, {
        "status": "ok" if code == 0 else "blocked",
        "install_root": str(install_root),
        "bundle": {**_bundle_diagnostics(install_root), "previous_bundle_version": previous_version},
        **({"workbuddy_hooks": hook_status} if hook_status is not None else {}),
        "outbox": _outbox_snapshot(args),
        "membership": _installed_membership(install_root),
        "doctor": doctor,
    })


def _uninstall(args: argparse.Namespace) -> int:
    _validate_layout(args)
    install_root = _absolute(args.install_root)
    if not install_root.is_dir():
        if "workbuddy-desktop" in _selected_hosts(args):
            stopped = _run_host(_uninstall_arguments(args)["workbuddy-desktop"] + _agent_arguments(args, "workbuddy-desktop"))
            if stopped.get("status") != "ok":
                return _report(args, {"status": "blocked", "workbuddy_worker": stopped})
            workbuddy_hooks.uninstall(**_workbuddy_options(args))
        return _report(args, {"status": "ok", "changed": False, "reason": "observer bundle is not installed", "install_root": str(install_root)})
    membership = _installed_membership(install_root)
    hosts = {name: _run_host(command + _agent_arguments(args, name)) for name, command in _uninstall_arguments(args).items()}
    failures = {name: report for name, report in hosts.items() if report.get("status") != "ok"}
    if failures:
        return _report(args, {"status": "blocked", "changed": False, "install_root": str(install_root), "hosts": hosts, "failures": failures})
    outbox_before = _outbox_snapshot(args)
    if any(report.get("status") == "blocked" for report in outbox_before["hosts"].values()):
        return _report(args, {"status": "blocked", "changed": False, "install_root": str(install_root), "hosts": hosts, "outbox": outbox_before, "reason": "outbox state is unreadable"})
    outbox_result: dict[str, Any]
    if args.outbox_action == "block" and outbox_before["pending"]:
        return _report(args, {"status": "blocked", "changed": False, "install_root": str(install_root), "hosts": hosts, "outbox": outbox_before, "reason": "pending outbox rows require --outbox-action keep, flush, or delete"})
    if args.outbox_action == "keep":
        outbox_result = {"status": "ok", "action": "keep", "before": outbox_before, "state_preserved": True}
    elif args.outbox_action == "flush":
        outbox_result = _flush_outboxes(args)
        if outbox_result.get("status") != "ok":
            return _report(args, {"status": "blocked", "changed": False, "install_root": str(install_root), "hosts": hosts, "outbox": outbox_result, "reason": "outbox flush did not finish"})
    elif args.outbox_action == "delete":
        outbox_result = _delete_outboxes(args)
    else:
        outbox_result = {"status": "ok", "action": "block", "before": outbox_before}
    if outbox_result.get("status") != "ok":
        return _report(args, {"status": "blocked", "changed": False, "hosts": hosts, "outbox": outbox_result, "state_preserved": True})
    if "workbuddy-desktop" in _selected_hosts(args):
        workbuddy_hooks.uninstall(**_workbuddy_options(args))
    if "dsh" in _selected_hosts(args):
        native_plugin = _run_host([sys.executable, str(install_root / "scripts/install_dsh_observer.py"), "uninstall", "--profile-dir", str(_absolute(args.dsh_profile_dir)), "--dsh-cli", str(_absolute(args.dsh_cli))])
        if native_plugin.get("status") != "ok":
            return _report(args, {"status": "blocked", "changed": False, "dsh_plugin": native_plugin, "state_preserved": True})
    remaining = {key: [host for host in values if host not in _selected_hosts(args)] for key, values in membership.items()}
    if any(remaining.values()):
        _write_membership(install_root, remaining)
        return _report(args, {"status": "ok", "changed": True, "install_root": str(install_root), "hosts": hosts, "outbox": outbox_result, "state_preserved": True, "shared_files_preserved": True, "membership": remaining})
    if sys.platform == "darwin" and not args.launch_agent_dir:
        # The last client is gone: remove the self-update LaunchAgent as well.
        try:
            import observer_update
            observer_update.unregister_macos()
        except Exception as exc:  # the collectors are already removed; report and continue
            print(f"warning: Observer updater could not be removed: {type(exc).__name__}", file=sys.stderr)
    try:
        shutil.rmtree(install_root)
    except OSError as exc:
        return _report(args, {"status": "blocked", "changed": False, "install_root": str(install_root), "hosts": hosts, "outbox": outbox_result, "error_type": type(exc).__name__, "reason": str(exc)})
    return _report(args, {"status": "ok", "changed": True, "install_root": str(install_root), "hosts": hosts, "outbox": outbox_result, "state_preserved": True, "shared_files_preserved": False, "membership": remaining})


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "install":
            return _install(args)
        if args.command == "status":
            return _status(args)
        return _uninstall(args)
    except (OSError, RuntimeError, ValueError) as exc:
        return _report(args, {"status": "blocked", "error_type": type(exc).__name__, "reason": str(exc)})


if __name__ == "__main__":
    raise SystemExit(main())
