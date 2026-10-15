"""macOS: Claude Desktop may be missing or installed in ~/Applications; a failing client must
not stop the self-update LaunchAgent from being registered."""
from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "bundle/scripts"


def _detect(home: Path) -> dict[str, tuple[str, str]]:
    env = {**os.environ, "HOME": str(home)}
    out = subprocess.run(["bash", str(SCRIPTS / "observer_detect_clients_macos.sh")], env=env,
                         capture_output=True, text=True, check=True).stdout
    rows = {}
    for line in out.splitlines():
        if not line.startswith("HOSTS="):
            host, state, reason = line.split("\t")
            rows[host] = (state, reason)
    return rows


def _claude_app(parent: Path) -> Path:
    app = parent / "Claude.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": "1.2.3"}))
    return app


def test_claude_data_without_the_app_is_not_offered(tmp_path):
    home = tmp_path / "home"
    (home / "Library/Application Support/Claude/IndexedDB/https_claude.ai_0.indexeddb.leveldb").mkdir(parents=True)
    if Path("/Applications/Claude.app").exists():  # a real Mac with Claude installed
        return
    state, reason = _detect(home)["claude"]
    assert state == "missing" and "app is not installed" in reason
    _claude_app(home / "Applications")
    assert _detect(home)["claude"][0] == "found"


def test_doctor_reads_the_claude_version_from_user_applications(tmp_path):
    import observer_doctor

    if Path("/Applications/Claude.app").exists():
        return
    app = _claude_app(tmp_path / "home/Applications")
    assert observer_doctor.macos_app_path("claude", home=tmp_path / "home") == app
    assert observer_doctor.macos_app_path("claude", home=tmp_path / "nobody") == Path("/Applications/Claude.app")


def test_bootstrap_registers_the_updater_even_when_a_client_fails():
    text = (SCRIPTS / "bootstrap_observer_macos.sh").read_text()
    install = next(line for line in text.splitlines() if '"$observer_installer" install' in line)
    assert install.endswith("|| observer_install_rc=$?")
    assert text.index("|| observer_install_rc=$?") < text.index("observer_update.py") < text.rindex('exit "$observer_install_rc"')
    assert text.rstrip().endswith('exit "$observer_install_rc"')


def test_any_subset_of_clients_is_detected(tmp_path):
    if Path("/Applications/Claude.app").exists() or Path("/Applications/WorkBuddy.app").exists():
        return
    home = tmp_path / "home"
    home.mkdir()
    assert _detect(home) == {k: v for k, v in _detect(home).items() if v[0] == "missing"}
    (home / ".workbuddy").mkdir()
    (home / ".workbuddy/settings.json").write_text("{}")
    state, reason = _detect(home)["workbuddy-desktop"]
    assert state == "missing" and "app is not installed" in reason
    app = home / "Applications/WorkBuddy.app/Contents"
    app.mkdir(parents=True)
    (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": "5.7.7"}))
    (home / ".codex/sessions").mkdir(parents=True)
    rows = _detect(home)
    assert [h for h, (state, _) in rows.items() if state == "found"] == ["codex", "workbuddy-desktop"]


def _fake_manifest(minimum, maximum):
    return {"connectors": {"claude": {"runtime": "claude", "min_client_version": minimum, "max_client_version": maximum}}}


def test_newer_client_version_is_installed_with_a_note_older_is_skipped(tmp_path, monkeypatch):
    import observer_doctor

    app = _claude_app(tmp_path)  # version 1.2.3
    monkeypatch.setattr(observer_doctor.sys, "platform", "darwin")
    check = lambda lo, hi: observer_doctor._compatibility_for_host("claude", _fake_manifest(lo, hi), app_path=app)
    assert check("1.0.0", "1.9.9")["status"] == "supported"
    newer = check("1.0.0", "1.1.0")
    assert newer["status"] == "untested" and "newer than the tested range" in newer["reason"]
    assert check("2.0.0", "2.9.9")["status"] == "unsupported"


def test_compatibility_only_prints_one_line_per_client(monkeypatch, capsys):
    import observer_doctor

    results = {"codex": {"status": "supported", "client_version": "0.158.0"}, "claude": {"status": "unsupported", "reason": "too\told"}}
    monkeypatch.setattr(observer_doctor, "_compatibility_for_host", lambda host, manifest, **_: results[host])
    assert observer_doctor.main(["--compatibility-only", "--host", "codex", "--host", "claude"]) == 0
    assert capsys.readouterr().out.splitlines() == ["codex\tsupported\t0.158.0", "claude\tunsupported\ttoo old"]


def _precheck_block() -> str:
    text = (SCRIPTS / "bootstrap_observer_macos.sh").read_text()
    start = text.index("observer_result_file=${OBSERVER_RESULT_FILE:-}")
    return text[start:text.index("observer_dsh=0", start)]


def test_bootstrap_skips_only_too_old_clients(tmp_path):
    fake = tmp_path / "python"
    fake.write_text("#!/bin/bash\nprintf 'codex\\tsupported\\t0.1\\nclaude\\tunsupported\\ttoo old\\nworkbuddy-desktop\\tuntested\\tnewer\\n'\n")
    fake.chmod(0o755)
    result = tmp_path / "result.tsv"
    script = ("set -euo pipefail\nfail() { echo \"FAIL $1\"; exit 2; }\nobserver_installer_args=()\n"
              f"observer_python={fake}\nobserver_source={tmp_path}\nobserver_hosts=(codex claude workbuddy-desktop)\n"
              + _precheck_block() + 'echo "HOSTS=${observer_hosts[*]}"\n')
    out = subprocess.run(["bash", "-c", script], env={**os.environ, "OBSERVER_RESULT_FILE": str(result)},
                         capture_output=True, text=True, check=True).stdout
    assert out.strip().splitlines()[-1] == "HOSTS=codex workbuddy-desktop"
    assert result.read_text().splitlines() == ["skipped\tclaude\ttoo old", "note\tworkbuddy-desktop\tnewer"]


def test_bootstrap_stops_when_every_client_is_too_old(tmp_path):
    fake = tmp_path / "python"
    fake.write_text("#!/bin/bash\nprintf 'claude\\tunsupported\\ttoo old\\n'\n")
    fake.chmod(0o755)
    script = ("set -euo pipefail\nfail() { echo \"FAIL $1\"; exit 2; }\nobserver_installer_args=()\n"
              f"observer_python={fake}\nobserver_source={tmp_path}\nobserver_hosts=(claude)\n" + _precheck_block())
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert done.returncode == 2 and "FAIL none of the selected clients" in done.stdout
