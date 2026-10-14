#!/usr/bin/env python3
"""Run the native DSH metadata-only observer worker."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for source in (ROOT / "packages/contracts/src", ROOT / "apps/cowork-runtime/src"):
    sys.path.insert(0, str(source))

from cowork_runtime.observer.dsh_worker import DshObserverWorker
from cowork_runtime.telemetry.langfuse import LangfuseConfig

# Windows scheduled tasks start workers with pythonw.exe so no console window
# appears; pythonw has no stdout/stderr, so discard the per-poll report there.
for _stream in ("stdout", "stderr"):
    if getattr(sys, _stream) is None:
        setattr(sys, _stream, open(os.devnull, "w", encoding="utf-8"))


def _emit(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path.home() / ".aihub/observer/dsh")
    parser.add_argument("--state-dir", type=Path, default=Path.home() / "Library/Application Support/AIHub/dsh-observer")
    parser.add_argument("--interval-seconds", type=float, default=5)
    parser.add_argument("--config-file", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-flush", action="store_true", help="local-only diagnostics; requires --once or --check")
    args = parser.parse_args(argv)
    if args.no_flush and not (args.once or args.check):
        parser.error("--no-flush requires --once or --check")
    if args.config_file is not None:
        os.environ["LANGFUSE_CONFIG_FILE"] = str(args.config_file.expanduser().resolve())
    worker: DshObserverWorker | None = None
    os.umask(0o077)
    try:
        source = args.source_root.expanduser().resolve()
        if args.check:
            source.stat()
            if not (source.is_file() or source.is_dir()):
                raise ValueError("DSH source is not a file or directory")
            if source.is_file():
                with source.open("rb"):
                    pass
            else:
                with os.scandir(source):
                    pass
            if not args.no_flush:
                LangfuseConfig.from_env()
            _emit({"status": "ok", "runtime": "dsh", "source_root": str(source), "delivery": "local-only" if args.no_flush else "configured"})
            return 0
        worker = DshObserverWorker(source_root=source, state_dir=args.state_dir, flush=not args.no_flush, interval_seconds=args.interval_seconds)
        def report(result: object) -> None:
            _emit({"status": "ok", "runtime": "dsh", **asdict(result), "outbox": worker.outbox.count_by_status()})
        if args.once:
            report(worker.run_once())
        else:
            signal.signal(signal.SIGTERM, lambda *_: worker.request_stop())
            signal.signal(signal.SIGINT, lambda *_: worker.request_stop())
            worker.run_forever(report)
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        _emit({"status": "failed", "runtime": "dsh", "error_type": type(exc).__name__, "reason": str(exc)})
        return 1
    finally:
        if worker is not None:
            worker.close()


if __name__ == "__main__":
    raise SystemExit(main())
