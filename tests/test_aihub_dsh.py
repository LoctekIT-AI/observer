"""AI Hub (the company build of DeepSeek Harness) is the DSH the collector targets on both platforms."""
from __future__ import annotations

import json
from pathlib import Path

import install_dsh_observer as dsh


def _app(root: Path, platform: str) -> Path:
    resources = root / ("AI Hub.app/Contents/Resources" if platform == "darwin" else "AI Hub/resources")
    launcher = resources / "runtime/cli/bin" / ("dsh" if platform == "darwin" else "dsh.cmd")
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n")
    runtime = resources / "runtime/primary-runtime/runtime.json"
    runtime.parent.mkdir(parents=True)
    runtime.write_text(json.dumps({"desktopVersion": "0.2.0-rc.2", "platform": platform}))
    return launcher


def test_launchers_on_both_platforms_run_dsh_without_a_separate_node(tmp_path):
    for platform in ("darwin", "win32"):
        launcher = _app(tmp_path / platform, platform)
        assert dsh.is_harness_launcher(launcher)
        assert dsh.dsh_command(launcher, None, "plugin", "--profile", "desktop", "add", "x") == [str(launcher), "plugin", "--profile", "desktop", "add", "x"]
        dsh.verify_native_runtime(launcher)
    assert not dsh.is_harness_launcher(Path("/Applications/DSH Desktop.app/Contents/Resources/app/node_modules/@deepseek-ai/dsh/lib/bin.js"))


def test_macos_prefers_ai_hub_once_it_has_been_opened(tmp_path):
    home = tmp_path / "home"
    app = tmp_path / "Applications/AI Hub.app"
    legacy = dsh.macos_default_dsh(home=home, aihub_app=app)
    assert legacy["profile"] == home / "Library/Application Support/dsh-desktop/harness/profiles/web"
    launcher = _app(tmp_path / "Applications", "darwin")
    assert launcher == app / "Contents/Resources/runtime/cli/bin/dsh"
    assert dsh.macos_default_dsh(home=home, aihub_app=app)["cli"] != launcher  # no desktop profile yet
    profile = home / "Library/Application Support/AI Hub/ah-home/profiles/desktop"
    profile.mkdir(parents=True)
    (profile / "package.json").write_text("{}")
    chosen = dsh.macos_default_dsh(home=home, aihub_app=app)
    assert chosen == {"app": app, "cli": launcher, "profile": profile}
    assert profile.parent.parent.name == "ah-home"  # DSH_HOME passed to the launcher


def test_dsh_built_in_tools_are_not_reported(tmp_path):
    from cowork_runtime.observer.dsh_worker import DshObserverWorker
    from test_layer3_degraded import _dsh_record

    source = tmp_path / "dsh"
    source.mkdir()
    (source / "component-events.jsonl").write_text(
        _dsh_record(event_id="dsh:e1", invocation_id="dsh:i1", component_kind="tool", component_id="bash", observation_kind="tool_result")
        + _dsh_record(event_id="dsh:e2", invocation_id="dsh:i2")  # skill office-docx
        + _dsh_record(event_id="dsh:e3", invocation_id="dsh:i3", component_kind="mcp_tool", component_id="mcp__x__y", observation_kind="tool_result"))
    worker = DshObserverWorker(source_root=source, state_dir=tmp_path / "state", flush=False)
    worker.run_once()
    assert sum(worker.outbox.count_by_status().values()) == 2
    worker.close()


def test_ai_hub_outside_applications_and_without_package_json_is_found(tmp_path, monkeypatch):
    """v0.2.39: a colleague's AI Hub was not in /Applications; newer AI Hub builds may have no package.json yet."""
    home = tmp_path / "home"
    launcher = _app(home / "Downloads", "darwin")
    app = home / "Downloads/AI Hub.app"
    profile = home / "Library/Application Support/AI Hub/ah-home/profiles/desktop"
    profile.mkdir(parents=True)  # opened once, no plugin installed yet: no package.json
    monkeypatch.setattr(dsh, "_aihub_candidates", lambda _home: [Path("/Volumes/AI Hub/AI Hub.app"), dsh.AIHUB_APP, app])
    assert dsh.find_aihub_app(home) == app
    assert dsh.macos_default_dsh(home=home) == {"app": app, "cli": launcher, "profile": profile}
    monkeypatch.setattr(dsh, "_aihub_candidates", lambda _home: [Path("/Volumes/AI Hub/AI Hub.app")])
    assert dsh.find_aihub_app(home) is None  # run from the disk image: never installed from there


def test_detect_script_finds_ai_hub_in_user_applications(tmp_path):
    import subprocess
    script = Path(__file__).resolve().parents[1] / "bundle/scripts/observer_detect_clients_macos.sh"
    home = tmp_path / "home"
    _app(home / "Applications", "darwin")
    profile = home / "Library/Application Support/AI Hub/ah-home/profiles/desktop"
    profile.mkdir(parents=True)
    out = subprocess.run(["bash", str(script)], env={"HOME": str(home), "PATH": "/usr/bin:/bin"}, capture_output=True, text=True, check=True).stdout
    line = next(item for item in out.splitlines() if item.startswith("dsh\t"))
    assert line.split("\t")[1] == "found" and str(home / "Applications/AI Hub.app") in line
    assert "dsh" in out.splitlines()[-1]
