"""Layer 4: signed self-update and metadata replay after an update."""
from __future__ import annotations

import io
import json
import os
import plistlib
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import observer_update as update
from cowork_runtime.observer import replay as replay_module
from cowork_runtime.observer.replay import ReplayLedger, apply_due

SEED = bytes(range(32))
PUBLIC = update.ed25519_public_key(SEED)
T0 = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- Ed25519


def test_ed25519_matches_rfc8032_vectors():
    seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
    public = update.ed25519_public_key(seed)
    assert public.hex() == "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    signature = update.ed25519_sign(seed, b"")
    assert signature.hex() == ("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555"
                               "fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
    assert update.ed25519_verify(public, b"", signature)
    seed = bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")
    assert update.ed25519_sign(seed, b"\x72").hex() == ("92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
                                                        "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00")


def test_ed25519_rejects_changed_message_key_or_signature():
    signature = update.ed25519_sign(SEED, b"message")
    assert update.ed25519_verify(PUBLIC, b"message", signature)
    assert not update.ed25519_verify(PUBLIC, b"messagE", signature)
    assert not update.ed25519_verify(update.ed25519_public_key(bytes(32)), b"message", signature)
    assert not update.ed25519_verify(PUBLIC, b"message", signature[:-1] + bytes([signature[-1] ^ 1]))
    assert not update.ed25519_verify(PUBLIC, b"message", b"\x00" * 64)


# --------------------------------------------------------------------------- channel entries


def _zip(version: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"bundle_version": version}))
        archive.writestr("scripts/observer_update.py", "# new version\n")
    return buffer.getvalue()


def test_channel_entry_signature_binds_every_field():
    data = _zip("0.2.27")
    entry = update.make_channel_entry(channel="test", version="0.2.27", archive=data, seed=SEED)
    assert entry["tag"] == "v0.2.27-test" and entry["asset"] == "observer-v0.2.27-test.zip"
    assert update.verify_channel_entry(dict(entry), channel="test", public_key=PUBLIC)["version"] == "0.2.27"
    for field, value in (("sha256", "0" * 64), ("size", entry["size"] + 1)):
        with pytest.raises(update.UpdateError) as caught:
            update.verify_channel_entry({**entry, field: value}, channel="test", public_key=PUBLIC)
        assert caught.value.code == "signature_invalid"
    with pytest.raises(update.UpdateError) as caught:  # a test release never reaches release installs
        update.verify_channel_entry(entry, channel="release", public_key=PUBLIC)
    assert caught.value.code == "channel_invalid"
    renamed = update.make_channel_entry(channel="test", version="0.2.27", archive=data, seed=SEED)
    renamed["asset"] = "observer-v0.2.99-test.zip"
    with pytest.raises(update.UpdateError):
        update.verify_channel_entry(renamed, channel="test", public_key=PUBLIC)


# --------------------------------------------------------------------------- updater flow


def _bundle(tmp_path: Path, *, version="0.2.26", key: bytes | None = PUBLIC, hosts=("codex", "workbuddy-desktop")) -> Path:
    bundle = tmp_path / "Observer" / "bundle"
    bundle.mkdir(parents=True)
    (bundle / "manifest.json").write_text(json.dumps({"bundle_version": version}))
    (bundle / "update.json").write_text(json.dumps({"repo": "example/observer", "channel": "test", "public_key": key.hex() if key else ""}))
    (bundle / "installed-hosts.json").write_text(json.dumps({"hosts": list(hosts), "untracked_hosts": []}))
    return bundle


def _config(tmp_path: Path, login=True) -> Path:
    path = tmp_path / "customer.env"
    path.write_text("OBSERVER_AUTH_ORIGIN=http://example\n" + ("OBSERVER_AUTH_TOKEN_REF=dpapi:abc\n" if login else ""))
    return path


class Net:
    def __init__(self, channel: dict | None, archive: bytes = b""):
        self.channel, self.archive, self.urls = channel, archive, []

    def __call__(self, url, *, max_bytes, timeout=120):
        self.urls.append(url)
        if url.endswith(".json"):
            if self.channel is None:
                raise update.UpdateError("download_failed", "offline")
            return json.dumps(self.channel).encode()
        return self.archive


