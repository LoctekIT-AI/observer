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


def _reuse_fails(monkeypatch, message):
    def access_token(self):
        raise auth.ObserverAuthError(message)
    monkeypatch.setattr(auth.ObserverAuthorizationClient, "access_token", access_token)


def test_expired_login_logs_in_again(tmp_path, monkeypatch, capsys):
    # A computer that was uninstalled or off for a month: reinstalling asks for a new login.
    config, calls = _setup(tmp_path, monkeypatch)
    _reuse_fails(monkeypatch, "Observer refresh token has expired; authorization is required again")
    assert auth.main(["--client-type", "codex", "--config-file", str(config), "--auth-origin", TEST_ORIGIN]) == 0
    assert calls == [("authorize", TEST_ORIGIN)]
    assert "登录已过期" in capsys.readouterr().err


def test_refused_login_logs_in_again(tmp_path, monkeypatch):
    config, calls = _setup(tmp_path, monkeypatch)
    _reuse_fails(monkeypatch, "Observer authorization rejected: refresh token is invalid")
    assert auth.main(["--client-type", "codex", "--config-file", str(config), "--auth-origin", TEST_ORIGIN]) == 0
    assert calls == [("authorize", TEST_ORIGIN)]


def test_network_failure_does_not_ask_for_a_new_login(tmp_path, monkeypatch, capsys):
    config, calls = _setup(tmp_path, monkeypatch)
    _reuse_fails(monkeypatch, "Observer authorization request failed: URLError")
    assert auth.main(["--client-type", "codex", "--config-file", str(config), "--auth-origin", TEST_ORIGIN]) == 2
    assert calls == []
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["status"] == "blocked"


def test_installers_check_a_found_login_instead_of_skipping_it():
    from pathlib import Path
    scripts = Path(__file__).resolve().parents[1] / "bundle/scripts"
    mac = (scripts / "bootstrap_observer_macos.sh").read_text()
    assert "reusing it without a new login" not in mac
    assert mac.count('scripts/observer_authorize.py" --client-type') == 2
    win = (scripts / "install_observer_windows.ps1").read_text(encoding="utf-8-sig")
    assert "reusing it without a new login" not in win
    assert win.count("'observer_authorize.py'),'--client-type'") == 2
