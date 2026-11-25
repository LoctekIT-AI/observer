#!/usr/bin/env python3
"""Keep Observer's background programs running where Task Scheduler is not allowed.

Some company computers (security software or policy) refuse to create scheduled
tasks for a normal user. The Windows installer then registers this supervisor as
a per-user startup item (HKCU ... Run) instead. At logon it starts every program
described by a job file in --jobs-dir and keeps them going:

  <name>.json   {"name", "kind": "worker" | "periodic", "execute", "arguments", "cwd",
                 "generation", "interval_seconds", "delay_seconds", "timeout_seconds"}

- worker: started at once; restarted when it exits (at most once a minute) and
  when its job file changes (the installer writes a new generation after an update);
  stopped when its job file is removed.
- pause file: while <jobs-dir>/pause exists (at most 30 minutes old), workers are
  stopped and nothing new is started; a running periodic job (the updater that is
  installing a new version) is left alone. The installer writes it while it replaces
  the bundle and removes it before it starts the new workers.
- periodic: run after delay_seconds, then every interval_seconds (the self-updater);
  never two at once; stopped after timeout_seconds.

Standard library only: it keeps running while the bundle next to it is replaced.
When the installed bundle version changes it starts the new copy of itself and exits.
With no job files left it exits (the installer also removes the startup item then).
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

POLL_SECONDS = 10
RESTART_GAP_SECONDS = 60
LOCK_NAME = "supervisor.lock"
PAUSE_NAME = "pause"
PAUSE_LIMIT_SECONDS = 1800
LOG_NAME = "supervisor.log"
LOG_LIMIT = 256 * 1024
_WINDOWS = os.name == "nt"
_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def _log(jobs_dir: Path, message: str) -> None:
    path = jobs_dir / LOG_NAME
    try:
        if path.exists() and path.stat().st_size > LOG_LIMIT:
            path.replace(path.with_suffix(".log.1"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def _spawn(execute: str, arguments: str, cwd: str | None) -> subprocess.Popen:
    env = dict(os.environ, OBSERVER_LAUNCH_MODE="startup")
    if _WINDOWS:
        command = subprocess.list2cmdline([execute]) + (" " + arguments if arguments else "")
        return subprocess.Popen(command, cwd=cwd or None, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=_CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP)
    return subprocess.Popen([execute, *shlex.split(arguments)], cwd=cwd or None, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


@dataclass
class _Running:
    process: subprocess.Popen | None = None
    definition: dict = field(default_factory=dict)
    started_at: float = 0.0
    next_run: float | None = None


class Supervisor:
    def __init__(self, jobs_dir: Path, *, spawn: Callable = _spawn, clock: Callable[[], float] = time.monotonic) -> None:
        self.jobs_dir = jobs_dir
        self.spawn = spawn
        self.clock = clock
        self.running: dict[str, _Running] = {}
        self.started = clock()

    def jobs(self) -> dict[str, dict]:
        jobs = {}
        for path in sorted(self.jobs_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and isinstance(data.get("execute"), str) and data.get("kind") in {"worker", "periodic"}:
                jobs[path.stem] = data
        return jobs

    def paused(self) -> bool:
        path = self.jobs_dir / PAUSE_NAME
        try:
            return time.time() - path.stat().st_mtime < PAUSE_LIMIT_SECONDS
        except OSError:
            return False

    def step(self) -> int:
        """One pass; returns the number of job files."""
        now = self.clock()
        jobs = self.jobs()
        if self.paused():
            for name, entry in self.running.items():
                if entry.process is not None and entry.definition.get("kind") == "worker":
                    _log(self.jobs_dir, f"stop {name} (installer pause)")
                    _stop(entry.process)
                    entry.process = None
                    entry.started_at = 0.0
            return len(jobs)
        for name in list(self.running):
            if name not in jobs:
                entry = self.running.pop(name)
                if entry.process is not None:
                    _log(self.jobs_dir, f"stop {name} (job removed)")
                    _stop(entry.process)
        for name, job in jobs.items():
            entry = self.running.setdefault(name, _Running())
            if job["kind"] == "worker":
                self._worker(name, job, entry, now)
            else:
                self._periodic(name, job, entry, now)
        return len(jobs)

    def _start(self, name: str, job: dict, entry: _Running, now: float) -> None:
        try:
            entry.process = self.spawn(job["execute"], job.get("arguments", ""), job.get("cwd"))
            entry.started_at = now
            entry.definition = job
            _log(self.jobs_dir, f"start {name} pid {entry.process.pid}")
        except OSError as error:
            entry.process = None
            entry.started_at = now
            _log(self.jobs_dir, f"cannot start {name}: {error}")

    def _worker(self, name: str, job: dict, entry: _Running, now: float) -> None:
        if entry.process is not None and entry.definition != job:
            _log(self.jobs_dir, f"restart {name} (job changed)")
            _stop(entry.process)
            entry.process = None
            entry.started_at = 0.0
        if entry.process is not None and entry.process.poll() is None:
            return
        if entry.process is not None:
            _log(self.jobs_dir, f"{name} exited with {entry.process.returncode}")
            entry.process = None
        if entry.started_at and now - entry.started_at < RESTART_GAP_SECONDS:
            return
        self._start(name, job, entry, now)

    def _periodic(self, name: str, job: dict, entry: _Running, now: float) -> None:
        if entry.process is not None:
            if entry.process.poll() is None:
                if now - entry.started_at > float(job.get("timeout_seconds", 7200)):
                    _log(self.jobs_dir, f"stop {name} (time limit)")
                    _stop(entry.process)
                return
            _log(self.jobs_dir, f"{name} finished with {entry.process.returncode}")
            entry.process = None
        if entry.next_run is None:
            entry.next_run = self.started + float(job.get("delay_seconds", 600))
        if now >= entry.next_run:
            entry.next_run = now + float(job.get("interval_seconds", 1800))
            self._start(name, job, entry, now)

    def stop_all(self) -> None:
        for entry in self.running.values():
            if entry.process is not None:
                _stop(entry.process)
        self.running.clear()


def _bundle_version(script: Path) -> str | None:
    try:
        return json.loads((script.resolve().parents[1] / "manifest.json").read_text(encoding="utf-8")).get("bundle_version")
    except (OSError, ValueError, IndexError):
        return None


class _Lock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def acquire(self, wait_seconds: float = 0) -> bool:
        deadline = time.monotonic() + wait_seconds
        self.handle = open(self.path, "a+b")
        while True:
            try:
                if _WINDOWS:
                    import msvcrt
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    return False
                time.sleep(0.5)

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            if _WINDOWS:
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.handle.close()
            self.handle = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs-dir", type=Path, required=True)
    parser.add_argument("--wait-lock", type=float, default=0, help="seconds to wait for a previous supervisor to exit")
    args = parser.parse_args(argv)
    jobs_dir = args.jobs_dir
    jobs_dir.mkdir(parents=True, exist_ok=True)
    lock = _Lock(jobs_dir / LOCK_NAME)
    if not lock.acquire(args.wait_lock):
        return 0  # another supervisor is running
    script = Path(__file__)
    version = _bundle_version(script)
    supervisor = Supervisor(jobs_dir)
    _log(jobs_dir, f"supervisor started (bundle {version}, pid {os.getpid()})")
    try:
        while True:
            if supervisor.step() == 0:
                _log(jobs_dir, "no jobs left; supervisor exits")
                return 0
            current = _bundle_version(script)
            busy = any(entry.process is not None and entry.process.poll() is None and entry.definition.get("kind") == "periodic"
                       for entry in supervisor.running.values())
            if current and version and current != version and not busy:
                # The bundle was updated: hand over to the new copy of this script.
                _log(jobs_dir, f"bundle {version} -> {current}: restarting supervisor")
                supervisor.stop_all()
                lock.release()
                _spawn(sys.executable, subprocess.list2cmdline([str(script), "--jobs-dir", str(jobs_dir), "--wait-lock", "60"])
                       if _WINDOWS else shlex.join([str(script), "--jobs-dir", str(jobs_dir), "--wait-lock", "60"]), str(jobs_dir))
                return 0
            time.sleep(POLL_SECONDS)
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
