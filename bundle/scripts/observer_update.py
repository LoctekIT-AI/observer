#!/usr/bin/env python3
"""Observer signed self-update.

A small background job (Windows: a scheduled task, macOS: a LaunchAgent) runs
``observer_update.py run`` every few hours. Each run:

1. reads ``update.json`` in the installed bundle (repository, channel, public key);
2. downloads the channel file ``channels/<channel>.json`` from the repository and
   checks its Ed25519 signature against the public key built into the bundle;
3. when the channel names a newer version, downloads that release ZIP, checks its
   size and SHA-256 against the signed channel entry, extracts it outside the
   installed bundle and runs the new version's own installer for the clients
   already installed on this computer, reusing the existing login (never opening a
   login page from the background);
4. writes ``status.json`` next to the bundle; the collectors include it in their
   health records.

Nothing is installed without a valid signature from the release key, and a
version is never replaced by an older or equal one. The private key never ships
in the bundle or the repository; ``tools/sign_channel.py`` signs on the release
machine. With no public key in ``update.json`` the updater does nothing.

Only the Python standard library is used: the managed runtime has no
cryptography package, so Ed25519 verification is implemented here (RFC 8032).
"""
from __future__ import annotations

import sys

# Never write __pycache__ into the installed bundle: Windows could not replace it.
sys.dont_write_bytecode = True

import argparse  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import platform  # noqa: E402
import plistlib  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402
import zipfile  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Callable  # noqa: E402

BUNDLE = Path(__file__).resolve().parents[1]
CHANNEL_SCHEMA = "observer.channel.v1"
MIN_CHECK_INTERVAL = timedelta(minutes=10)  # only spaces logon and timer runs; the channel file is a plain CDN download
RETRY_INTERVAL = timedelta(hours=6)
MAX_ATTEMPTS = 3
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_CHANNEL_BYTES = 64 * 1024
INSTALL_TIMEOUT_SECONDS = 3600  # a background install for four clients can take 10+ minutes
USER_AGENT = "observer-updater"
LAUNCH_AGENT_LABEL = "com.aihub.observer-updater"
UPDATE_INTERVAL_SECONDS = 6 * 3600
HOSTS = ("dsh", "codex", "claude", "workbuddy-desktop")
CHILD_ENV = "OBSERVER_UPDATER_CHILD"
_VERSION = re.compile(r"\d+\.\d+\.\d+")


# --------------------------------------------------------------------------- Ed25519 (RFC 8032)

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _recover_x(y: int, sign: int) -> int | None:
    if y >= _P:
        return None
    xx = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if xx == 0:
        return None if sign else 0
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P:
        x = x * _SQRT_M1 % _P
    if (x * x - xx) % _P:
        return None
    if x & 1 != sign:
        x = _P - x
    return x


_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)
_ZERO = (0, 1, 1, 0)


