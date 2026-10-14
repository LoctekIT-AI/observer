"""PKCE authorization and protected token storage for the Observer service.

The Observer service deliberately does not expose the existing Langfuse
credentials to clients.  This module keeps the short lived access token and
its rotating refresh token in the platform protected store, and exposes only
the bearer header needed by the metadata exporter.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import math
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .keychain import KeychainError, delete_secret, read_secret, store_secret


class ObserverAuthError(RuntimeError):
    """The Observer authorization state is missing, invalid, or expired."""


def _single_line(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(c in value for c in "\r\n\x00"):
        raise ObserverAuthError(f"Observer token response field {name} is invalid")
    return value.strip()


def _absolute_http_url(value: str, name: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname
        parsed.port  # validate malformed ports at the boundary
    except ValueError as exc:
        raise ObserverAuthError(f"Observer {name} must be an absolute HTTPS URL") from exc
    if not hostname or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ObserverAuthError(f"Observer {name} must be an absolute HTTPS URL")
    if parsed.scheme == "http":
        # Test environments on the internal network (e.g. 192.168.17.121) have no TLS.
        # Plain http is accepted only for private, non-loopback IP literals so a
        # public or production domain can never be downgraded.
        if not _is_private_ip_literal(hostname):
            raise ObserverAuthError(f"Observer {name} must be an absolute HTTPS URL (http is only allowed for private IP addresses)")
    elif parsed.scheme != "https":
        raise ObserverAuthError(f"Observer {name} must be an absolute HTTPS URL")
    return value.rstrip("/")


def _is_private_ip_literal(hostname: str) -> bool:
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return address.is_private and not address.is_loopback


@dataclass(frozen=True)
class ObserverToken:
    access_token: str
    refresh_token: str
    access_expires_at: float
    refresh_expires_at: float
    telemetry_endpoint: str
    token_type: str = "Bearer"

    @classmethod
    def from_response(cls, payload: object, *, now: float | None = None) -> "ObserverToken":
        if not isinstance(payload, dict):
            raise ObserverAuthError("Observer token response must be a JSON object")
        access = _single_line(payload.get("access_token"), "access_token")
        refresh = _single_line(payload.get("refresh_token"), "refresh_token")
        endpoint = _absolute_http_url(_single_line(payload.get("telemetry_endpoint"), "telemetry_endpoint"), "telemetry_endpoint")
        token_type = _single_line(payload.get("token_type", "Bearer"), "token_type")
        if token_type.lower() != "bearer":
            raise ObserverAuthError("Observer token_type must be Bearer")
        try:
            access_ttl = int(payload["expires_in"])
            refresh_ttl = int(payload["refresh_expires_in"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ObserverAuthError("Observer token expiry values are invalid") from exc
        if access_ttl <= 0 or refresh_ttl <= 0:
            raise ObserverAuthError("Observer token expiry values must be positive")
        timestamp = time.time() if now is None else now
        return cls(access, refresh, timestamp + access_ttl, timestamp + refresh_ttl, endpoint, token_type)

    @classmethod
    def from_json(cls, raw: str) -> "ObserverToken":
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ObserverAuthError("Observer token store contains invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ObserverAuthError("Observer token store must contain a JSON object")
        required = ("access_token", "refresh_token", "access_expires_at", "refresh_expires_at", "telemetry_endpoint")
        if any(key not in payload for key in required):
            raise ObserverAuthError("Observer token store is missing required fields")
        try:
            access_expires_at = float(payload["access_expires_at"])
            refresh_expires_at = float(payload["refresh_expires_at"])
        except (TypeError, ValueError) as exc:
            raise ObserverAuthError("Observer token store expiry values are invalid") from exc
        if not math.isfinite(access_expires_at) or not math.isfinite(refresh_expires_at) or access_expires_at <= 0 or refresh_expires_at <= 0:
            raise ObserverAuthError("Observer token store expiry values must be positive")
        token_type = _single_line(payload.get("token_type", "Bearer"), "token_type")
        if token_type.lower() != "bearer":
            raise ObserverAuthError("Observer token_type must be Bearer")
        return cls(
            _single_line(payload["access_token"], "access_token"),
            _single_line(payload["refresh_token"], "refresh_token"),
            access_expires_at,
            refresh_expires_at,
            _absolute_http_url(_single_line(payload["telemetry_endpoint"], "telemetry_endpoint"), "telemetry_endpoint"),
            token_type,
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "access_expires_at": self.access_expires_at,
                "refresh_expires_at": self.refresh_expires_at,
                "telemetry_endpoint": self.telemetry_endpoint,
                "token_type": self.token_type,
            },
            separators=(",", ":"),
            sort_keys=True,
        )


@dataclass(frozen=True)
class ObserverTokenStore:
    service: str
    account: str

    def load(self) -> ObserverToken:
        try:
            return ObserverToken.from_json(read_secret(service=self.service, account=self.account))
        except (KeychainError, ObserverAuthError) as exc:
            raise ObserverAuthError(f"Observer protected token is unavailable: {exc}") from exc

    def save(self, token: ObserverToken) -> None:
        try:
            store_secret(service=self.service, account=self.account, secret=token.to_json())
        except KeychainError as exc:
            raise ObserverAuthError(f"Observer protected token could not be stored: {exc}") from exc

    def delete(self) -> None:
        try:
            delete_secret(service=self.service, account=self.account)
        except KeychainError as exc:
            raise ObserverAuthError(f"Observer protected token could not be removed: {exc}") from exc


@contextmanager
def _refresh_lock(path: Path | None):
    """Serialize refresh-token rotation across host workers in one install."""
    if path is None:
        yield
        return
    path = path.expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def pkce_pair() -> tuple[str, str, str]:
    """Return state, verifier, and an S256 code challenge."""
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return state, verifier, challenge


def _json_request(url: str, payload: dict[str, str], *, timeout: float) -> dict[str, Any]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except Exception as exc:  # urllib has platform-specific socket errors; preserve the native cause.
        raise ObserverAuthError(f"Observer authorization request failed: {type(exc).__name__}") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ObserverAuthError("Observer authorization response is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ObserverAuthError("Observer authorization response must be a JSON object")
    if "error" in value:
        description = value.get("error_description") or value.get("error")
        raise ObserverAuthError(f"Observer authorization rejected: {description}")
    return value


class _CallbackHandler(BaseHTTPRequestHandler):
    server: "_CallbackServer"

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        self.server.result = {
            "path": parsed.path,
            "code": query.get("code", [""])[0],
            "state": query.get("state", [""])[0],
            "error": query.get("error", [""])[0],
            "error_description": query.get("error_description", [""])[0],
        }
        body = b"Observer authorization finished. You can close this window."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
        self.server.ready.set()

    def log_message(self, format: str, *args: object) -> None:
        return


class _CallbackServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _CallbackHandler)
        self.ready = threading.Event()
        self.result: dict[str, str] | None = None


@dataclass
class ObserverAuthorizationClient:
    origin: str
    client_type: str
    installation_id: str
    store: ObserverTokenStore
    timeout_seconds: float = 300.0
    open_browser: bool = True
    refresh_lock_path: Path | None = None

    def __post_init__(self) -> None:
        self.origin = _absolute_http_url(self.origin, "origin")
        if self.client_type not in {"dsh", "codex", "claude", "workbuddy-desktop"}:
            raise ObserverAuthError("unsupported Observer client type")
        if not 1 <= len(self.installation_id) <= 200 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-" for c in self.installation_id):
            raise ObserverAuthError("installation_id contains unsupported characters")
        if self.timeout_seconds <= 0:
            raise ObserverAuthError("authorization timeout must be positive")

    def authorize(self) -> ObserverToken:
        state, verifier, challenge = pkce_pair()
        callback = _CallbackServer()
        callback_thread = threading.Thread(target=callback.serve_forever, daemon=True)
        callback_thread.start()
        redirect_uri = f"http://127.0.0.1:{callback.server_port}/callback"
        params = {
            "client_id": "observer",
            "client_type": self.client_type,
            "installation_id": self.installation_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        authorize_url = f"{self.origin}/authorize?{urllib.parse.urlencode(params)}"
        try:
            if self.open_browser:
                if not webbrowser.open(authorize_url):
                    raise ObserverAuthError("the system browser could not be opened")
            else:
                print(authorize_url, flush=True)
            if not callback.ready.wait(self.timeout_seconds):
                raise ObserverAuthError("Observer authorization callback timed out")
            result = callback.result or {}
            if result.get("path") != "/callback":
                raise ObserverAuthError("Observer authorization callback path is invalid")
            if result.get("state") != state:
                raise ObserverAuthError("Observer authorization state does not match")
            if result.get("error"):
                raise ObserverAuthError(f"Observer authorization rejected: {result.get('error_description') or result['error']}")
            code = result.get("code", "")
            if not code:
                raise ObserverAuthError("Observer authorization callback did not contain a code")
            payload = _json_request(
                f"{self.origin}/oauth/token",
                {"grant_type": "authorization_code", "client_id": "observer", "code": code, "redirect_uri": redirect_uri, "code_verifier": verifier},
                timeout=min(self.timeout_seconds, 30.0),
            )
            token = ObserverToken.from_response(payload)
            self.store.save(token)
            return token
        finally:
            callback.shutdown()
            callback.server_close()
            callback_thread.join(timeout=2)

    def access_token(self, *, now: float | None = None) -> ObserverToken:
        timestamp = time.time() if now is None else now
        token = self.store.load()
        if token.access_expires_at - timestamp > 60:
            return token
        with _refresh_lock(self.refresh_lock_path):
            # Another worker may have rotated the token while this process
            # waited for the lock.  Reload before consuming the one-time
            # refresh token so its family is never accidentally revoked.
            token = self.store.load()
            if token.access_expires_at - timestamp > 60:
                return token
            if token.refresh_expires_at <= timestamp:
                raise ObserverAuthError("Observer refresh token has expired; authorization is required again")
            payload = _json_request(
                f"{self.origin}/oauth/token",
                {"grant_type": "refresh_token", "client_id": "observer", "refresh_token": token.refresh_token},
                timeout=30.0,
            )
            refreshed = ObserverToken.from_response(payload, now=timestamp)
            self.store.save(refreshed)
            return refreshed

    def authorization_header(self) -> tuple[str, str]:
        token = self.access_token()
        return "Authorization", f"Bearer {token.access_token}"

    def revoke(self) -> None:
        token = self.store.load()
        _json_request(f"{self.origin}/oauth/revoke", {"token": token.refresh_token}, timeout=30.0)
        self.store.delete()


__all__ = [
    "ObserverAuthError",
    "ObserverAuthorizationClient",
    "ObserverToken",
    "ObserverTokenStore",
    "pkce_pair",
]