class Installer:
    def __init__(self, bundle: Path, *, code=0, installs=True):
        self.bundle, self.code, self.installs, self.calls = bundle, code, installs, []

    def __call__(self, *, source, archive, hosts, log):
        self.calls.append({"source": source, "archive": archive, "hosts": hosts})
        if self.installs:
            (self.bundle / "manifest.json").write_text((source / "manifest.json").read_text())
        return self.code


def _updater(tmp_path, bundle, net, installer, *, login=True, now=T0):
    clock = {"now": now}
    updater = update.Updater(bundle=bundle, config_file=_config(tmp_path, login), system="Windows",
                             fetcher=net, runner=installer, now=lambda: clock["now"])
    return updater, clock


def test_newer_signed_version_is_installed_for_the_installed_clients(tmp_path):
    bundle = _bundle(tmp_path)
    data = _zip("0.2.27")
    net = Net(update.make_channel_entry(channel="test", version="0.2.27", archive=data, seed=SEED), data)
    installer = Installer(bundle)
    updater, _ = _updater(tmp_path, bundle, net, installer)
    result = updater.run()
    assert result["status"] == "updated" and result["updated_to"] == "0.2.27" and result["current_version"] == "0.2.27"
    assert installer.calls[0]["hosts"] == ["codex", "workbuddy-desktop"]
    assert net.urls == ["https://raw.githubusercontent.com/example/observer/main/channels/test.json",
                        "https://github.com/example/observer/releases/download/v0.2.27-test/observer-v0.2.27-test.zip"]
    # the work folder is outside the installed bundle and removed afterwards
    assert not installer.calls[0]["source"].is_relative_to(bundle) and not (bundle.parent / "updater" / "work").exists()
    assert json.loads((bundle.parent / "updater" / "status.json").read_text())["status"] == "updated"


def test_same_or_older_version_and_bad_signature_are_never_installed(tmp_path):
    bundle = _bundle(tmp_path)
    installer = Installer(bundle)
    for version in ("0.2.26", "0.2.9"):
        data = _zip(version)
        net = Net(update.make_channel_entry(channel="test", version=version, archive=data, seed=SEED), data)
        updater, _ = _updater(tmp_path, bundle, net, installer)
        assert updater.run(force=True)["status"] == "up_to_date"
    data = _zip("0.2.27")
    forged = update.make_channel_entry(channel="test", version="0.2.27", archive=data, seed=bytes(32))
    updater, _ = _updater(tmp_path, bundle, Net(forged, data), installer)
    assert updater.run(force=True)["status"] == "signature_invalid"
    assert installer.calls == []


def test_archive_must_match_the_signed_digest_and_failures_back_off(tmp_path):
    bundle = _bundle(tmp_path)
    data = _zip("0.2.27")
    entry = update.make_channel_entry(channel="test", version="0.2.27", archive=data, seed=SEED)
    installer = Installer(bundle)
    updater, clock = _updater(tmp_path, bundle, Net(entry, data + b"tampered"), installer)
    assert updater.run()["status"] == "archive_mismatch"
    clock["now"] += timedelta(hours=2)
    assert updater.run()["status"] == "waiting_retry"
    clock["now"] += timedelta(hours=6)
    assert updater.run()["status"] == "archive_mismatch"
    clock["now"] += timedelta(hours=7)
    assert updater.run()["status"] == "archive_mismatch"
    clock["now"] += timedelta(hours=7)
    assert updater.run()["status"] == "gave_up"  # three attempts per version
    assert installer.calls == []


def test_failed_install_is_reported(tmp_path):
    bundle = _bundle(tmp_path)
    data = _zip("0.2.27")
    entry = update.make_channel_entry(channel="test", version="0.2.27", archive=data, seed=SEED)
    updater, _ = _updater(tmp_path, bundle, Net(entry, data), Installer(bundle, code=1, installs=False))
    result = updater.run()
    assert result["status"] == "install_failed" and result["attempts"] == {"0.2.27": 1}


