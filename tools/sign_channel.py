#!/usr/bin/env python3
"""Release signing for Observer self-update.

    # once per channel, on the release machine (prints only the public key):
    python3 tools/sign_channel.py keygen --key ~/observer-signing/test.key

    # for every release, after building the ZIP:
    python3 tools/sign_channel.py sign --key ~/observer-signing/test.key \
        --channel test --archive dist/observer-v0.2.26-test.zip --out channels/test.json

    # anyone can check a channel file against the public key in the repository:
    python3 tools/sign_channel.py verify --channel test --entry channels/test.json \
        --archive dist/observer-v0.2.26-test.zip

The private key file holds the 32-byte Ed25519 seed as hex. Keep it on the release
machine only (mode 600); never commit it, paste it into chat or put it in a bundle.
The public key goes into tools/update-keys/<channel>.pub and from there into every
bundle built for that channel (see tools/build_bundle.py).

Installed collectors only update when channels/<channel>.json on the main branch
names a newer version, so publish the GitHub release first, verify it, and commit
the channel file last.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "bundle" / "scripts"))
import observer_update as update  # noqa: E402

KEYS = ROOT / "tools" / "update-keys"


def _read_seed(path: Path) -> bytes:
    text = path.expanduser().read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise SystemExit("key file must contain 64 lowercase hex characters")
    if os.name != "nt" and path.expanduser().stat().st_mode & 0o077:
        raise SystemExit("key file must not be readable by other users (chmod 600)")
    return bytes.fromhex(text)


def keygen(args: argparse.Namespace) -> int:
    path = args.key.expanduser()
    if path.exists():
        raise SystemExit(f"{path} already exists; refusing to overwrite a signing key")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    seed = secrets.token_bytes(32)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(seed.hex() + "\n")
    print(update.ed25519_public_key(seed).hex())
    return 0


def public(args: argparse.Namespace) -> int:
    print(update.ed25519_public_key(_read_seed(args.key)).hex())
    return 0


def _version_from_archive(archive: Path, channel: str) -> str:
    match = re.fullmatch(r"observer-v(\d+\.\d+\.\d+)" + re.escape(update.channel_suffix(channel)) + r"\.zip", archive.name)
    if not match:
        raise SystemExit(f"{archive.name} is not an observer-v<version>{update.channel_suffix(channel)}.zip archive")
    return match.group(1)


def sign(args: argparse.Namespace) -> int:
    seed = _read_seed(args.key)
    expected = KEYS / f"{args.channel}.pub"
    if expected.is_file() and expected.read_text().strip() != update.ed25519_public_key(seed).hex():
        raise SystemExit(f"this key does not match {expected}")
    data = args.archive.read_bytes()
    entry = update.make_channel_entry(channel=args.channel, version=_version_from_archive(args.archive, args.channel), archive=data, seed=seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{args.out}: {entry['tag']} sha256 {entry['sha256']}")
    return 0


def verify(args: argparse.Namespace) -> int:
    key = (args.public_key or (KEYS / f"{args.channel}.pub").read_text()).strip()
    entry = json.loads(args.entry.read_text(encoding="utf-8"))
    update.verify_channel_entry(entry, channel=args.channel, public_key=bytes.fromhex(key))
    if args.archive is not None:
        data = args.archive.read_bytes()
        import hashlib
        if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"] or args.archive.name != entry["asset"]:
            raise SystemExit("archive does not match the signed channel entry")
    print(f"ok: {entry['tag']} signed by {key[:16]}…")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("keygen"); p.add_argument("--key", type=Path, required=True); p.set_defaults(func=keygen)
    p = sub.add_parser("public"); p.add_argument("--key", type=Path, required=True); p.set_defaults(func=public)
    p = sub.add_parser("sign")
    p.add_argument("--key", type=Path, required=True)
    p.add_argument("--channel", required=True)
    p.add_argument("--archive", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.set_defaults(func=sign)
    p = sub.add_parser("verify")
    p.add_argument("--channel", required=True)
    p.add_argument("--entry", type=Path, required=True)
    p.add_argument("--archive", type=Path)
    p.add_argument("--public-key")
    p.set_defaults(func=verify)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
