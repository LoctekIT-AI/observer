#!/usr/bin/env python3
"""Authorize one Observer installation through the existing site login."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if not getattr(sys, "frozen", False):
    for import_root in (SCRIPT_ROOT / "packages/contracts/src", SCRIPT_ROOT / "apps/cowork-runtime/src"):
        sys.path.insert(0, str(import_root))

from cowork_runtime.telemetry.identity import validate_pseudonym
from cowork_runtime.telemetry.keychain import KeychainError, parse_secret_reference, read_secret, secret_reference, store_secret
from cowork_runtime.telemetry.observer_auth import ObserverAuthError, ObserverAuthorizationClient, ObserverTokenStore, _refresh_lock


DEFAULT_CONFIG = (Path(os.environ["LOCALAPPDATA"]) / "AIHub/observer/customer.env") if os.name == "nt" else Path.home() / "Library/Application Support/AIHub/observer/customer.env"
DEFAULT_ORIGIN = "https://itsub.loctek.com/observer"
SERVICE = "com.aihub.skill-mcp-observer"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--client-type", choices=("dsh", "codex", "claude", "workbuddy-desktop"), required=True)
    parser.add_argument("--config-file", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--auth-origin", default=DEFAULT_ORIGIN)
    parser.add_argument("--installation-id")
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument("--no-browser", action="store_true", help="print the authorization URL for a separately controlled browser")
    parser.add_argument("--replace", action="store_true", help="replace an existing Observer authorization and rotate its protected token")
    return parser


def _read_config(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    if path.is_symlink():
        raise ValueError("configuration destination must not be a symbolic link")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError("existing Observer configuration could not be read") from exc
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key.startswith("OBSERVER_") or key.startswith("AIHUB_"):
            values[key] = value.strip()
    return values


def _pseudonyms(values: dict[str, str]) -> tuple[str, str]:
    tenant = values.get("AIHUB_TENANT_PSEUDONYM") or f"tenant_{secrets.token_urlsafe(18)}"
    user = values.get("AIHUB_USER_PSEUDONYM") or f"user_{secrets.token_urlsafe(18)}"
    return validate_pseudonym(tenant, prefix="tenant"), validate_pseudonym(user, prefix="user")


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, raw_path = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(raw_path)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _render_config(*, values: dict[str, str], origin: str, client_type: str, installation_id: str, token_ref: str) -> str:
    tenant, user = _pseudonyms(values)
    rendered = {
        "AIHUB_TELEMETRY_CONSENT": "granted",
        "AIHUB_TENANT_PSEUDONYM": tenant,
        "AIHUB_USER_PSEUDONYM": user,
        "OBSERVER_AUTH_ORIGIN": origin.rstrip("/"),
        "OBSERVER_AUTH_TOKEN_REF": token_ref,
        "OBSERVER_CLIENT_TYPE": client_type,
        "OBSERVER_INSTALLATION_ID": installation_id,
    }
    if any(not value or any(c in value for c in "\r\n\x00") for value in rendered.values()):
        raise ValueError("Observer configuration values must be non-empty single-line values")
    return "\n".join(f"{key}={value}" for key, value in rendered.items()) + "\n"


def _authorize_or_reuse(args: argparse.Namespace, config_file: Path) -> int:
    values = _read_config(config_file)
    has_existing_auth = bool(values.get("OBSERVER_AUTH_TOKEN_REF"))
    account = "observer-token-" + hashlib.sha256(str(config_file).encode("utf-8")).hexdigest()[:32]
    # An existing login is reused only for the same authorization site. Moving a computer
    # from the test environment to production (another origin) always logs in again, so
    # uploads never keep going to the old site with its old token.
    same_origin = values.get("OBSERVER_AUTH_ORIGIN", "").rstrip("/") == str(args.auth_origin).rstrip("/")
    if config_file.exists() and has_existing_auth and same_origin and not args.replace:
        scheme, service, protected_account = parse_secret_reference(values["OBSERVER_AUTH_TOKEN_REF"])
        expected_scheme = "dpapi" if os.name == "nt" else "keychain"
        if scheme != expected_scheme or service != SERVICE or protected_account != account:
            raise ValueError("existing Observer token reference does not match this configuration")
        client_type = values.get("OBSERVER_CLIENT_TYPE", "")
        installation_id = values.get("OBSERVER_INSTALLATION_ID", "")
        if not client_type or not installation_id:
            raise ValueError("existing Observer authorization is missing its client identity")
        token = ObserverAuthorizationClient(
            origin=values.get("OBSERVER_AUTH_ORIGIN", args.auth_origin),
            client_type=client_type,
            installation_id=installation_id,
            store=ObserverTokenStore(SERVICE, account),
            timeout_seconds=args.timeout_seconds,
            refresh_lock_path=config_file.with_name(".observer-token-refresh.lock"),
        ).access_token()
        print(json.dumps({"status": "ok", "authorization": "reused", "client_type": client_type, "installation_id": installation_id, "config_file": str(config_file), "telemetry_endpoint": token.telemetry_endpoint}, ensure_ascii=False, sort_keys=True))
        return 0
    installation_id = args.installation_id or values.get("OBSERVER_INSTALLATION_ID") or f"observer-{args.client_type}-{secrets.token_hex(12)}"
    token_store = ObserverTokenStore(SERVICE, account)
    previous_secret: str | None = None
    previous_ref = values.get("OBSERVER_AUTH_TOKEN_REF", "")
    if previous_ref:
        try:
            scheme, previous_service, previous_account = parse_secret_reference(previous_ref)
            expected_scheme = "dpapi" if os.name == "nt" else "keychain"
            if scheme != expected_scheme or previous_service != SERVICE or previous_account != account:
                raise ValueError("existing Observer token reference does not match this configuration")
            previous_secret = read_secret(service=previous_service, account=previous_account)
        except KeychainError as exc:
            raise ValueError("existing Observer authorization could not be read for replacement") from exc
    client = ObserverAuthorizationClient(
        origin=args.auth_origin,
        client_type=args.client_type,
        installation_id=installation_id,
        store=token_store,
        timeout_seconds=args.timeout_seconds,
        open_browser=not args.no_browser,
    )
    token = client.authorize()
    try:
        token_ref = secret_reference(SERVICE, account)
        _atomic_write(config_file, _render_config(values=values, origin=args.auth_origin, client_type=args.client_type, installation_id=installation_id, token_ref=token_ref))
    except BaseException:
        if previous_secret is not None:
            store_secret(service=SERVICE, account=account, secret=previous_secret)
        else:
            token_store.delete()
        raise
    print(json.dumps({"status": "ok", "client_type": args.client_type, "installation_id": installation_id, "config_file": str(config_file), "telemetry_endpoint": token.telemetry_endpoint}, ensure_ascii=False, sort_keys=True))
    return 0

def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config_file = args.config_file.expanduser().absolute()
        # All hosts share one customer.env. Serialize the complete check,
        # browser flow, protected-token write, and config write so concurrent
        # installers cannot each open a separate authorization page.
        with _refresh_lock(config_file.with_name(".observer-authorization.lock")):
            return _authorize_or_reuse(args, config_file)
    except (KeychainError, ObserverAuthError, OSError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "error_type": type(exc).__name__, "reason": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