def test_no_background_login_disabled_without_key_and_check_interval(tmp_path):
    data = _zip("0.2.27")
    entry = update.make_channel_entry(channel="test", version="0.2.27", archive=data, seed=SEED)
    bundle = _bundle(tmp_path)
    installer = Installer(bundle)
    updater, clock = _updater(tmp_path, bundle, Net(entry, data), installer, login=False)
    assert updater.run()["status"] == "login_missing" and installer.calls == []
    net = Net(entry, data)
    updater.fetch = net
    clock["now"] += timedelta(minutes=20)
    assert updater.run()["status"] == "login_missing" and net.urls == []  # checked less than an hour ago
    other = _bundle(tmp_path / "nokey", key=None)
    disabled, _ = _updater(tmp_path, other, Net(entry, data), installer)
    assert disabled.run()["status"] == "disabled"
    nohosts = _bundle(tmp_path / "nohosts", hosts=())
    empty, _ = _updater(tmp_path, nohosts, Net(entry, data), installer)
    assert empty.run()["status"] == "no_hosts"


def test_offline_channel_is_a_status_not_a_crash(tmp_path):
    bundle = _bundle(tmp_path)
    updater, _ = _updater(tmp_path, bundle, Net(None), Installer(bundle))
    assert updater.run()["status"] == "channel_unavailable"


