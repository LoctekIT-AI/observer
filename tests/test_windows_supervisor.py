"""Windows startup-item fallback: the supervisor keeps workers and the updater running."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import observer_supervisor_windows as sup

SLEEPER = f"-c \"import time; time.sleep(60)\""


def _job(directory: Path, name: str, **fields) -> None:
    data = {"name": name, "kind": "worker", "execute": sys.executable, "arguments": SLEEPER, "cwd": str(directory), **fields}
    (directory / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")


def test_workers_start_restart_on_change_and_stop_when_removed(tmp_path):
    clock = [1000.0]
    s = sup.Supervisor(tmp_path, clock=lambda: clock[0])
    _job(tmp_path, "codex", generation="1")
    assert s.step() == 1
    first = s.running["codex"].process
    assert first.poll() is None
    _job(tmp_path, "codex", generation="2")  # the installer wrote a new generation after an update
    s.step()
    second = s.running["codex"].process
    assert second.pid != first.pid and first.poll() is not None
    second.kill(); second.wait()
    clock[0] += 5
    s.step()
    assert s.running["codex"].process is None  # exited: not restarted within a minute
    clock[0] += 61
    s.step()
    third = s.running["codex"].process
    assert third.poll() is None
    (tmp_path / "codex.json").unlink()
    assert s.step() == 0
    assert third.poll() is not None and "codex" not in s.running


def test_pause_stops_workers_but_not_a_running_updater(tmp_path):
    clock = [1000.0]
    s = sup.Supervisor(tmp_path, clock=lambda: clock[0])
    _job(tmp_path, "codex")
    _job(tmp_path, "updater", kind="periodic", delay_seconds=0, interval_seconds=1800)
    s.step()
    worker, updater = s.running["codex"].process, s.running["updater"].process
    assert worker.poll() is None and updater.poll() is None
    (tmp_path / "pause").write_text("x")
    s.step()
    assert worker.poll() is not None and updater.poll() is None  # the updater may be the one installing
    clock[0] += 120
    s.step()
    assert s.running["codex"].process is None  # nothing new while paused
    (tmp_path / "pause").unlink()
    s.step()
    assert s.running["codex"].process.poll() is None  # started again at once
    s.stop_all()


def test_periodic_runs_after_delay_then_every_interval_never_twice(tmp_path):
    clock = [0.0]
    s = sup.Supervisor(tmp_path, clock=lambda: clock[0])
    _job(tmp_path, "updater", kind="periodic", delay_seconds=600, interval_seconds=1800,
         arguments="-c \"pass\"")
    s.step()
    assert s.running["updater"].process is None
    clock[0] = 601
    s.step()
    run = s.running["updater"].process
    assert run is not None
    run.wait()
    clock[0] = 700
    s.step()
    assert s.running["updater"].process is None  # finished; next run only after the interval
    clock[0] = 601 + 1800
    s.step()
    assert s.running["updater"].process is not None
    s.stop_all()


def test_main_exits_without_jobs_and_single_instance(tmp_path):
    assert sup.main(["--jobs-dir", str(tmp_path)]) == 0
    lock = sup._Lock(tmp_path / sup.LOCK_NAME)
    assert lock.acquire()
    assert sup.main(["--jobs-dir", str(tmp_path)]) == 0  # another one is running: exit at once
    lock.release()
    assert "no jobs left" in (tmp_path / sup.LOG_NAME).read_text()


def test_native_result_skips_notes_printed_before_the_json():
    # v0.3.7 on a computer without scheduled tasks: the register script printed a note line
    # before its JSON and the install was rolled back ("Expecting value: line 1 column 1").
    import pytest
    from observer_windows import _json_result

    note = "Observer: this computer does not allow scheduled tasks; using a startup item for codex instead.\n"
    assert _json_result(note + '{"status":"ok","launch_mode":"startup"}\n') == {"status": "ok", "launch_mode": "startup"}
    assert _json_result('{"status":"ok"}') == {"status": "ok"}
    with pytest.raises(ValueError):
        _json_result(note)
