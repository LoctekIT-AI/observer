#!/usr/bin/env python3
"""Install the Observer plugin using native DSH per-profile bundle reconciliation."""
from __future__ import annotations

import argparse
import hashlib
import gzip
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

PACKAGE_NAME = "@observer/dsh-observer"
# DeepSeek Harness desktop builds whose bundled DSH runtime was verified with this
# plugin (tools/execute, tools/result and session/event seams present).
HARNESS_DESKTOP_VERSIONS = {"0.2.0-rc.2"}


def is_harness_launcher(dsh_cli: Path) -> bool:
    """DeepSeek Harness builds (including the company's AI Hub) ship DSH inside app.asar
    behind runtime/cli/bin/dsh.cmd (Windows) or runtime/cli/bin/dsh (macOS)."""
    return dsh_cli.name.lower() in {"dsh.cmd", "dsh"} and dsh_cli.parent.name == "bin" and dsh_cli.parents[1].name == "cli"


# macOS defaults. AI Hub (the company build of DeepSeek Harness) keeps its DSH home
# under Electron userData and uses the reserved "desktop" profile; the older DSH
# Desktop layout is the fallback.
AIHUB_APP = Path("/Applications/AI Hub.app")
LEGACY_DSH_APP = Path("/Applications/DSH Desktop.app")


def macos_default_dsh(home: Path | None = None, aihub_app: Path = AIHUB_APP) -> dict[str, Path]:
    home = home or Path.home()
    launcher = aihub_app / "Contents/Resources/runtime/cli/bin/dsh"
    profile = home / "Library/Application Support/AI Hub/ah-home/profiles/desktop"
    if launcher.is_file() and (profile / "package.json").is_file():
        return {"app": aihub_app, "cli": launcher, "profile": profile}
    return {"app": LEGACY_DSH_APP, "cli": LEGACY_DSH_APP / "Contents/Resources/app/node_modules/@deepseek-ai/dsh/lib/bin.js",
        "profile": home / "Library/Application Support/dsh-desktop/harness/profiles/web"}


def dsh_command(dsh_cli: Path, node: Path | None, *arguments: str) -> list[str]:
    """Command line for the native DSH CLI: the Harness launcher runs DSH with the app's own Node."""
    if is_harness_launcher(dsh_cli):
        return [str(dsh_cli), *arguments]
    if node is None:
        raise FileNotFoundError("Node executable is unavailable")
    return [str(node), str(dsh_cli), *arguments]


def verify_native_runtime(dsh_cli: Path) -> None:
    if is_harness_launcher(dsh_cli):
        runtime = json.loads((dsh_cli.parents[2] / "primary-runtime" / "runtime.json").read_text(encoding="utf-8"))
        if runtime.get("desktopVersion") not in HARNESS_DESKTOP_VERSIONS:
            raise ValueError(f"unverified DeepSeek Harness version: {runtime.get('desktopVersion')}")
        return
    for package, expected in (("dsh-tools", "0.1.5-rc.2"), ("dsh-session", "0.1.5-rc.2"), ("cordis", "4.0.2")):
        native = json.loads((dsh_cli.parents[2] / package / "package.json").read_text())
        if native.get("version") != expected:
            raise ValueError(f"unverified native {package} version: {native.get('version')}")
PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "packages/dsh-observer"


def _already_active(profile_dir: Path, specifier: str, contents: dict[str, bytes]) -> bool:
    """True when this exact Observer package is the active profile dependency."""
    try:
        current = json.loads((profile_dir / "package.json").read_text())
    except (OSError, ValueError):
        return False
    if current.get("dependencies", {}).get(PACKAGE_NAME) != specifier:
        return False
    if PACKAGE_NAME not in current.get("dsh", {}).get("profile", {}).get("bundles", []):
        return False
    installed = profile_dir / "node_modules" / PACKAGE_NAME
    try:
        return all((installed / name).read_bytes() == content for name, content in contents.items())
    except OSError:
        return False


