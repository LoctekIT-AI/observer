"""Native macOS Keychain and current-user Windows DPAPI boundary for customer telemetry config."""

from __future__ import annotations

import hmac
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
import platform
import shlex
import shutil
import subprocess


class KeychainError(RuntimeError):
    """The requested protected secret operation could not be completed."""


def _security() -> str:
    if platform.system() != "Darwin":
        raise KeychainError("macOS Keychain is unavailable on this platform")
    executable = shutil.which("security")
    if executable is None:
        raise KeychainError("macOS security command is unavailable")
    return executable


def _validate_identity(service: str, account: str) -> None:
    if not service.strip() or not account.strip():
        raise KeychainError("Keychain service and account are required")
    if any(character in service + account for character in "\r\n\x00"):
        raise KeychainError("Keychain identity must be single-line")


def store_secret(*, service: str, account: str, secret: str) -> None:
    _validate_identity(service, account)
    if platform.system() == "Windows":
        _store_dpapi(service, account, secret)
        return
    if not secret or "\x00" in secret:
        raise KeychainError("Keychain secret must be non-empty text without NUL")
    # security 的交互模式从 stdin 接收十六进制数据，避免密钥出现在进程 argv。
    command = " ".join(shlex.quote(value) for value in (
        "add-generic-password", "-U", "-s", service, "-a", account, "-X", secret.encode("utf-8").hex(),
    )) + "\n"
    # 上游 MAX_LINE_LEN=4096；禁止被原生命令截断后造成部分写入。
    if len(command.encode("utf-8")) >= 4096:
        raise KeychainError("Keychain stdin command exceeds native line limit")
    result = subprocess.run([_security(), "-i"], input=command, capture_output=True, text=True, check=False)
    if result.returncode != 0 or result.stderr.strip():
        raise KeychainError(f"Keychain secret could not be stored (security exit {result.returncode})")
    # security -i 的退出码不足以证明内部命令成功，必须真实读取并验证结果。
    protected = read_secret(service=service, account=account)
    if not hmac.compare_digest(protected.encode("utf-8"), secret.encode("utf-8")):
        raise KeychainError("Keychain protected readback did not match")


def read_secret(*, service: str, account: str) -> str:
    _validate_identity(service, account)
    if platform.system() == "Windows":
        return _read_dpapi(service, account)
    result = subprocess.run(
        [_security(), "find-generic-password", "-w", "-s", service, "-a", account],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0 or not result.stdout:
        raise KeychainError(f"Keychain secret is unavailable (security exit {result.returncode})")
    return result.stdout.removesuffix("\n")


def delete_secret(*, service: str, account: str) -> None:
    """Remove only the explicitly identified temporary/staged protected item."""
    _validate_identity(service, account)
    if platform.system() == "Windows":
        _dpapi_path(service, account).unlink()
        return
    result = subprocess.run([_security(), "delete-generic-password", "-s", service, "-a", account], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise KeychainError(f"Keychain item cleanup failed (security exit {result.returncode})")


__all__ = ["KeychainError", "delete_secret", "read_secret", "store_secret", "parse_secret_reference", "secret_reference"]


_DPAPI_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_WINDOWS_DEVICES = {"con", "prn", "aux", "nul", *[f"com{i}" for i in range(1, 10)], *[f"lpt{i}" for i in range(1, 10)]}


def validate_dpapi_identity(service: str, account: str) -> None:
    for value in (service, account):
        if not _DPAPI_IDENTIFIER.fullmatch(value) or value.endswith(".") or value.split(".", 1)[0] in _WINDOWS_DEVICES:
            raise KeychainError("DPAPI identity must be a safe lowercase identifier")


def secret_reference(service: str, account: str) -> str:
    _validate_identity(service, account)
    system = platform.system()
    if system == "Windows":
        validate_dpapi_identity(service, account)
        return f"dpapi://{service}/{account}"
    if system == "Darwin":
        return f"keychain://{service}/{account}"
    raise KeychainError("protected secret storage is unavailable on this platform")


def parse_secret_reference(reference: str) -> tuple[str, str, str]:
    if reference != reference.strip() or any(c in reference for c in "\r\n\x00"):
        raise KeychainError("protected secret reference must be a single-line URI")
    parsed = urlsplit(reference)
    if parsed.scheme not in {"dpapi", "keychain"} or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise KeychainError("unsupported protected secret reference")
    service, account = parsed.netloc, parsed.path.removeprefix("/")
    if "/" in account or not parsed.path.startswith("/"):
        raise KeychainError("protected secret reference must identify one account")
    _validate_identity(service, account)
    if parsed.scheme == "dpapi":
        validate_dpapi_identity(service, account)
    return parsed.scheme, service, account


def _dpapi_path(service: str, account: str) -> Path:
    validate_dpapi_identity(service, account)
    local = os.environ.get("LOCALAPPDATA", "")
    if platform.system() != "Windows" or not local or not Path(local).is_absolute():
        raise KeychainError("current-user LOCALAPPDATA is unavailable for DPAPI storage")
    base = Path(local).resolve(strict=True)
    path = base / "AIHub" / "observer" / "secrets" / service / (account + ".dpapi")
    # 禁止符号链接、junction 重定向到当前用户 Observer 目录之外。
    for part in (path, *path.parents):
        if part == base:
            break
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise KeychainError("DPAPI storage must not contain links or junctions")
    if not path.resolve().is_relative_to(base / "AIHub" / "observer" / "secrets"):
        raise KeychainError("DPAPI path escaped current-user Observer state")
    return path


def _dpapi(data: bytes, *, service: str, account: str, protect: bool) -> bytes:
    if platform.system() != "Windows":
        raise KeychainError("Windows DPAPI is unavailable on this platform")
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    native = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    native.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    native.restype = wintypes.BOOL
    payload = ctypes.create_string_buffer(data)
    identity = ctypes.create_string_buffer(f"dpapi://{service}/{account}".encode("utf-8"))
    source = Blob(len(data), ctypes.cast(payload, ctypes.POINTER(ctypes.c_ubyte)))
    entropy = Blob(len(identity.raw) - 1, ctypes.cast(identity, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    # CurrentUser 默认范围；仅 UI_FORBIDDEN=1，不使用 LOCAL_MACHINE。
    if not native(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(output)):
        error = ctypes.get_last_error()
        raise KeychainError(f"DPAPI {'protect' if protect else 'unprotect'} failed (WinError {error})") from ctypes.WinError(error)
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        if kernel.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p)):
            error = ctypes.get_last_error()
            raise KeychainError(f"DPAPI LocalFree failed (WinError {error})") from ctypes.WinError(error)


def _store_dpapi(service: str, account: str, secret: str) -> None:
    if not secret or "\x00" in secret:
        raise KeychainError("DPAPI secret must be non-empty text without NUL")
    path = _dpapi_path(service, account)
    encrypted = _dpapi(secret.encode("utf-8"), service=service, account=account, protect=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".secret-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encrypted)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    if not hmac.compare_digest(_read_dpapi(service, account).encode("utf-8"), secret.encode("utf-8")):
        raise KeychainError("DPAPI protected readback did not match")


def _read_dpapi(service: str, account: str) -> str:
    path = _dpapi_path(service, account)
    return _dpapi(path.read_bytes(), service=service, account=account, protect=False).decode("utf-8")
