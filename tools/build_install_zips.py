#!/usr/bin/env python3
"""Build the double-click installer zips in install/.

A browser download of a bare .command file loses its executable bit, so macOS refuses
to run it on double-click. A zip keeps the bit (Archive Utility restores it), and a
zip is always downloaded rather than shown as text. The Windows .cmd is zipped too so
both platforms download the same way.

The zips are deterministic (fixed timestamp, fixed modes), so rebuilding from unchanged
sources gives the same bytes. `--check` exits 1 when a zip is missing or out of date.
"""
from __future__ import annotations

import argparse
import io
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "install"
FIXED_DATE = (2026, 1, 1, 0, 0, 0)

# zip name -> [(file name inside the zip, unix mode)]
ZIPS = {
    "Install-Observer-macOS.zip": [("Install-Observer-macOS.command", 0o755)],
    "Install-Observer-Windows.zip": [("Install-Observer-Windows.cmd", 0o644)],
}


def build(name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for member, mode in ZIPS[name]:
            info = zipfile.ZipInfo(member, date_time=FIXED_DATE)
            info.create_system = 3  # Unix, so the mode below is honoured
            info.external_attr = (0o100000 | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, (INSTALL / member).read_bytes())
    return buf.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only verify the zips are up to date")
    args = parser.parse_args()
    stale = []
    for name in ZIPS:
        data = build(name)
        target = INSTALL / name
        if target.exists() and target.read_bytes() == data:
            print(f"up to date: install/{name}")
            continue
        if args.check:
            stale.append(name)
            print(f"OUT OF DATE: install/{name}")
        else:
            target.write_bytes(data)
            print(f"wrote install/{name} ({len(data)} bytes)")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
