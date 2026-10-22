"""v0.3.2: WorkBuddy AI (international build, ~/.workbuddy-ai) gets the same hooks and reports runtime workbuddy-ai."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import install_workbuddy_hooks as hooks
from cowork_runtime.observer.workbuddy_worker import WorkBuddyDesktopObserverWorker
from cowork_runtime.telemetry.health import health_payload
from datetime import datetime, timezone

BRIDGE = Path(__file__).resolve().parents[1] / "bundle" / "scripts" / "workbuddy_hook_bridge.py"


def _bridge(event_file: Path, stage: str, payload: dict, *extra: str) -> None:
    result = subprocess.run([sys.executable, str(BRIDGE), "--stage", stage, "--event-file", str(event_file), *extra],
        input=json.dumps(payload), text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def _skill_call(event_file: Path, session: str, call: str, skill: str, *extra: str) -> None:
    base = {"session_id": session, "tool_name": "Skill", "tool_use_id": call, "tool_input": {"skill": skill}}
    _bridge(event_file, "started", {**base, "hook_event_name": "PreToolUse"}, *extra)
    _bridge(event_file, "completed", {**base, "hook_event_name": "PostToolUse",
        "tool_response": f"Base directory for this skill: /Users/u/.workbuddy/skills/{skill}"}, *extra)


def test_bridge_tags_only_workbuddy_ai_records(tmp_path):
    events = tmp_path / "events.jsonl"
    _skill_call(events, "s-main", "c1", "skill-creator")
    _skill_call(events, "s-ai", "c2", "skill-creator", "--client", "workbuddy-ai")
    records = [json.loads(line) for line in events.read_text().splitlines()]
    assert [record.get("client") for record in records] == [None, None, "workbuddy-ai", "workbuddy-ai"]
    assert "tool_input" not in json.dumps(records)


def test_worker_reports_each_build_under_its_own_runtime(tmp_path):
    events = tmp_path / "events.jsonl"
    events.touch()
    worker = WorkBuddyDesktopObserverWorker(source_root=events, state_dir=tmp_path / "state", flush=False)
    assert not worker.run_once().errors  # installation: nothing before this is uploaded
    _skill_call(events, "s-main", "c1", "neodata-financial-search")
    _skill_call(events, "s-ai", "c2", "skill-creator", "--client", "workbuddy-ai")
    report = worker.run_once()
    assert not report.errors
    queued = worker.outbox.claim(owner_id="t", now=datetime.now(timezone.utc), limit=100)
    assert sorted((item.event.runtime, item.event.component_id) for item in queued) == [
        ("workbuddy", "neodata-financial-search"), ("workbuddy-ai", "skill-creator")]
    worker.close()


def _main_receipt(tmp_path: Path) -> tuple[Path, Path]:
    """A normal WorkBuddy install: main hooks plus their receipt."""
    python = tmp_path / "runtime" / "python"
    bridge = tmp_path / "bundle" / "scripts" / "workbuddy_hook_bridge.py"
    for path in (python, bridge):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    state = tmp_path / "workbuddy-observer"
    receipt = state / "workbuddy-hooks-owned.json"
    home = tmp_path / "home"
    (home / ".workbuddy").mkdir(parents=True)
    hooks.install(settings=home / ".workbuddy" / "settings.json", receipt=receipt, python=python, bridge=bridge,
        event_file=tmp_path / "events" / "workbuddy-hook-events.jsonl")
    return receipt, home


def test_sync_installs_hooks_into_workbuddy_ai_only_when_it_is_installed(tmp_path):
    receipt, home = _main_receipt(tmp_path)
    assert hooks.sync_companions(main_receipt=receipt, home=home) == {"workbuddy-ai": {"status": "not_installed"}}
    assert not (receipt.parent / "workbuddy-ai-hooks-owned.json").exists()

    settings = home / ".workbuddy-ai" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"claw": {"keep": True}, "hooks": {"PreToolUse": [{"matcher": "x", "hooks": [{"type": "command", "command": "other"}]}]}}))
    first = hooks.sync_companions(main_receipt=receipt, home=home)["workbuddy-ai"]
    assert first["status"] == "ok" and first["changed"] is True
    raw = json.loads(settings.read_text())
    assert raw["claw"] == {"keep": True}  # the client's own settings are preserved
    commands = [group["hooks"][0]["command"] for groups in raw["hooks"].values() for group in groups]
    assert "other" in commands
    ours = [command for command in commands if "workbuddy_hook_bridge.py" in command]
    assert len(ours) == 5 and all(command.endswith("--client workbuddy-ai") for command in ours)
    main_commands = json.loads((home / ".workbuddy" / "settings.json").read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert ours[0].split(" --stage")[0] == main_commands.split(" --stage")[0]  # same Python and bridge as WorkBuddy

    again = hooks.sync_companions(main_receipt=receipt, home=home)["workbuddy-ai"]
    assert again["status"] == "ok" and again["changed"] is False  # every 30 minutes: no rewrite

    assert hooks.uninstall_companions(main_receipt=receipt, home=home)["workbuddy-ai"]["changed"] is True
    raw = json.loads(settings.read_text())
    assert raw == {"claw": {"keep": True}, "hooks": {"PreToolUse": [{"matcher": "x", "hooks": [{"type": "command", "command": "other"}]}]}}


def test_sync_reports_instead_of_raising(tmp_path):
    receipt, home = _main_receipt(tmp_path)
    (home / ".workbuddy-ai").mkdir()
    (home / ".workbuddy-ai" / "settings.json").write_text("[]")  # not an object
    result = hooks.sync_companions(main_receipt=receipt, home=home)["workbuddy-ai"]
    assert result["status"] == "blocked"


def test_updater_adds_workbuddy_ai_hooks_installed_after_observer(tmp_path, monkeypatch):
    import observer_update

    root = tmp_path / "AIHub"
    bundle = root / "observer" / "bundle"
    (bundle / "scripts").mkdir(parents=True)
    (bundle / "installed-hosts.json").write_text(json.dumps({"hosts": ["codex", "workbuddy-desktop"]}))
    python = tmp_path / "python"
    python.write_text("")
    bridge = bundle / "scripts" / "workbuddy_hook_bridge.py"
    bridge.write_text("")
    home = tmp_path / "home"
    (home / ".workbuddy").mkdir(parents=True)
    receipt = root / "workbuddy-observer" / "workbuddy-hooks-owned.json"
    hooks.install(settings=home / ".workbuddy" / "settings.json", receipt=receipt, python=python, bridge=bridge,
        event_file=tmp_path / "events.jsonl")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    updater = observer_update.Updater(bundle=bundle, system="Darwin")
    assert updater.sync_workbuddy_companions() == {"workbuddy-ai": {"status": "not_installed"}}
    (home / ".workbuddy-ai").mkdir()  # WorkBuddy AI installed and opened later
    assert updater.sync_workbuddy_companions()["workbuddy-ai"]["status"] == "ok"
    assert (root / "workbuddy-observer" / "workbuddy-ai-hooks-owned.json").is_file()
    saved = json.loads((updater.state_dir / "workbuddy-companions.json").read_text())
    assert saved["result"]["workbuddy-ai"]["status"] == "ok"


def test_health_says_which_builds_are_hooked():
    body, _, _ = health_payload("workbuddy", {"status": "ok", "hooked_clients": "workbuddy,workbuddy-ai"},
        now=datetime.now(timezone.utc), totals={})
    attributes = {item["key"]: item["value"] for item in body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
    assert attributes["observer.health.hooked_clients"] == {"stringValue": "workbuddy,workbuddy-ai"}


def test_detection_finds_workbuddy_ai_alone(tmp_path):
    script = Path(__file__).resolve().parents[1] / "bundle" / "scripts" / "observer_detect_clients_macos.sh"
    home = tmp_path / "home"
    (home / ".workbuddy-ai").mkdir(parents=True)
    (home / ".workbuddy-ai" / "settings.json").write_text("{}")
    app = home / "Applications" / "WorkBuddy AI.app" / "Contents"
    app.mkdir(parents=True)
    (app / "Info.plist").write_text("")
    result = subprocess.run(["/bin/bash", str(script)], capture_output=True, text=True, env={**os.environ, "HOME": str(home)})
    assert "workbuddy-desktop\tfound" in result.stdout
    assert "workbuddy-desktop" in [line for line in result.stdout.splitlines() if line.startswith("HOSTS=")][0]
