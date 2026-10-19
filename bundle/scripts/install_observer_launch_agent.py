#!/usr/bin/env python3
"""Install and inspect the Codex observer as a macOS LaunchAgent.

The installer deliberately keeps credentials out of the LaunchAgent.  The
worker receives only the path to the local configuration file.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LABEL = "com.aihub.codex-observer"
DEFAULT_INTERVAL = 5.0


def _absolute(path: Path) -> Path:
    # Resolving symlinks here would turn a virtualenv interpreter into the base
    # interpreter and discard its installed runtime dependencies.
    return Path(os.path.abspath(path.expanduser()))


def _default_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _default_source() -> Path:
    return Path.home() / ".codex" / "sessions"


def _default_state() -> Path:
    return Path.home() / "Library" / "Application Support" / "AIHub" / "observer"


def _default_worker() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    return Path(__file__).with_name("codex_observer_worker.py")


def _default_config_file() -> Path:
    return Path.home() / "Library" / "Application Support" / "AIHub" / "observer" / "customer.env"


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")


def _worker_arguments(args: argparse.Namespace) -> tuple[list[str], Path]:
    worker = _absolute(args.worker)
    python = _absolute(args.python)
    # Python scripts are launched through the selected interpreter.  A frozen
    # executable is placed directly in ProgramArguments.
    command = [str(python), str(worker)] if worker.suffix == ".py" else [str(worker)]
    command.extend(
        [
            "--source-root",
            str(_absolute(args.source_root)),
            "--state-dir",
            str(_absolute(args.state_dir)),
            "--interval-seconds",
            str(args.interval_seconds),
        ]
    )
    if args.config_file is not None:
        command.extend(["--config-file", str(_absolute(args.config_file))])
    return command, worker


def render_plist(args: argparse.Namespace) -> dict[str, Any]:
    command, _worker = _worker_arguments(args)
    state_dir = _absolute(args.state_dir)
    payload: dict[str, Any] = {
        "Label": LABEL,
        "ProgramArguments": command,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "Umask": 0o077,
        "AbandonProcessGroup": False,
        "ExitTimeOut": 30,
        "StandardOutPath": str(state_dir / f"{LABEL.rsplit('.', 1)[-1]}.stdout.log"),
        "StandardErrorPath": str(state_dir / f"{LABEL.rsplit('.', 1)[-1]}.stderr.log"),
    }
    return payload


def write_plist(args: argparse.Namespace, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as handle:
        plistlib.dump(render_plist(args), handle, fmt=plistlib.FMT_XML, sort_keys=False)


def _prerequisite_reasons(args: argparse.Namespace) -> list[str]:
    reasons: list[str] = []
    worker = _absolute(args.worker)
    if not worker.is_file():
        reasons.append(f"worker_missing:{worker}")
    elif worker.suffix != ".py" and not os.access(worker, os.X_OK):
        reasons.append(f"worker_not_executable:{worker}")

    python = _absolute(args.python)
    if worker.suffix == ".py" and (not python.is_file() or not os.access(python, os.X_OK)):
        reasons.append(f"python_missing_or_not_executable:{python}")

    source = _absolute(args.source_root)
    if not source.exists():
        reasons.append(f"source_missing:{source}")
    elif not source.is_dir() and not source.is_file():
        reasons.append(f"source_invalid:{source}")

    if args.config_file is None:
        reasons.append("config_missing:--config-file is required")
    else:
        config = _absolute(args.config_file)
        if not config.is_file():
            reasons.append(f"config_missing:{config}")
    if not reasons:
        command, _ = _worker_arguments(args)
        check = subprocess.run([*command, "--check"], capture_output=True, text=True, check=False, timeout=30)
        if check.returncode != 0:
            reasons.append(f"worker_check_failed:{check.stdout.strip() or check.stderr.strip()}")
        else:
            report = json.loads(check.stdout)
            if report.get("status") != "ok":
                reasons.append(f"worker_check_failed:{check.stdout.strip()}")
    return reasons


def _uid() -> str:
    return str(os.getuid())


def _launchctl(*arguments: str) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("launchctl")
    if executable is None:
        raise FileNotFoundError("launchctl is unavailable")
    return subprocess.run(
        [executable, *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _domain() -> str:
    return f"gui/{_uid()}"


def _service_target() -> str:
    return f"{_domain()}/{LABEL}"


def _registration() -> dict[str, Any]:
    result = _launchctl("print", _service_target())
    if result.returncode == 0:
        state_match = re.search(r"^\s*state = (.+)$", result.stdout, re.MULTILINE)
        pid_match = re.search(r"^\s*pid = (\d+)$", result.stdout, re.MULTILINE)
        state = state_match.group(1).strip() if state_match else None
        pid = int(pid_match.group(1)) if pid_match else None
        running = state == "running" and pid is not None
        if running:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                running = False
        return {"registered": True, "running": running, "pid": pid, "launchctl_state": state}
    if result.returncode == 113 and "Could not find service" in result.stderr:
        return {"registered": False, "running": False, "pid": None, "launchctl_state": None}
    raise RuntimeError(f"launchctl print exited {result.returncode}: {(result.stderr or result.stdout).strip()}")


def _base_result(args: argparse.Namespace, plist_path: Path) -> dict[str, Any]:
    try:
        registration = _registration()
        registration_error = None
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        registration = {"registered": False, "running": False, "pid": None, "launchctl_state": None}
        registration_error = str(exc)
    return {
        "label": LABEL,
        "plist": str(plist_path),
        "installed": plist_path.is_file(),
        "enabled": False,
        "blocked": False,
        "reasons": [],
        "launchctl_error": registration_error,
        **registration,
    }


def _wait_for_removal() -> dict[str, Any]:
    # A busy worker (e.g. flushing a large outbox) can take longer than a few seconds to exit.
    deadline = time.monotonic() + 30.0
    while True:
        registration = _registration()
        if not registration["registered"]:
            return registration
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("LaunchAgent remains registered 30 seconds after successful bootout")
        time.sleep(min(0.25, remaining))


def _only_delivery_failed(status: dict[str, Any]) -> bool:
    """True when the worker reads its source fine and only the upload failed.

    Upload failures (network, the receiver rejecting one pending record) are
    retried by the worker from its outbox and do not depend on the installed
    version, so they must not block an update or roll it back.
    """
    errors = status.get("errors")
    return (
        isinstance(errors, list)
        and bool(errors)
        and all(isinstance(item, dict) and item.get("code") == "delivery_failed" for item in errors)
    )


def _worker_status(args: argparse.Namespace, registration: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    path = _absolute(args.state_dir) / "status.json"
    try:
        with path.open(encoding="utf-8") as handle:
            status = json.load(handle)
        if not isinstance(status, dict):
            return None, "worker status is not an object"
        # PyInstaller onefile launchd owns the bootloader parent, while the
        # worker writes status from its child process. Script mode uses pid.
        # A status written by a previous worker process (e.g. before reinstall)
        # says nothing about the current one: treat it as not yet written.
        launch_pid = status["launch_pid"] if "launch_pid" in status else status.get("pid")
        if launch_pid != registration.get("pid"):
            return status, "worker status PID does not match LaunchAgent PID"
        if status.get("status") != "ok" and not _only_delivery_failed(status):
            return status, "worker status is not ok"
        checked_at = status.get("checked_at")
        if not isinstance(checked_at, str):
            return status, "worker status has no checked_at"
        value = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
        if value.tzinfo is None:
            return status, "worker status checked_at has no timezone"
        age = (datetime.now(timezone.utc) - value).total_seconds()
        if age < 0 or age > max(30.0, 3.0 * float(args.interval_seconds)):
            return status, "worker status is stale"
        return status, None
    except FileNotFoundError:
        return None, f"worker status missing: {path}"
    except (OSError, ValueError) as exc:
        return None, f"worker status unreadable: {exc}"


def _plist_reasons(args: argparse.Namespace, plist_path: Path) -> list[str]:
    try:
        with plist_path.open("rb") as handle:
            document = plistlib.load(handle)
        if document.get("ProgramArguments") != render_plist(args)["ProgramArguments"]:
            return ["registered plist ProgramArguments do not match requested worker configuration"]
        return []
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        return [f"plist unreadable:{exc}"]


def command_render(args: argparse.Namespace) -> int:
    destination = _absolute(args.output or args.plist)
    write_plist(args, destination)
    _emit({"status": "rendered", "plist": str(destination)})
    return 0


def command_status(args: argparse.Namespace) -> int:
    plist_path = _absolute(args.plist)
    result = _base_result(args, plist_path)
    reasons = _prerequisite_reasons(args)
    if not plist_path.is_file():
        reasons.append(f"plist_missing:{plist_path}")
    if not result["registered"]:
        reasons.append("LaunchAgent is not registered")
    if result["launchctl_error"]:
        reasons.append(f"launchctl_error:{result['launchctl_error']}")
    elif not result["running"]:
        reasons.append("LaunchAgent worker is not running")
    if result["running"]:
        worker_status, worker_error = _worker_status(args, result)
        result["worker_status"] = worker_status
        if worker_error:
            reasons.append(worker_error)
    if plist_path.is_file():
        reasons.extend(_plist_reasons(args, plist_path))
    result["reasons"] = reasons
    result["blocked"] = bool(reasons)
    result["enabled"] = not reasons
    result["status"] = "blocked" if reasons else "ok"
    _emit(result)
    return 0 if not reasons else 2


def command_install(args: argparse.Namespace) -> int:
    plist_path = _absolute(args.plist)
    reasons = _prerequisite_reasons(args)
    if reasons:
        result = {
            "status": "blocked",
            "label": LABEL,
            "plist": str(plist_path),
            "installed": plist_path.is_file(),
            "enabled": False,
            "blocked": True,
            "reasons": reasons,
        }
        _emit(result)
        return 2

    _absolute(args.state_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
    # bootstrap registers and starts a LaunchAgent.  A separate print check is
    # required before reporting enabled; writing a plist alone is insufficient.
    if _registration()["registered"]:
        existing_bootout = _launchctl("bootout", _service_target())
        if existing_bootout.returncode != 0:
            detail = (existing_bootout.stderr or existing_bootout.stdout).strip().splitlines()
            reason = detail[0] if detail else f"launchctl bootout exited {existing_bootout.returncode}"
            _emit({
                "status": "blocked",
                "label": LABEL,
                "plist": str(plist_path),
                "installed": plist_path.is_file(),
                "enabled": False,
                "blocked": True,
                "reasons": [reason],
            })
            return 2
        _wait_for_removal()
    write_plist(args, plist_path)
    bootstrap = _launchctl("bootstrap", _domain(), str(plist_path))
    registration = {"registered": False, "running": False, "pid": None, "launchctl_state": None}
    worker_status: dict[str, Any] | None = None
    worker_error: str | None = None
    # The first status is written after the worker's first pass, which can take a
    # while on a machine with a long native history. Wait for a status written by
    # the current process; a genuine failure reported by it ends the wait at once.
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        registration = _registration()
        if registration["registered"] and registration["running"]:
            worker_status, worker_error = _worker_status(args, registration)
            if worker_error is None or worker_error == "worker status is not ok":
                break
        time.sleep(0.5)
    reasons = []
    if bootstrap.returncode != 0:
        detail = (bootstrap.stderr or bootstrap.stdout).strip().splitlines()
        reasons.append(detail[0] if detail else f"launchctl bootstrap exited {bootstrap.returncode}")
    if not registration["registered"]:
        reasons.append("LaunchAgent is not registered")
    elif not registration["running"]:
        reasons.append("LaunchAgent worker is not running")
    if registration["running"] and worker_error:
        reasons.append(worker_error)
    reasons.extend(_plist_reasons(args, plist_path))
    result = {
        "status": "ok" if not reasons else "blocked",
        "label": LABEL,
        "plist": str(plist_path),
        "installed": plist_path.is_file(),
        "enabled": not reasons,
        "blocked": bool(reasons),
        "reasons": reasons,
        **registration,
        "worker_status": worker_status,
    }
    _emit(result)
    return 0 if not reasons else 2


def command_uninstall(args: argparse.Namespace) -> int:
    plist_path = _absolute(args.plist)
    reasons: list[str] = []
    if _registration()["registered"]:
        bootout = _launchctl("bootout", _service_target())
        if bootout.returncode != 0:
            detail = (bootout.stderr or bootout.stdout).strip().splitlines()
            reasons.append(detail[0] if detail else f"launchctl bootout exited {bootout.returncode}")
        else:
            _wait_for_removal()
    registration = _registration()
    if registration["registered"]:
        reasons.append("LaunchAgent remains registered")
    if not reasons and plist_path.exists():
        plist_path.unlink()
    result = {
        "status": "ok" if not reasons else "blocked",
        "label": LABEL,
        "plist": str(plist_path),
        "installed": plist_path.is_file(),
        "enabled": False,
        "blocked": bool(reasons),
        "reasons": reasons,
        "state_preserved": True,
        **registration,
    }
    _emit(result)
    return 0 if not reasons else 2


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("command", choices=("render", "install", "status", "uninstall"))
    result.add_argument("--label", help="explicit native LaunchAgent label (for isolated registrations)")
    result.add_argument("--worker", type=Path, default=_default_worker())
    result.add_argument("--python", type=Path, default=Path(sys.executable))
    result.add_argument("--source-root", type=Path, default=_default_source())
    result.add_argument("--state-dir", type=Path, default=_default_state())
    result.add_argument("--config-file", type=Path, default=_default_config_file())
    result.add_argument("--interval-seconds", type=float, default=DEFAULT_INTERVAL)
    result.add_argument("--plist", type=Path, default=_default_plist())
    result.add_argument("--output", type=Path, help="destination used by render (defaults to --plist)")
    return result


def main(argv: list[str] | None = None) -> int:
    global LABEL
    args = parser().parse_args(argv)
    if args.label is not None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]+", args.label):
            parser().error("invalid LaunchAgent label")
        LABEL = args.label
    if args.interval_seconds <= 0:
        parser().error("--interval-seconds must be positive")
    try:
        if args.command == "render":
            return command_render(args)
        if args.command == "install":
            return command_install(args)
        if args.command == "status":
            return command_status(args)
        return command_uninstall(args)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        _emit({"status": "blocked", "installed": _absolute(args.plist).is_file(), "enabled": False, "blocked": True, "reasons": [str(exc)], "error_type": type(exc).__name__})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