def test_unsafe_archive_entries_are_rejected(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../outside.txt", "x")
    path = tmp_path / "bad.zip"
    path.write_bytes(buffer.getvalue())
    with pytest.raises(update.UpdateError):
        update.safe_extract(path, tmp_path / "out")
    assert not (tmp_path / "outside.txt").exists()


def test_installer_commands_reuse_the_login(tmp_path):
    updater = update.Updater(bundle=tmp_path, system="Windows")
    command = updater.installer_command(source=Path("C:/w/source"), archive=Path("C:/w/a.zip"), hosts=["codex", "dsh"])
    assert command[-5:] == ["-Archive", str(Path("C:/w/a.zip")), "-Hosts", "codex,dsh", "-Unattended"]
    mac = update.Updater(bundle=tmp_path, system="Darwin").installer_command(source=Path("/w/s"), archive=Path("/w/a.zip"), hosts=["codex", "claude"])
    assert mac[2:] == ["--archive", "/w/a.zip", "--host", "codex", "--host", "claude"] and "--authorize" not in mac


def test_macos_launch_agent_is_written_once_and_not_reloaded_during_an_update(tmp_path, monkeypatch):
    bundle = _bundle(tmp_path)
    log = tmp_path / "launchctl.log"
    launchctl = tmp_path / "launchctl"
    launchctl.write_text(f"#!/bin/sh\necho \"$@\" >> '{log}'\n[ \"$1\" = print ] && [ -f '{tmp_path}/loaded' ] && exit 0\n[ \"$1\" = bootstrap ] && touch '{tmp_path}/loaded'\n[ \"$1\" = print ] && exit 1\nexit 0\n")
    launchctl.chmod(0o755)
    agents = tmp_path / "LaunchAgents"
    python = tmp_path / "python"
    monkeypatch.delenv(update.CHILD_ENV, raising=False)
    assert update.register_macos(python=python, bundle=bundle, agents_dir=agents, launchctl=str(launchctl)) == "registered"
    plist = plistlib.loads((agents / f"{update.LAUNCH_AGENT_LABEL}.plist").read_bytes())
    assert plist["ProgramArguments"] == [str(python), str(bundle / "scripts" / "observer_update.py"), "run"]
    assert plist["AbandonProcessGroup"] is True and plist["StartInterval"] == 6 * 3600
    assert update.register_macos(python=python, bundle=bundle, agents_dir=agents, launchctl=str(launchctl)) == "unchanged"
    monkeypatch.setenv(update.CHILD_ENV, "1")
    assert update.register_macos(python=tmp_path / "python2", bundle=bundle, agents_dir=agents, launchctl=str(launchctl)) == "written_reload_at_login"
    assert "bootout" not in log.read_text()


# --------------------------------------------------------------------------- replay


class Registry:
    def __init__(self):
        self.cursors = {}

    def cursor(self, installation_id, path):
        return self.cursors.get((installation_id, path))

    def save_cursor(self, cursor):
        self.cursors[(cursor.installation_id, cursor.source_path)] = cursor


def test_replay_rewinds_only_after_a_version_change(tmp_path):
    from cowork_runtime.adapters.registry import CodexCursor

    source = tmp_path / "rollout.jsonl"
    source.write_text("x" * 100)
    identity = lambda path: "id-1"  # noqa: E731
    registry = Registry()
    registry.save_cursor(CodexCursor("inst", str(source), "id-1", 100, T0))
    old = ReplayLedger(tmp_path, version="0.2.26")
    old.note("inst", str(source), "id-1", 40)
    old.note("inst", str(source), "id-1", 60)  # the earliest position is kept
    assert apply_due(old, registry, identity) == (0, set())  # same version: nothing to replay
    new = ReplayLedger(tmp_path, version="0.2.27")
    assert apply_due(new, registry, identity) == (1, set())
    assert registry.cursor("inst", str(source)).offset == 40
    assert apply_due(new, registry, identity) == (0, set())  # replayed once per update
    old.note("inst", str(source), "id-1", 10, guarded=True)
    assert apply_due(new, registry, lambda path: "replaced") == (0, set())  # a replaced file is not replayed


def test_dsh_worker_replays_skipped_lines_after_an_update(tmp_path, monkeypatch):
    from cowork_runtime.observer.dsh_worker import DshObserverWorker

    from test_layer3_degraded import _dsh_record

    source_dir = tmp_path / "dsh"
    source_dir.mkdir()
    (source_dir / "component-events.jsonl").write_text(_dsh_record(event_id="dsh:e1") + _dsh_record(event_id="dsh:e2", observation_kind="future_kind"))
    monkeypatch.setattr(replay_module, "bundle_version", lambda: "0.2.26")
    worker = DshObserverWorker(source_root=source_dir, state_dir=tmp_path / "state", flush=False)
    worker.run_once()
    pending = worker.outbox.count_by_status()
    worker.close()
    points = json.loads((tmp_path / "state" / "replay.json").read_text())["points"]
    assert len(points) == 1 and points[0]["version"] == "0.2.26" and points[0]["offset"] > 0

    monkeypatch.setattr(replay_module, "bundle_version", lambda: "0.2.27")
    worker = DshObserverWorker(source_root=source_dir, state_dir=tmp_path / "state", flush=False)
    worker.run_once()
    assert worker.outbox.count_by_status() == pending  # nothing delivered twice
    health = json.loads((tmp_path / "state" / "health.json").read_text())
    assert health["totals"]["replayed_sources"] == 1
    worker.close()
    points = json.loads((tmp_path / "state" / "replay.json").read_text())["points"]
    assert [point["version"] for point in points] == ["0.2.27"]  # still unreadable: noted for the next update


def test_macos_update_skips_clients_that_are_no_longer_detected(tmp_path, monkeypatch):
    bundle = _bundle(tmp_path, hosts=("codex", "dsh"))
    data = _zip("0.2.27")
    entry = update.make_channel_entry(channel="test", version="0.2.27", archive=data, seed=SEED)
    installer = Installer(bundle)
    updater = update.Updater(bundle=bundle, config_file=_config(tmp_path), system="Darwin", fetcher=Net(entry, data),
                             runner=installer, now=lambda: T0)
    monkeypatch.setattr(updater, "detect_hosts", lambda source: {"codex", "claude"})
    result = updater.run()
    assert result["status"] == "updated" and result["skipped_hosts"] == ["dsh"]
    assert installer.calls[0]["hosts"] == ["codex"]


def test_health_is_sent_again_after_an_update(tmp_path, monkeypatch):
    from cowork_runtime.telemetry import health

    sent = []
    reporter = health.HealthReporter(tmp_path, "codex", send=lambda body, trace, span: sent.append(body))
    monkeypatch.setattr(health, "bundle_version", lambda: "0.2.26")
    assert reporter.observe({"status": "ok"}, now=T0)
    assert not reporter.observe({"status": "ok"}, now=T0 + timedelta(minutes=5))
    monkeypatch.setattr(health, "bundle_version", lambda: "0.2.27")
    assert reporter.observe({"status": "ok"}, now=T0 + timedelta(minutes=6))
