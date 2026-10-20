"""Production rollout: a computer moved from the test site to production logs in again."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import observer_authorize as auth

TEST_ORIGIN = "http://192.168.17.121:38090/observer"
PROD_ORIGIN = "https://itsub.loctek.com/observer"


def _setup(tmp_path, monkeypatch):
    config = tmp_path / "customer.env"
    account = "observer-token-" + hashlib.sha256(str(config.absolute()).encode()).hexdigest()[:32]
    calls = []

    class FakeClient:
        def __init__(self, *, origin, **_kwargs):
            self.origin = origin

        def authorize(self):
            calls.append(("authorize", self.origin))
            return SimpleNamespace(telemetry_endpoint=self.origin + "/otel/v1/traces")

        def access_token(self):
            calls.append(("reuse", self.origin))
            return SimpleNamespace(telemetry_endpoint=self.origin + "/otel/v1/traces")

    monkeypatch.setattr(auth, "ObserverAuthorizationClient", FakeClient)
    monkeypatch.setattr(auth, "ObserverTokenStore", lambda *_a, **_k: SimpleNamespace(delete=lambda: None))
    monkeypatch.setattr(auth, "read_secret", lambda **_k: "old-token")
    monkeypatch.setattr(auth, "store_secret", lambda **_k: None)
    scheme = "dpapi" if auth.os.name == "nt" else "keychain"
    monkeypatch.setattr(auth, "secret_reference", lambda service, acct: f"{scheme}://{service}/{acct}")
    ref = auth.secret_reference(auth.SERVICE, account)
    config.write_text(
        f"OBSERVER_AUTH_ORIGIN={TEST_ORIGIN}\nOBSERVER_AUTH_TOKEN_REF={ref}\n"
        "OBSERVER_CLIENT_TYPE=codex\nOBSERVER_INSTALLATION_ID=observer-codex-0123456789abcdef01234567\n"
    )
    return config, calls


def test_same_site_reuses_login(tmp_path, monkeypatch):
    config, calls = _setup(tmp_path, monkeypatch)
    assert auth.main(["--client-type", "codex", "--config-file", str(config), "--auth-origin", TEST_ORIGIN]) == 0
    assert calls == [("reuse", TEST_ORIGIN)]


def test_new_site_logs_in_again_and_keeps_installation_id(tmp_path, monkeypatch, capsys):
    config, calls = _setup(tmp_path, monkeypatch)
    assert auth.main(["--client-type", "codex", "--config-file", str(config), "--auth-origin", PROD_ORIGIN]) == 0
    assert calls == [("authorize", PROD_ORIGIN)]
    assert f"OBSERVER_AUTH_ORIGIN={PROD_ORIGIN}" in config.read_text()
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["installation_id"] == "observer-codex-0123456789abcdef01234567"


def test_installers_reuse_a_login_only_for_the_same_site():
    from pathlib import Path
    scripts = Path(__file__).resolve().parents[1] / "bundle/scripts"
    assert 'grep -qxF "OBSERVER_AUTH_ORIGIN=${observer_auth_origin%/}"' in (scripts / "bootstrap_observer_macos.sh").read_text()
    assert "'^OBSERVER_AUTH_ORIGIN=' + [regex]::Escape($AuthOrigin.TrimEnd('/'))" in (scripts / "install_observer_windows.ps1").read_text(encoding="utf-8-sig")