def install(*, profile_dir: Path, dsh_cli: Path, node: Path | None, source_root: Path, package_root: Path = PACKAGE_ROOT) -> dict[str, object]:
    profile_dir = profile_dir.expanduser().resolve()
    if profile_dir.parent.name != "profiles" or not (profile_dir / "package.json").is_file():
        raise ValueError("--profile-dir must be an existing DSH_HOME/profiles/NAME profile")
    source_root = source_root.expanduser().resolve()
    dsh_cli = dsh_cli.expanduser().resolve(strict=True)
    node = node.expanduser().resolve(strict=True) if node is not None and not is_harness_launcher(dsh_cli) else None
    verify_native_runtime(dsh_cli)
    manifest = json.loads((package_root / "package.json").read_text())
    if manifest.get("name") != PACKAGE_NAME or manifest.get("dsh", {}).get("bundle", {}).get("patch") != "./cordis.patch.yml":
        raise ValueError("Observer package has invalid native bundle metadata")
    installation_id = "install-dsh-" + hashlib.sha256(str(profile_dir).encode()).hexdigest()[:24]
    config = {"eventFile": str(source_root / "component-events.jsonl"), "installationId": installation_id}
    patch = [{"insert": [{"id": "dsh-observer", "name": PACKAGE_NAME, "config": config}]}]
    cache = profile_dir / "observer-packages"
    cache.mkdir(mode=0o700, exist_ok=True)
    contents = {
        name: (json.dumps(patch, sort_keys=True) + "\n").encode() if name == "cordis.patch.yml" else (package_root / name).read_bytes()
        for name in ("package.json", "index.mjs", "README.md", "cordis.patch.yml")
    }
    content_hash = hashlib.sha256(b"".join(name.encode() + content for name, content in sorted(contents.items()))).hexdigest()[:16]
    archive = cache / f"dsh-observer-{manifest['version']}-{content_hash}.tgz"
    # 内容变化对应新的 file: 依赖路径，防止 pnpm 复用同版本旧配置缓存。
    with archive.open("wb") as output, gzip.GzipFile(filename="", fileobj=output, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as bundle:
            for name, content in contents.items():
                member = tarfile.TarInfo("package/" + name)
                member.size = len(content)
                member.mode = 0o644
                bundle.addfile(member, io.BytesIO(content))
    source_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    result_base = {"status": "ok", "enabled": True, "profile_dir": str(profile_dir), "installation_id": installation_id, "event_file": config["eventFile"], "package": PACKAGE_NAME, "package_archive": str(archive), "desktop_acceptance": "unverified"}
    specifier = "file:" + archive.as_posix()
    if _already_active(profile_dir, specifier, contents):
        # This exact package is already installed and active.  Re-running
        # DSH's plugin command would re-reconcile the whole profile with pnpm,
        # which can fail for reasons unrelated to Observer.
        return {**result_base, "reused_existing_install": True}
    env = {**os.environ, "DSH_HOME": str(profile_dir.parent.parent)}
    result = subprocess.run(dsh_command(dsh_cli, node, "plugin", "--profile", profile_dir.name, "add", specifier), env=env, capture_output=True, text=True, check=False, timeout=180)
    if result.returncode:
        raise RuntimeError(f"native DSH plugin installation failed ({result.returncode}): {(result.stderr or result.stdout).strip()}")
    current = json.loads((profile_dir / "package.json").read_text())
    if PACKAGE_NAME not in current.get("dependencies", {}) or PACKAGE_NAME not in current.get("dsh", {}).get("profile", {}).get("bundles", []):
        raise RuntimeError("native DSH did not install and activate the Observer bundle")
    return result_base


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("install", "uninstall"), default="install")
    parser.add_argument("--profile-dir", type=Path, required=True)
    parser.add_argument("--dsh-cli", type=Path, required=True, help="installed native dsh/lib/bin.js")
    parser.add_argument("--node", type=Path, default=shutil.which("node"))
    parser.add_argument("--source-root", type=Path, default=Path.home() / ".aihub/observer/dsh")
    args = parser.parse_args(argv)
    try:
        if args.node is None and not is_harness_launcher(args.dsh_cli):
            raise FileNotFoundError("Node executable is unavailable")
        if args.command == "uninstall":
            profile = args.profile_dir.expanduser().resolve()
            current = json.loads((profile / "package.json").read_text()) if (profile / "package.json").is_file() else {}
            if PACKAGE_NAME in current.get("dependencies", {}):
                result = subprocess.run(dsh_command(args.dsh_cli, args.node, "plugin", "--profile", profile.name, "remove", PACKAGE_NAME), env={**os.environ, "DSH_HOME": str(profile.parent.parent)}, capture_output=True, text=True, timeout=180)
                if result.returncode:
                    raise RuntimeError(f"native DSH plugin removal failed ({result.returncode}): {(result.stderr or result.stdout).strip()}")
            payload = {"status": "ok", "enabled": False, "state_preserved": True}
        else:
            payload = install(profile_dir=args.profile_dir, dsh_cli=args.dsh_cli, node=args.node, source_root=args.source_root)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        sys.stdout.write(json.dumps({"status": "failed", "error_type": type(exc).__name__, "reason": str(exc)}) + "\n")
        return 1
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
