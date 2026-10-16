"""The Feishu login page opens by itself; the install window says so and shows the address."""
import subprocess

from cowork_runtime.telemetry import observer_auth


def test_macos_uses_open_to_bring_the_browser_up(monkeypatch):
    calls = []
    monkeypatch.setattr(observer_auth.sys, "platform", "darwin")
    monkeypatch.setattr(observer_auth.os.path, "exists", lambda p: p == "/usr/bin/open")
    monkeypatch.setattr(observer_auth.subprocess, "run", lambda args, **kw: calls.append(args) or subprocess.CompletedProcess(args, 0))
    assert observer_auth._open_system_browser("http://example/authorize?x=1") is True
    assert calls == [["/usr/bin/open", "http://example/authorize?x=1"]]


def test_falls_back_to_webbrowser_when_open_fails(monkeypatch):
    monkeypatch.setattr(observer_auth.sys, "platform", "darwin")
    monkeypatch.setattr(observer_auth.os.path, "exists", lambda p: True)
    def broken(*a, **k):
        raise OSError("no open")
    monkeypatch.setattr(observer_auth.subprocess, "run", broken)
    monkeypatch.setattr(observer_auth.webbrowser, "open", lambda url: True)
    assert observer_auth._open_system_browser("http://example") is True


def test_install_window_shows_the_address_and_time_limit(monkeypatch, capsys):
    import pytest

    monkeypatch.setattr(observer_auth, "_open_system_browser", lambda url: False)
    client = observer_auth.ObserverAuthorizationClient(origin="http://192.168.17.121:38090/observer", client_type="codex",
                                                       installation_id="install-test", store=None, timeout_seconds=0.2)
    with pytest.raises(observer_auth.ObserverAuthError):
        client.authorize()
    err = capsys.readouterr().err.splitlines()
    assert "复制到浏览器" in err[0] and err[1].startswith("http://192.168.17.121:38090/observer/authorize?")
