#!/usr/bin/env python3
"""Run the Codex Desktop metadata-only observer; default mode is continuous."""

from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import sys
from pathlib import Path

# 源码诊断和客户独立可执行文件共用入口，客户不必设置 PYTHONPATH。
SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if not getattr(sys, "frozen", False):
    for import_root in (SCRIPT_ROOT / "packages/contracts/src", SCRIPT_ROOT / "apps/cowork-runtime/src"):
        sys.path.insert(0, str(import_root))

from cowork_runtime.observer.codex_worker import CodexObserverWorker, CodexObserverRun
from cowork_runtime.telemetry.langfuse import LangfuseConfig

# Windows scheduled tasks start workers with pythonw.exe so no console window
# appears; pythonw has no stdout/stderr, so discard the per-poll report there.
for _stream in ("stdout", "stderr"):
    if getattr(sys, _stream) is None:
        setattr(sys, _stream, open(os.devnull, "w", encoding="utf-8"))


def _emit(report: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()


def _report(run: CodexObserverRun, worker: CodexObserverWorker) -> dict[str, object]:
    return {
        "status": "ok" if not run.errors else "failed", "files": run.files,
        "events": run.events, "terminal_enqueued": run.terminal_enqueued,
        "initialized": run.initialized, "errors": list(run.errors),
        "outbox": worker.outbox.count_by_status(),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path.home() / ".codex/sessions")
    parser.add_argument("--state-dir", type=Path, default=Path.home() / "Library/Application Support/AIHub/observer")
    parser.add_argument("--interval-seconds", type=float, default=5.0)
    parser.add_argument("--config-file", type=Path)
    parser.add_argument("--once", action="store_true", help="run one diagnostic pass and exit")
    parser.add_argument("--check", action="store_true", help="validate source and Observer authorization without consuming events")
    parser.add_argument("--runtime-info", action="store_true", help="print runtime identity and exit")
    parser.add_argument("--no-flush", action="store_true", help="local diagnostics only; requires --once")
    return parser


def main(argv: list[str] | None = None) -> int:
    forwarded = sys.argv[1:] if argv is None else argv
    if forwarded and forwarded[0] == "launch-agent":
        from install_observer_launch_agent import main as launch_agent_main
        return launch_agent_main(forwarded[1:])
    parser = _parser()
    args = parser.parse_args(forwarded)
    if args.runtime_info:
        _emit({"runtime": "codex", "contract_version": "component_event.v1", "version": "0.1.0",
               "python_version": platform.python_version(), "frozen": bool(getattr(sys, "frozen", False))})
        return 0
    if args.no_flush and not args.once:
        parser.error("--no-flush requires --once")
    if args.interval_seconds <= 0:
        parser.error("--interval-seconds must be positive")
    if args.config_file is not None:
        os.environ["LANGFUSE_CONFIG_FILE"] = str(args.config_file.expanduser().resolve())
    if args.check:
        try:
            source = args.source_root.expanduser().resolve()
            if source.is_dir():
                with os.scandir(source) as entries:
                    next(entries, None)
            else:
                with source.open("rb"):
                    pass
            LangfuseConfig.from_env()
            _emit({"status": "ok", "runtime": "codex", "source_root": str(source),
                   "state_dir": str(args.state_dir.expanduser().resolve()),
                   "config_file": os.environ.get("LANGFUSE_CONFIG_FILE")})
            return 0
        except (OSError, ValueError, RuntimeError) as exc:
            _emit({"status": "blocked", "runtime": "codex", "error_type": type(exc).__name__})
            return 1
    worker = None
    os.umask(0o077)
    try:
        worker = CodexObserverWorker(source_root=args.source_root, state_dir=args.state_dir,
                                     interval_seconds=args.interval_seconds, flush=not args.no_flush)
        if args.once:
            report = worker.run_once()
            _emit(_report(report, worker))
            return int(bool(report.errors))
        signal.signal(signal.SIGTERM, lambda *_: worker.request_stop())
        signal.signal(signal.SIGINT, lambda *_: worker.request_stop())
        # 失败仍明确写入状态并按周期重试，进程不伪报 delivered。
        worker.run_forever(lambda report: _emit(_report(report, worker)))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        _emit({"status": "failed", "errors": [{"error_type": type(exc).__name__}],
               "outbox": worker.outbox.count_by_status() if worker else {}})
        return 1
    finally:
        if worker is not None:
            worker.close()


if __name__ == "__main__":
    raise SystemExit(main())
