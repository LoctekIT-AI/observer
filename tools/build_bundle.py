#!/usr/bin/env python3
"""Build the Observer install package (observer-v<version>[-test].zip) from bundle/.

    python3 tools/build_bundle.py --version 0.2.26                 # production package
    python3 tools/build_bundle.py --version 0.2.26 --test          # test package (192.168.17.121)

Self-update: update.json in the package names the repository and channel the
installed updater follows, and the channel's public key from
tools/update-keys/<channel>.pub ("test" for --test builds, "release" otherwise).
Without a key file for the channel the package's updater stays disabled.

The source in bundle/ always carries the production authorization origin. A test
build replaces it with the test origin and marks the README as a test build.

Rules the installer depends on:
- The version must be raised for every build that changes any file; the installer
  skips activation when the manifest (including bundle_version) is unchanged.
- Windows PowerShell 5.1 reads BOM-less .ps1 files in the ANSI code page (GBK on
  Chinese Windows), so every .ps1 must be ASCII-only and is written with a UTF-8 BOM.
- The ZIP is deterministic (sorted names, fixed timestamps) so the same source and
  version always give the same SHA-256.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "bundle"
PRODUCTION_ORIGIN = "https://itsub.loctek.com/observer"
TEST_ORIGIN = "http://192.168.17.121:38090/observer"
FORBIDDEN_IN_TEST = (b"itsub.loctek.com", b"ai-os-test", b"ai-hubs")
SKIP_PARTS = {"__pycache__", ".DS_Store", ".pytest_cache"}
UPDATE_KEYS = ROOT / "tools" / "update-keys"


def collect() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in sorted(BUNDLE.rglob("*")):
        if not path.is_file() or any(part in SKIP_PARTS for part in path.relative_to(BUNDLE).parts):
            continue
        if path.suffix in {".pyc", ".pyo"}:
            continue
        files[path.relative_to(BUNDLE).as_posix()] = path.read_bytes()
    return files


def update_settings(files: dict[str, bytes], *, test: bool, repo: str | None) -> None:
    settings = json.loads(files["update.json"])
    channel = "test" if test else "release"
    key_file = UPDATE_KEYS / f"{channel}.pub"
    key = key_file.read_text(encoding="utf-8").strip() if key_file.is_file() else ""
    if key and not re.fullmatch(r"[0-9a-f]{64}", key):
        raise SystemExit(f"{key_file} must hold a 64-character hex Ed25519 public key")
    if test and not key:
        raise SystemExit(f"test builds need {key_file} (see tools/sign_channel.py keygen)")
    if not key:
        print(f"warning: no {key_file}; self-update is disabled in this package", file=sys.stderr)
    settings.update(channel=channel, public_key=key)
    if repo:
        settings["repo"] = repo
    files["update.json"] = (json.dumps(settings, indent=2, sort_keys=True) + "\n").encode()


def build(version: str, *, test: bool, out_dir: Path, repo: str | None = None) -> Path:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit("--version must look like 0.2.26")
    files = collect()

    manifest = json.loads(files["manifest.json"])
    manifest["bundle_version"] = version
    if files["manifest.json"].startswith(b"{\n"):
        files["manifest.json"] = json.dumps(manifest, indent=2, sort_keys=True).encode()
    else:
        files["manifest.json"] = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    for name in (".codex-plugin/plugin.json", ".codebuddy-plugin/plugin.json"):
        text, count = re.subn(r'"version": "[^"]*"', f'"version": "{version}"', files[name].decode(), count=1)
        if count != 1:
            raise SystemExit(f"no version field in {name}")
        files[name] = text.encode()

    update_settings(files, test=test, repo=repo)

    readme = files["README.md"].decode()
    if not readme.startswith("# Observer\n"):
        raise SystemExit("bundle/README.md must start with '# Observer'")
    title = f"# Observer {version}（测试版）\n\n> 测试版：默认授权地址为测试机 `{TEST_ORIGIN}`（内网 http），仅用于 192.168.17.121 测试环境验收，不连接正式站。\n" if test else f"# Observer {version}\n"
    files["README.md"] = (title + readme[len("# Observer\n"):]).encode()

    if test:
        for name, data in list(files.items()):
            if PRODUCTION_ORIGIN.encode() in data:
                files[name] = data.replace(PRODUCTION_ORIGIN.encode(), TEST_ORIGIN.encode())
        leaked = [name for name, data in files.items() if any(item in data for item in FORBIDDEN_IN_TEST)]
        if leaked:
            raise SystemExit(f"production / forbidden domain left in a test build: {leaked}")

    for name in list(files):
        if name.endswith(".ps1"):
            body = files[name][3:] if files[name].startswith(b"\xef\xbb\xbf") else files[name]
            if any(byte > 127 for byte in body):
                raise SystemExit(f"non-ASCII characters in {name} (Windows PowerShell 5.1 would misread them)")
            files[name] = b"\xef\xbb\xbf" + body

    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"observer-v{version}{'-test' if test else ''}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for name in sorted(files):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            bundle.writestr(entry, files[name], compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (out_dir / (archive.name + ".sha256")).write_text(f"{digest}  {archive.name}\n")
    print(f"{archive}  {digest}  ({len(files)} files)")
    return archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", required=True, help="bundle version, e.g. 0.2.26 (raise it for every build)")
    parser.add_argument("--test", action="store_true", help="test build for the 192.168.17.121 environment")
    parser.add_argument("--out", type=Path, default=ROOT / "dist", help="output directory (default: dist/)")
    parser.add_argument("--update-repo", help="owner/repo the installed updater follows (default: bundle/update.json)")
    args = parser.parse_args(argv)
    build(args.version, test=args.test, out_dir=args.out, repo=args.update_repo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
