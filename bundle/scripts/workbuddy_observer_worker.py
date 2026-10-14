#!/usr/bin/env python3
"""Run the WorkBuddy Desktop metadata-only observer; default mode is continuous."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if not getattr(sys, "frozen", False):
    for import_root in (SCRIPT_ROOT / "packages/contracts/src", SCRIPT_ROOT / "apps/cowork-runtime/src"):
        sys.path.insert(0, str(import_root))

from cowork_runtime.observer.workbuddy_worker import WorkBuddyDesktopObserverWorker, WorkBuddyObserverRun
from cowork_runtime.telemetry.langfuse import LangfuseConfig

# Windows scheduled tasks start workers with pythonw.exe so no console window
# appears; pythonw has no stdout/stderr, so discard the per-poll report there.
for _stream in ("stdout", "stderr"):
    if getattr(sys, _stream) is None:
        setattr(sys, _stream, open(os.devnull, "w", encoding="utf-8"))


def _emit(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()


def _report(run: WorkBuddyObserverRun, worker: WorkBuddyDesktopObserverWorker) -> dict[str, object]:
    return {
        "status": "ok" if not run.errors else "failed",
        "runtime": "workbuddy",
        "files": run.files,
        "sessions": run.sessions,
        "events": run.events,
        "terminal_enqueued": run.terminal_enqueued,
        "initialized": run.initialized,
        "errors": list(run.errors),
        "outbox": worker.outbox.count_by_status(),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path.home() / ".aihub/observer/workbuddy-hook-events.jsonl")
    parser.add_argument("--state-dir", type=Path, default=Path.home() / "Library/Application Support/AIHub/workbuddy-observer")
    parser.add_argument("--interval-seconds", type=float, default=5.0)
    parser.add_argument("--config-file", type=Path)
    parser.add_argument("--once", action="store_true", help="run one diagnostic pass and exit")
    parser.add_argument("--check", action="store_true", help="validate source and Observer authorization without consuming events")
    parser.add_argument("--no-flush", action="store_true", help="local diagnostics only; requires --once")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    parser = _parser()
    if args.no_flush and not args.once:
        parser.error("--no-flush requires --once")
    if args.interval_seconds <= 0:
        parser.error("--interval-seconds must be positive")
    if args.config_file is not None:
        os.environ["LANGFUSE_CONFIG_FILE"] = str(args.config_file.expanduser().resolve())
    if args.check:
        try:
            source = args.source_root.expanduser().resolve()
            if not source.exists() or not source.is_file():
                raise OSError(f"source is unavailable: {source}")
            LangfuseConfig.from_env()
            _emit({"status": "ok", "runtime": "workbuddy", "source_root": str(source), "state_dir": str(args.state_dir.expanduser().resolve())})
            return 0
        except (OSError, ValueError, RuntimeError) as exc:
            _emit({"status": "blocked", "runtime": "workbuddy", "error_type": type(exc).__name__})
            return 1

    worker: WorkBuddyDesktopObserverWorker | None = None
    os.umask(0o077)
    try:
        worker = WorkBuddyDesktopObserverWorker(
            source_root=args.source_root,
            state_dir=args.state_dir,
            interval_seconds=args.interval_seconds,
            flush=not args.no_flush,
        )
        if args.once:
            report = worker.run_once()
            _emit(_report(report, worker))
            return int(bool(report.errors))
        signal.signal(signal.SIGTERM, lambda *_: worker.request_stop())
        signal.signal(signal.SIGINT, lambda *_: worker.request_stop())
        worker.run_forever(lambda report: _emit(_report(report, worker)))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        _emit({"status": "failed", "runtime": "workbuddy", "errors": [{"error_type": type(exc).__name__}]})
        return 1
    finally:
        if worker is not None:
            worker.close()


if __name__ == "__main__":
    raise SystemExit(main())
