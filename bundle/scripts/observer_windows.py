#!/usr/bin/env python3
"""Selected Windows Observer lifecycle and shared, real input validation."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for base in (ROOT / "apps/cowork-runtime/src", ROOT / "packages/contracts/src"):
    sys.path.insert(0, str(base))

from cowork_runtime.plugin_package.archive_installer import PluginArchiveInstaller
from install_observer_bundle import HOSTS, _installed_membership, _write_membership
import install_workbuddy_hooks as workbuddy_hooks

WORKERS = {"codex": "codex", "claude": "claude", "dsh": "dsh", "workbuddy-desktop": "workbuddy"}


def validate_layout(*, bundle: Path, runtime: Path, source: Path, state: Path, config: Path,
    dsh_profile: Path | None = None, dsh_cli: Path | None = None) -> None:
    paths = {"bundle": bundle, "runtime": runtime, "source": source, "state": state, "config": config,
        **{name: path for name, path in (("dsh_profile", dsh_profile), ("dsh_cli", dsh_cli)) if path is not None}}
    for name, path in paths.items():
        if not path.is_absolute() or any(c in str(path) for c in '\x00\r\n"'):
            raise ValueError(f"{name} must be an absolute safe path")
    root = bundle.resolve()
    for name, path in paths.items():
        if name == "bundle":
            continue
        candidate = path.resolve()
        if root.is_relative_to(candidate) or candidate.is_relative_to(root):
            raise ValueError(f"bundle must not overlap {name}")


def dsh_installer_script(install_root: Path, action: str) -> Path:
    # 卸载 helper 属于执行中的 bootstrap 源码，不依赖可能已被上次卸载删除的 bundle。
    root = ROOT if action == "uninstall" else install_root
    return (root / "scripts/install_dsh_observer.py").resolve(strict=True)


def managed_python_identity(*, python: Path, runtime: Path) -> dict[str, str]:
    runtime = runtime.resolve(strict=True)
    venv = runtime / "venv"
    launcher = python.absolute()
    if launcher.parent.parent.resolve(strict=True) != venv.resolve(strict=True):
        raise ValueError("Observer launcher must belong to the explicit managed venv")
    config = {key.strip(): value.strip() for key, value in (line.split("=", 1) for line in (venv / "pyvenv.cfg").read_text().splitlines() if "=" in line)}
    home = Path(config["home"].strip())
    if not home.is_absolute():
        raise ValueError("managed venv home must be absolute")
    managed = (runtime / "python").resolve(strict=True)
    home_real = home.resolve(strict=True)
    if not home_real.is_relative_to(managed):
        raise ValueError("managed venv home escaped the dedicated Python directory")
    probe = "import json,sys; print(json.dumps({'executable':sys.executable,'base_executable':sys._base_executable,'prefix':sys.prefix,'base_prefix':sys.base_prefix,'implementation':sys.implementation.name,'version':list(sys.version_info[:2])}))"
    result = subprocess.run([str(launcher), "-I", "-c", probe], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(f"managed Python identity probe failed ({result.returncode}): {result.stderr.strip()}")
    info = json.loads(result.stdout)
    if info["implementation"] != "cpython" or info["version"] != [3, 12]:
        raise ValueError("Observer requires actual managed CPython 3.12")
    base = Path(info["base_executable"]).resolve(strict=True)
    prefix = Path(info["prefix"]).resolve(strict=True)
    base_prefix = Path(info["base_prefix"]).resolve(strict=True)
    if prefix != venv.resolve(strict=True) or Path(info["executable"]).resolve(strict=True) != launcher.resolve(strict=True):
        raise ValueError("Python probe did not retain the explicit venv launcher identity")
    if not base.is_file() or base.parent != home_real or not base_prefix.is_relative_to(managed):
        raise ValueError("Python base interpreter does not match the managed pyvenv.cfg home")
    invocation = home / Path(info["base_executable"]).name
    if invocation.resolve(strict=True) != base:
        raise ValueError("managed base invocation does not match the real interpreter")
    return {"launcher": str(launcher), "base_executable": str(base), "base_invocation": str(invocation),
        "prefix": str(prefix), "base_prefix": str(base_prefix)}


def worker_plan(*, host: str, bundle: Path, python: Path, source: Path, state: Path, config: Path) -> dict[str, object]:
    if host not in HOSTS:
        raise ValueError("an explicit supported host is required")
    paths = {}
    for name, path in (("bundle", bundle), ("python", python), ("source", source), ("state", state), ("config", config)):
        if any(c in str(path) for c in '\x00\r\n"'):
            raise ValueError(f"unsafe {name} path")
        if not path.is_absolute():
            raise ValueError(f"{name} must be absolute")
        paths[name] = path.resolve(strict=name != "state")
    for name in ("python", "config"):
        if not paths[name].is_file():
            raise ValueError(f"{name} must be an existing file")
    if not paths["bundle"].is_dir() or not (paths["source"].is_file() or paths["source"].is_dir()):
        raise ValueError("bundle and native source must exist")
    if host == "workbuddy-desktop" and not paths["source"].is_file():
        raise ValueError("WorkBuddy requires an explicit existing native hook event file")
    worker = paths["bundle"] / "scripts" / (WORKERS[host] + "_observer_worker.py")
    worker.resolve(strict=True)
    arguments = [str(worker), "--source-root", str(paths["source"]), "--state-dir", str(paths["state"]), "--config-file", str(paths["config"])]
    if host == "claude":
        # Windows does not use the macOS skills-root default. The directory sits
        # next to the IndexedDB data; it may not exist yet when Claude's local
        # agent mode has never been used, and the worker treats that as "no
        # installed Skills" rather than an error.
        skills = paths["source"].parent.parent / "local-agent-mode-sessions"
        arguments += ["--skills-root", str(skills)]
    return {"host": host, "python": str(paths["python"]), "arguments": subprocess.list2cmdline(arguments), "argv": arguments, "state": str(paths["state"])}


def _remove_updater_task() -> None:
    # The last client is gone: the self-update task has nothing left to update.
    executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command = [str(executable), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts/register_observer_updater_windows.ps1"), "-Mode", "uninstall"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if result.returncode:
        print(f"warning: Observer updater task could not be removed: {result.stderr.strip()}", file=sys.stderr)


def _powershell(args: argparse.Namespace, action: str, **extra: str) -> dict:
    executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command = [str(executable), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts/register_observer_task_windows.ps1"), "-Mode", action, "-HostId", args.host, "-PluginDirectory", str(args.install_root), "-PythonExecutable", str(args.python), "-SourceRoot", str(args.source_root), "-StateDirectory", str(args.state_dir), "-ConfigFile", str(args.config_file), "-RuntimeDirectory", str(args.runtime_dir)]
    for key, value in extra.items():
        command += ["-" + key, str(value)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise RuntimeError(f"selected native task {action} failed ({result.returncode}): {result.stderr.strip()}")
    return json.loads(result.stdout)


def _is_harness_launcher(cli: Path) -> bool:
    # DeepSeek Harness ships DSH inside app.asar; its launcher runs DSH with the app's own Node and pnpm.
    return cli.name.lower() == "dsh.cmd" and cli.parent.name == "bin" and cli.parents[1].name == "cli"


def _dsh(args: argparse.Namespace, action: str, dependency: str | None = None) -> None:
    profile = args.dsh_profile.resolve(strict=True)
    cli = args.dsh_cli.resolve(strict=True)
    if not (profile / "package.json").is_file() or profile.parent.name != "profiles":
        raise ValueError("DSH requires an existing DSH_HOME/profiles/NAME profile")
    harness = _is_harness_launcher(cli)
    node = args.runtime_dir / "node/node.exe"
    if harness:
        env = {**os.environ, "DSH_HOME": str(profile.parent.parent)}
    else:
        env = {**os.environ, "DSH_HOME": str(profile.parent.parent),
            "PATH": str(node.parent) + os.pathsep + str(args.runtime_dir / "pnpm/node_modules/.bin") + os.pathsep + os.environ["PATH"],
            "PNPM_HOME": str(args.runtime_dir / "pnpm-home"),
            "NPM_CONFIG_USERCONFIG": str(args.runtime_dir / "npm-user.conf"),
            "NPM_CONFIG_GLOBALCONFIG": str(args.runtime_dir / "npm-global.conf"),
            "NPM_CONFIG_STORE_DIR": str(args.runtime_dir / "pnpm-store"),
            "NPM_CONFIG_CACHE": str(args.runtime_dir / "npm-cache")}
        for required in (node, args.runtime_dir / "pnpm/node_modules/.bin/pnpm.cmd"):
            required.resolve(strict=True)
    if dependency is not None:
        spec = "file:" + (profile / dependency[5:]).resolve().as_posix() if dependency.startswith("file:") else dependency
        native = ["plugin", "--profile", profile.name, "add", "@observer/dsh-observer@" + spec]
        command = [str(cli), *native] if harness else [str(node), str(cli), *native]
    else:
        command = [str(args.python), str(dsh_installer_script(args.install_root, action)), action, "--profile-dir", str(profile), "--dsh-cli", str(cli), "--source-root", str(args.source_root)]
        if not harness:
            command += ["--node", str(node)]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=240)
    if result.returncode:
        raise RuntimeError(f"native DSH {action} failed ({result.returncode}): {result.stderr or result.stdout}")


def _profile(args: argparse.Namespace) -> dict:
    raw = json.loads((args.dsh_profile / "package.json").read_text())
    dependency = raw.get("dependencies", {}).get("@observer/dsh-observer")
    if dependency is not None and not isinstance(dependency, str):
        raise ValueError("DSH Observer dependency must be a package spec")
    bundles = raw.get("dsh", {}).get("profile", {}).get("bundles", [])
    return {"dependency": dependency, "index": bundles.index("@observer/dsh-observer") if "@observer/dsh-observer" in bundles else None}


def _restore_profile(args: argparse.Namespace, previous: dict) -> None:
    _dsh(args, "uninstall" if previous["dependency"] is None else "install", previous["dependency"])
    manifest = args.dsh_profile / "package.json"
    raw = json.loads(manifest.read_text())
    bundles = raw.setdefault("dsh", {}).setdefault("profile", {}).setdefault("bundles", [])
    bundles[:] = [b for b in bundles if b != "@observer/dsh-observer"]
    if previous["index"] is not None:
        bundles.insert(previous["index"], "@observer/dsh-observer")
    manifest.write_text(json.dumps(raw, indent=2) + "\n")
    if _profile(args) != previous:
        raise RuntimeError("native DSH profile restoration postcondition failed")


def lifecycle(args: argparse.Namespace) -> dict:
    if os.name != "nt":
        raise RuntimeError("Windows native lifecycle is unavailable on this platform")
    if args.host == "dsh" and (args.dsh_profile is None or args.dsh_cli is None):
        raise ValueError("DSH requires explicit --dsh-profile and --dsh-cli")
    validate_layout(bundle=args.install_root, runtime=args.runtime_dir, source=args.source_root, state=args.state_dir, config=args.config_file, dsh_profile=args.dsh_profile, dsh_cli=args.dsh_cli)
    if args.host == "workbuddy-desktop":
        for path in (args.workbuddy_settings, args.state_dir / "workbuddy-hooks-owned.json", *args.workbuddy_policy_settings):
            validate_layout(bundle=args.install_root, runtime=args.runtime_dir, source=args.source_root, state=args.state_dir, config=path)
    installer = PluginArchiveInstaller(args.install_root)
    membership = _installed_membership(args.install_root)
    hook_options = {"settings": args.workbuddy_settings, "receipt": args.state_dir / "workbuddy-hooks-owned.json"} if args.host == "workbuddy-desktop" else None
    if args.command == "status":
        if installer.current() is None:
            raise RuntimeError("selected Observer bundle is not installed")
        hooks = workbuddy_hooks.status(**hook_options, policy_settings=tuple(args.workbuddy_policy_settings)) if hook_options else None
        return {"status": "ok", "native": _powershell(args, "status"), "membership": membership, "workbuddy_hooks": hooks}
    if args.command == "uninstall":
        native = _powershell(args, "uninstall")
        if hook_options:
            workbuddy_hooks.uninstall(**hook_options)
        if args.host == "dsh":
            _dsh(args, "uninstall")
        remaining = {key: [h for h in value if h != args.host] for key, value in membership.items()}
        if any(remaining.values()):
            _write_membership(args.install_root, remaining)
        else:
            _remove_updater_task()
            if args.install_root.exists():
                shutil.rmtree(args.install_root)
        return {"status": "ok", "native": native, "membership": remaining, "state_config_credentials_preserved": True, "shared_files_preserved": bool(any(remaining.values()))}
    if args.archive is None:
        raise ValueError("install requires --archive")
    # 快照仅限当前选端；注册失败时先停止选端，再回滚 bundle，最后恢复旧任务。
    previous_task = _powershell(args, "snapshot")
    previous_profile = _profile(args) if args.host == "dsh" else None
    hooks_before = workbuddy_hooks.snapshot(**hook_options) if hook_options else None
    hooks_after = None
    had_bundle = args.install_root.is_dir()
    archive = installer.install(args.archive)
    try:
        if hook_options:
            workbuddy_hooks.install(**hook_options, python=args.python, bridge=args.install_root / "scripts/workbuddy_hook_bridge.py", event_file=args.source_root, policy_settings=tuple(args.workbuddy_policy_settings))
            hooks_after = workbuddy_hooks.snapshot(**hook_options)
        worker_plan(host=args.host, bundle=args.install_root, python=args.python, source=args.source_root, state=args.state_dir, config=args.config_file)
        if args.host == "dsh":
            _dsh(args, "install")
        native = _powershell(args, "install")
        _write_membership(args.install_root, {"hosts": list(dict.fromkeys([*membership["hosts"], args.host])), "untracked_hosts": [h for h in membership["untracked_hosts"] if h != args.host]})
    except Exception as original:
        failures = []
        stopped = False
        try:
            _powershell(args, "uninstall")
            stopped = True
        except Exception as error:
            failures.append(f"selected task stop: {error}")
        if previous_profile is not None:
            try:
                if _profile(args) != previous_profile:
                    _restore_profile(args, previous_profile)
            except Exception as error:
                failures.append(f"DSH profile restore: {error}")
        if hooks_after is not None and stopped:
            try:
                workbuddy_hooks.restore(hooks_before, hooks_after)
            except Exception as error:
                failures.append(f"WorkBuddy hooks restore: {error}")
                stopped = False
        if archive.changed and stopped:
            try:
                if had_bundle:
                    installer.rollback()
                else:
                    shutil.rmtree(args.install_root)
            except Exception as error:
                failures.append(f"archive rollback: {error}")
        try:
            _powershell(args, "restore", TaskXml=previous_task["xml"], WasRunning=str(previous_task["running"]))
        except Exception as error:
            failures.append(f"selected task restore: {error}")
        raise RuntimeError(f"Windows install failed: {original}; rollback errors: {failures}") from original
    return {"status": "ok", "host": args.host, "native": native, "bundle_version": archive.bundle_version, "desktop_event_acceptance": "unverified"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install", "status", "uninstall", "plan"))
    parser.add_argument("--host", choices=HOSTS, required=True)
    for name in ("install-root", "python", "source-root", "state-dir", "config-file", "runtime-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("archive", "dsh-profile", "dsh-cli"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--workbuddy-settings", type=Path, default=Path.home() / ".workbuddy/settings.json")
    parser.add_argument("--workbuddy-policy-settings", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            payload = worker_plan(host=args.host, bundle=args.install_root, python=args.python, source=args.source_root, state=args.state_dir, config=args.config_file)
            payload["identity"] = managed_python_identity(python=args.python, runtime=args.runtime_dir)
        else:
            payload = lifecycle(args)
        sys.stdout.write(json.dumps(payload) + "\n")
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        sys.stdout.write(json.dumps({"status": "blocked", "error_type": type(exc).__name__, "reason": str(exc)}) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