def _add(a: tuple, b: tuple) -> tuple:
    p1 = (a[1] - a[0]) * (b[1] - b[0]) % _P
    p2 = (a[1] + a[0]) * (b[1] + b[0]) % _P
    c = 2 * a[3] * b[3] * _D % _P
    d = 2 * a[2] * b[2] % _P
    e, f, g, h = p2 - p1, d - c, d + c, p2 + p1
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(scalar: int, point: tuple) -> tuple:
    result = _ZERO
    while scalar:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _same(a: tuple, b: tuple) -> bool:
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _encode(point: tuple) -> bytes:
    z = pow(point[2], _P - 2, _P)
    x, y = point[0] * z % _P, point[1] * z % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decode(data: bytes) -> tuple | None:
    if len(data) != 32:
        return None
    y = int.from_bytes(data, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def _hash_int(data: bytes) -> int:
    return int.from_bytes(hashlib.sha512(data).digest(), "little")


def _secret_scalar(seed: bytes) -> tuple[int, bytes]:
    if len(seed) != 32:
        raise ValueError("Ed25519 seed must be 32 bytes")
    digest = hashlib.sha512(seed).digest()
    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    return scalar, digest[32:]


def ed25519_public_key(seed: bytes) -> bytes:
    scalar, _ = _secret_scalar(seed)
    return _encode(_mul(scalar, _G))


def ed25519_sign(seed: bytes, message: bytes) -> bytes:
    scalar, prefix = _secret_scalar(seed)
    public = _encode(_mul(scalar, _G))
    r = _hash_int(prefix + message) % _L
    encoded_r = _encode(_mul(r, _G))
    k = _hash_int(encoded_r + public + message) % _L
    return encoded_r + ((r + k * scalar) % _L).to_bytes(32, "little")


def ed25519_verify(public: bytes, message: bytes, signature: bytes) -> bool:
    if len(public) != 32 or len(signature) != 64:
        return False
    point_a = _decode(public)
    point_r = _decode(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if point_a is None or point_r is None or s >= _L:
        return False
    k = _hash_int(signature[:32] + public + message) % _L
    return _same(_mul(s, _G), _add(point_r, _mul(k, point_a)))


# --------------------------------------------------------------------------- channel entries


class UpdateError(Exception):
    """A failed update step; ``code`` is a fixed word safe to report."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def version_tuple(value: str) -> tuple[int, int, int]:
    if not _VERSION.fullmatch(value or ""):
        raise UpdateError("bad_version", repr(value))
    major, minor, patch = (int(part) for part in value.split("."))
    return major, minor, patch


def channel_suffix(channel: str) -> str:
    return "-test" if channel == "test" else ""


def channel_message(entry: dict) -> bytes:
    """The exact bytes the release key signs for one channel entry."""
    fields = [CHANNEL_SCHEMA, entry["channel"], entry["version"], entry["tag"], entry["asset"], entry["sha256"], str(entry["size"])]
    return ("\n".join(fields) + "\n").encode("utf-8")


def make_channel_entry(*, channel: str, version: str, archive: bytes, seed: bytes, published_at: str | None = None) -> dict:
    version_tuple(version)
    suffix = channel_suffix(channel)
    entry = {
        "schema": CHANNEL_SCHEMA,
        "channel": channel,
        "version": version,
        "tag": f"v{version}{suffix}",
        "asset": f"observer-v{version}{suffix}.zip",
        "sha256": hashlib.sha256(archive).hexdigest(),
        "size": len(archive),
        "published_at": published_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    entry["signature"] = ed25519_sign(seed, channel_message(entry)).hex()
    return entry


def verify_channel_entry(entry: object, *, channel: str, public_key: bytes) -> dict:
    if not isinstance(entry, dict) or entry.get("schema") != CHANNEL_SCHEMA:
        raise UpdateError("channel_invalid", "unknown channel schema")
    for name in ("channel", "version", "tag", "asset", "sha256", "signature"):
        if not isinstance(entry.get(name), str):
            raise UpdateError("channel_invalid", f"missing {name}")
    if not isinstance(entry.get("size"), int) or isinstance(entry["size"], bool) or not 0 < entry["size"] <= MAX_ARCHIVE_BYTES:
        raise UpdateError("channel_invalid", "size")
    if entry["channel"] != channel:
        raise UpdateError("channel_invalid", "channel does not match this installation")
    version_tuple(entry["version"])
    suffix = channel_suffix(channel)
    if entry["tag"] != f"v{entry['version']}{suffix}" or entry["asset"] != f"observer-v{entry['version']}{suffix}.zip":
        raise UpdateError("channel_invalid", "tag or asset name")
    if not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) or not re.fullmatch(r"[0-9a-f]{128}", entry["signature"]):
        raise UpdateError("channel_invalid", "digest or signature format")
    if not ed25519_verify(public_key, channel_message(entry), bytes.fromhex(entry["signature"])):
        raise UpdateError("signature_invalid")
    return entry


# --------------------------------------------------------------------------- configuration


@dataclass(frozen=True)
class UpdateConfig:
    repo: str
    channel: str
    public_key: bytes | None
    channel_url: str
    download_url: str  # with {tag} and {asset}

    @classmethod
    def load(cls, bundle: Path) -> "UpdateConfig":
        path = bundle / "update.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        repo = str(data.get("repo") or "")
        channel = str(data.get("channel") or "release")
        if repo and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise UpdateError("config_invalid", "repo")
        if not re.fullmatch(r"[a-z0-9-]+", channel):
            raise UpdateError("config_invalid", "channel")
        key_hex = str(data.get("public_key") or "")
        public_key = bytes.fromhex(key_hex) if re.fullmatch(r"[0-9a-f]{64}", key_hex) else None
        if key_hex and public_key is None:
            raise UpdateError("config_invalid", "public_key")
        channel_url = str(data.get("channel_url") or (f"https://raw.githubusercontent.com/{repo}/main/channels/{channel}.json" if repo else ""))
        download_url = str(data.get("download_url") or (f"https://github.com/{repo}/releases/download/{{tag}}/{{asset}}" if repo else ""))
        return cls(repo, channel, public_key, channel_url, download_url)

    @property
    def enabled(self) -> bool:
        return bool(self.public_key and self.channel_url and self.download_url)


def bundle_version(bundle: Path) -> str:
    return str(json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))["bundle_version"])


def installed_hosts(bundle: Path) -> list[str]:
    path = bundle / "installed-hosts.json"
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    hosts = data.get("hosts") if isinstance(data, dict) else None
    return [host for host in hosts if host in HOSTS] if isinstance(hosts, list) else []


def default_config_file(system: str) -> Path:
    if system == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "AIHub/observer/customer.env"
    return Path.home() / "Library/Application Support/AIHub/observer/customer.env"


def has_login(config_file: Path) -> bool:
    try:
        lines = config_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    return any(line.startswith("OBSERVER_AUTH_TOKEN_REF=") and line.split("=", 1)[1].strip() for line in lines)


# --------------------------------------------------------------------------- network


def fetch(url: str, *, max_bytes: int, timeout: int = 120) -> bytes:
    """HTTPS GET with a size limit, using the system trust store."""
    if not url.startswith("https://"):
        raise UpdateError("download_failed", "only https URLs are used")
    if platform.system() == "Darwin":
        # The managed Python on macOS does not read the system keychain; curl does.
        return _curl(Path("/usr/bin/curl"), url, max_bytes=max_bytes, timeout=timeout)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(max_bytes + 1)
    except (urllib.error.URLError, OSError) as exc:
        curl = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe"
        if platform.system() == "Windows" and curl.is_file():
            return _curl(curl, url, max_bytes=max_bytes, timeout=timeout)
        raise UpdateError("download_failed", type(exc).__name__) from exc
    if len(data) > max_bytes:
        raise UpdateError("download_failed", "too large")
    return data


def _curl(curl: Path, url: str, *, max_bytes: int, timeout: int) -> bytes:
    command = [str(curl), "--fail", "--silent", "--show-error", "--location", "--proto", "=https", "--proto-redir", "=https",
        "--max-time", str(timeout), "--max-filesize", str(max_bytes), "--user-agent", USER_AGENT, url]
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout + 30, creationflags=_no_window())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError("download_failed", type(exc).__name__) from exc
    if result.returncode or len(result.stdout) > max_bytes:
        raise UpdateError("download_failed", f"curl exit {result.returncode}")
    return result.stdout


def _no_window() -> int:
    return 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW


def _installer_flags() -> int:
    # NORMAL_PRIORITY_CLASS: scheduled tasks start below normal priority, which slows the install a lot.
    return _no_window() | (0x00000020 if os.name == "nt" else 0)


# --------------------------------------------------------------------------- updater


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def _parse(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def safe_extract(archive: Path, destination: Path) -> None:
    """Extract only plain relative entries; anything else rejects the archive."""
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            name = info.filename
            parts = name.split("/")
            if not name or name.startswith("/") or "\\" in name or ":" in name or any(part in {"", ".", ".."} for part in parts[:-1]) or parts[-1] in {".", ".."}:
                raise UpdateError("archive_invalid", "unsafe entry name")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise UpdateError("archive_invalid", "symbolic link entry")
        bundle.extractall(destination)


class Updater:
    def __init__(self, *, bundle: Path = BUNDLE, state_dir: Path | None = None, config_file: Path | None = None,
                 system: str | None = None, fetcher: Callable[..., bytes] = fetch,
                 runner: Callable[..., int] | None = None, now: Callable[[], datetime] = _now) -> None:
        self.bundle = bundle
        self.state_dir = state_dir or bundle.parent / "updater"
        self.system = system or platform.system()
        self.config_file = config_file or default_config_file(self.system)
        self.fetch = fetcher
        self.runner = runner or self._run_installer
        self.now = now
        self.status_path = self.state_dir / "status.json"

    # ---- state
    def _state(self) -> dict:
        try:
            data = json.loads(self.status_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _finish(self, state: dict, status: str, **fields: object) -> dict:
        state.update(fields)
        state["status"] = status
        state["checked_at"] = _iso(self.now())
        try:
            state["current_version"] = bundle_version(self.bundle)
        except (OSError, ValueError, KeyError):
            state["current_version"] = None
        if not getattr(self, "_dry_run", False):  # "check" only reports; it never overwrites a running update's status
            _atomic_json(self.status_path, state)
        return state

    def _lock(self) -> Path | None:
        lock = self.state_dir / "updater.lock"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            age = self.now().timestamp() - lock.stat().st_mtime
            if age > INSTALL_TIMEOUT_SECONDS + 600:
                lock.unlink()  # left behind by a crashed run
        except FileNotFoundError:
            pass
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return None
        os.write(descriptor, str(os.getpid()).encode())
        os.close(descriptor)
        return lock

    # ---- main
    def run(self, *, force: bool = False, dry_run: bool = False) -> dict:
        self._dry_run = dry_run
        state = self._state()
        try:
            config = UpdateConfig.load(self.bundle)
        except (UpdateError, ValueError, OSError) as exc:
            return self._finish(state, "config_invalid", last_error=getattr(exc, "code", type(exc).__name__))
        if not config.enabled:
            return self._finish(state, "disabled")
        last = _parse(state.get("checked_at"))
        if not force and not dry_run and last is not None and self.now() - last < MIN_CHECK_INTERVAL and state.get("status") not in {"updating"}:
            return state
        lock = None if dry_run else self._lock()
        if lock is None and not dry_run:
            return {**state, "status": "busy"}
        try:
            return self._run(config, state, force=force, dry_run=dry_run)
        except UpdateError as exc:
            return self._finish(state, exc.code, last_error=exc.code)
        except Exception as exc:  # the updater must never take anything else down
            return self._finish(state, "internal_error", last_error=type(exc).__name__)
        finally:
            if lock is not None:
                lock.unlink(missing_ok=True)

    def _run(self, config: UpdateConfig, state: dict, *, force: bool, dry_run: bool) -> dict:
        current = bundle_version(self.bundle)
        try:
            raw = self.fetch(config.channel_url, max_bytes=MAX_CHANNEL_BYTES)
            entry = json.loads(raw.decode("utf-8"))
        except UpdateError as exc:
            raise UpdateError("channel_unavailable", str(exc)) from exc
        except ValueError as exc:
            raise UpdateError("channel_invalid", "not JSON") from exc
        assert config.public_key is not None
        entry = verify_channel_entry(entry, channel=config.channel, public_key=config.public_key)
        available = entry["version"]
        # An install that failed part-way can already have switched the shared bundle to the new
        # version while some clients still run the old setup: such a version is retried, not "up to date".
        incomplete = state.get("incomplete_version") == available
        if version_tuple(available) < version_tuple(current) or (available == current and not incomplete):
            return self._finish(state, "up_to_date", available_version=available, last_error=None)
        if dry_run:
            return {**state, "status": "update_available", "available_version": available, "current_version": current}
        attempts = state.get("attempts") if isinstance(state.get("attempts"), dict) else {}
        attempts = {key: value for key, value in attempts.items() if key == available}
        tried = int(attempts.get(available, 0))
        last_attempt = _parse(state.get("last_attempt_at"))
        if not force and tried >= MAX_ATTEMPTS:
            return self._finish(state, "gave_up", available_version=available, attempts=attempts)
        if not force and tried and last_attempt is not None and self.now() - last_attempt < RETRY_INTERVAL:
            return self._finish(state, "waiting_retry", available_version=available, attempts=attempts)
        hosts = installed_hosts(self.bundle)
        if not hosts:
            return self._finish(state, "no_hosts", available_version=available)
        if not has_login(self.config_file):
            # A background job must never open a login page.
            return self._finish(state, "login_missing", available_version=available)

        attempts[available] = tried + 1
        state.update(attempts=attempts, last_attempt_at=_iso(self.now()), available_version=available, incomplete_version=available)
        self._finish(state, "updating")
        archive_bytes = self.fetch(config.download_url.format(tag=entry["tag"], asset=entry["asset"]), max_bytes=MAX_ARCHIVE_BYTES, timeout=600)
        if len(archive_bytes) != entry["size"] or hashlib.sha256(archive_bytes).hexdigest() != entry["sha256"]:
            raise UpdateError("archive_mismatch")
        work = self.state_dir / "work"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        archive = work / entry["asset"]
        archive.write_bytes(archive_bytes)
        source = work / "source"
        safe_extract(archive, source)
        if bundle_version(source) != available:
            raise UpdateError("archive_mismatch", "manifest version")
        detected = self.detect_hosts(source)
        if detected is not None:
            # A client that was removed or moved must not block the update of the others
            # (Windows: install_observer_windows_all.ps1 -Unattended skips it the same way).
            skipped = [host for host in hosts if host not in detected]
            hosts = [host for host in hosts if host in detected]
            state["skipped_hosts"] = skipped
            if not hosts:
                raise UpdateError("no_hosts", "no installed client was detected")
        log = self.state_dir / "install.log"
        code = self.runner(source=source, archive=archive, hosts=hosts, log=log)
        if code != 0:
            raise UpdateError("install_failed", f"exit {code}")
        if bundle_version(self.bundle) != available:
            raise UpdateError("install_failed", "installed version did not change")
        shutil.rmtree(work, ignore_errors=True)
        state.pop("incomplete_version", None)
        return self._finish(state, "updated", updated_to=available, updated_at=_iso(self.now()), attempts={}, last_error=None)

    # ---- installer
    def detect_hosts(self, source: Path) -> set[str] | None:
        """macOS: the clients the new version's detection finds (None = not checked)."""
        if self.system != "Darwin":
            return None
        script = source / "scripts" / "observer_detect_clients_macos.sh"
        try:
            result = subprocess.run(["/bin/bash", str(script)], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UpdateError("detect_failed", type(exc).__name__) from exc
        lines = [line for line in result.stdout.splitlines() if line.startswith("HOSTS=")]
        if result.returncode or not lines:
            raise UpdateError("detect_failed", f"exit {result.returncode}")
        return set(lines[-1][len("HOSTS="):].split())

    def installer_command(self, *, source: Path, archive: Path, hosts: list[str]) -> list[str]:
        if self.system == "Windows":
            powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            return [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                str(source / "scripts" / "install_observer_windows_all.ps1"), "-Archive", str(archive),
                "-Hosts", ",".join(hosts), "-Unattended"]
        if self.system == "Darwin":
            command = ["/bin/bash", str(source / "scripts" / "bootstrap_observer_macos.sh"), "--archive", str(archive)]
            for host in hosts:
                command += ["--host", host]
            return command
        raise UpdateError("unsupported_platform")

    def _run_installer(self, *, source: Path, archive: Path, hosts: list[str], log: Path) -> int:
        command = self.installer_command(source=source, archive=archive, hosts=hosts)
        environment = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
        environment[CHILD_ENV] = "1"
        with log.open("wb") as output:
            try:
                result = subprocess.run(command, cwd=str(self.state_dir), stdin=subprocess.DEVNULL, stdout=output,
                    stderr=subprocess.STDOUT, env=environment, timeout=INSTALL_TIMEOUT_SECONDS,
                    creationflags=_installer_flags(), start_new_session=os.name != "nt")
            except subprocess.TimeoutExpired:
                return 124
        return result.returncode


# --------------------------------------------------------------------------- macOS LaunchAgent


def launch_agent_plist(*, python: Path, script: Path, state_dir: Path) -> bytes:
    return plistlib.dumps({
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [str(python), str(script), "run"],
        "WorkingDirectory": str(state_dir),
        "StartInterval": UPDATE_INTERVAL_SECONDS,
        "RunAtLoad": True,
        "ProcessType": "Background",
        # The installer runs in its own session and must outlive a reload of this job.
        "AbandonProcessGroup": True,
        "StandardOutPath": str(state_dir / "launchd.log"),
        "StandardErrorPath": str(state_dir / "launchd.log"),
    }, sort_keys=True)


def register_macos(*, python: Path, bundle: Path, agents_dir: Path | None = None, launchctl: str = "/bin/launchctl") -> str:
    state_dir = bundle.parent / "updater"
    state_dir.mkdir(parents=True, exist_ok=True)
    agents_dir = agents_dir or Path.home() / "Library/LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    plist = agents_dir / f"{LAUNCH_AGENT_LABEL}.plist"
    content = launch_agent_plist(python=python, script=bundle / "scripts" / "observer_update.py", state_dir=state_dir)
    changed = not plist.is_file() or plist.read_bytes() != content
    if changed:
        temporary = plist.with_name(plist.name + ".tmp")
        temporary.write_bytes(content)
        os.replace(temporary, plist)
    domain = f"gui/{os.getuid()}"
    loaded = subprocess.run([launchctl, "print", f"{domain}/{LAUNCH_AGENT_LABEL}"], capture_output=True).returncode == 0
    if loaded and not changed:
        return "unchanged"
    if loaded and os.environ.get(CHILD_ENV):
        # Reloading would stop the update that is running this installer;
        # launchd picks up the new definition at the next login.
        return "written_reload_at_login"
    if loaded:
        subprocess.run([launchctl, "bootout", f"{domain}/{LAUNCH_AGENT_LABEL}"], capture_output=True)
    result = subprocess.run([launchctl, "bootstrap", domain, str(plist)], capture_output=True, text=True)
    if result.returncode:
        raise UpdateError("register_failed", result.stderr.strip()[:200])
    return "registered"


def unregister_macos(*, agents_dir: Path | None = None, launchctl: str = "/bin/launchctl") -> str:
    plist = (agents_dir or Path.home() / "Library/LaunchAgents") / f"{LAUNCH_AGENT_LABEL}.plist"
    subprocess.run([launchctl, "bootout", f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}"], capture_output=True)
    plist.unlink(missing_ok=True)
    return "removed"


# --------------------------------------------------------------------------- command line


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Observer signed self-update")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="check the channel and install a newer signed version")
    run.add_argument("--force", action="store_true", help="ignore the check interval and retry limits")
    sub.add_parser("check", help="only report whether a newer signed version is available")
    sub.add_parser("status", help="print the last updater status")
    register = sub.add_parser("register", help="macOS: install the updater LaunchAgent")
    register.add_argument("--python", type=Path, required=True)
    register.add_argument("--bundle", type=Path, default=BUNDLE)
    sub.add_parser("unregister", help="macOS: remove the updater LaunchAgent")
    args = parser.parse_args(argv)
    if args.command == "register":
        print(json.dumps({"status": register_macos(python=args.python.absolute(), bundle=args.bundle.resolve())}))
        return 0
    if args.command == "unregister":
        print(json.dumps({"status": unregister_macos()}))
        return 0
    updater = Updater()
    if args.command == "status":
        print(json.dumps(updater._state(), indent=2, sort_keys=True))
        return 0
    result = updater.run(force=getattr(args, "force", False), dry_run=args.command == "check")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
