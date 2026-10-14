#!/usr/bin/env python3
"""Collect local observer events and report operations without printing source content."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
from pathlib import Path

from cowork_contracts import COMPONENT_EVENT_CONTRACT_VERSION, PluginManifestV1
from cowork_runtime.adapters import (
    ClaudeIncrementalAdapter,
    ClaudeDesktopAdapter,
    CodexIncrementalAdapter,
    PluginRegistry,
    WorkBuddyDesktopAdapter,
    WorkBuddyJsonlAdapter,
)
from cowork_runtime.persistence import SQLiteRuntimeStore
from cowork_runtime.telemetry import LangfuseConfig, LangfuseContentOutbox, LangfuseExporter

RUNTIMES = ("dsh", "codex", "claude", "workbuddy-desktop")
CLAUDE_DESKTOP_ROOT = Path.home() / "Library" / "Application Support" / "Claude" / "IndexedDB" / "https_claude.ai_0.indexeddb.leveldb"
CLAUDE_DESKTOP_SKILLS_ROOT = Path.home() / "Library" / "Application Support" / "Claude" / "local-agent-mode-sessions"


def _path_from_env(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser().resolve() if raw else default.expanduser().resolve()


def _source_roots() -> dict[str, Path]:
    home = Path.home()
    workbuddy_root = os.environ.get("WORKBUDDY_PROJECT_PATH")
    if workbuddy_root:
        workbuddy_source = Path(workbuddy_root).expanduser().resolve()
    else:
        observer_dir = _path_from_env("AIHUB_WORKBUDDY_EVENT_DIR", home / ".aihub" / "observer")
        workbuddy_source = observer_dir / "workbuddy-hook-events.jsonl"
    configured_claude = os.environ.get("CLAUDE_PROJECT_PATH")
    # Claude Desktop's IndexedDB is the only accepted source for this runtime.
    # Claude Code project JSONL is a different product and cannot be substituted
    # when the Desktop source is missing.
    claude_source = Path(configured_claude).expanduser().resolve() if configured_claude else CLAUDE_DESKTOP_ROOT
    return {
        "dsh": _path_from_env("DSH_OBSERVER_SOURCE_ROOT", home / ".aihub/observer/dsh"),
        "codex": _path_from_env("CODEX_ROLLOUT_PATH", home / ".codex/sessions"),
        "claude": claude_source,
        "workbuddy-desktop": workbuddy_source,
    }


def _supplemental_source_roots() -> dict[str, tuple[Path, ...]]:
    codex_app_server_root = os.environ.get("CODEX_APP_SERVER_EVIDENCE_PATH")
    return {
        "dsh": (),
        "codex": (
            (Path(codex_app_server_root).expanduser().resolve(),)
            if codex_app_server_root
            else ()
        ),
        "claude": (),
        "workbuddy-desktop": (),
    }


def _resolve_source(path: Path, pattern: str = "*.jsonl") -> Path:
    if not path.exists() or path.is_file():
        return path
    if not path.is_dir():
        raise ValueError(f"source is not a regular file or directory: {path}")
    if path == CLAUDE_DESKTOP_ROOT or CLAUDE_DESKTOP_ROOT in path.parents:
        patterns = ("*.ldb", "*.log")
        candidates = [item for candidate_pattern in patterns for item in path.rglob(candidate_pattern) if item.is_file()]
    else:
        candidates = [item for item in path.rglob(pattern) if item.is_file()]
    return max(candidates, key=lambda item: item.stat().st_mtime, default=path / pattern)


def _source_status(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"status": "missing", "path": str(path), "records": 0}
    if not path.is_file():
        raise ValueError(f"source is not a regular file: {path}")
    if CLAUDE_DESKTOP_ROOT in path.parents:
        return {"status": "present", "path": str(path), "records": 0}
    records = 0
    with path.open("rb") as handle:
        for line in handle:
            if line.endswith(b"\n"):
                records += 1
            elif line:
                raise ValueError(f"source contains a truncated final line: {path}")
    return {"status": "present", "path": str(path), "records": records}


def _session_installation_id(base: str, session_id: str) -> str:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
    return f"{base}-session-{digest}"


def _definition(runtime: str, source: Path, session_id: str) -> tuple[str, str, list[str], type]:
    if runtime == "claude" and CLAUDE_DESKTOP_ROOT in source.parents:
        return (
            "claude-desktop-adapter",
            # Bump the installation namespace when the Desktop parser learns
            # the native deferred-Skill activation record.  Existing cursors
            # must replay once so previously observed calls are not lost.
            _session_installation_id("install-claude-desktop-2", session_id),
            ["tool_calls", "desktop", "indexeddb", "deferred_skill_search"],
            ClaudeDesktopAdapter,
        )
    if runtime == "workbuddy-desktop" and source.name == "workbuddy-hook-events.jsonl":
        return (
            "workbuddy-desktop-adapter",
            _session_installation_id("install-workbuddy-desktop-1", session_id),
            ["tool_calls", "hooks", "desktop", "source:official-workbuddy-command-hook"],
            WorkBuddyDesktopAdapter,
        )
    if runtime == "codex" and source.name.endswith(".app-server.jsonl"):
        return (
            "codex-adapter",
            _session_installation_id("install-codex-1", session_id),
            ["tool_calls", "usage"],
            CodexIncrementalAdapter,
        )
    return {
        "codex": ("codex-adapter", "install-codex-1", ["tool_calls", "usage"], CodexIncrementalAdapter),
        "claude": ("claude-adapter", "install-claude-1", ["tool_calls", "usage"], ClaudeIncrementalAdapter),
        "workbuddy-desktop": ("workbuddy-desktop-adapter", "install-workbuddy-desktop-1", ["tool_calls", "hooks", "desktop", "source:official-workbuddy-command-hook"], WorkBuddyJsonlAdapter),
    }[runtime]


def _collect_one(runtime: str, source: Path, session_id: str, state_dir: Path) -> tuple[int, int]:
    if runtime == "dsh":
        from cowork_runtime.observer.dsh_worker import DshObserverWorker
        worker = DshObserverWorker(source_root=source, state_dir=state_dir, flush=False)
        try:
            result = worker.run_once()
            return result.events, result.terminal_enqueued
        finally:
            worker.close()
    state_dir.mkdir(parents=True, exist_ok=True)
    store = SQLiteRuntimeStore(state_dir / "observer.sqlite3")
    outbox = LangfuseContentOutbox(state_dir / "langfuse-content-outbox.sqlite3")
    try:
        registry = PluginRegistry(store)
        plugin_id, installation_id, capabilities, adapter_type = _definition(runtime, source, session_id)
        if store.get_adapter_plugin(plugin_id, installation_id) is None:
            registry.register(PluginManifestV1(
                plugin_id=plugin_id, runtime="workbuddy" if runtime == "workbuddy-desktop" else runtime,
                contract_version=COMPONENT_EVENT_CONTRACT_VERSION, installation_id=installation_id,
                enabled=True, capabilities=capabilities,
            ))
        adapter = adapter_type(registry)
        kwargs: dict[str, object] = {"installation_id": installation_id, "session_id": session_id}
        if isinstance(adapter, ClaudeDesktopAdapter):
            skill_ids = tuple(
                path.parent.name
                for path in CLAUDE_DESKTOP_SKILLS_ROOT.rglob("SKILL.md")
                if path.is_file()
            ) if CLAUDE_DESKTOP_SKILLS_ROOT.is_dir() else ()
            kwargs["skill_ids"] = skill_ids
        result = adapter.read(source, **kwargs)
        terminal = [event for event in result.events if event.stage != "started"]
        for event in terminal:
            outbox.enqueue(event)
        return len(result.events), len(terminal)
    finally:
        outbox.close()
        store.close()


def _jsonl_session_ids(runtime: str, source: Path) -> tuple[str, ...]:
    if runtime == "claude" and CLAUDE_DESKTOP_ROOT in source.parents:
        return (f"claude-desktop:{source.name}",)
    if runtime == "workbuddy-desktop" and source.name == "workbuddy-hook-events.jsonl":
        field = "session_id"
    elif runtime == "codex" and source.name.endswith(".app-server.jsonl"):
        field = "threadId"
    else:
        return (source.stem,)

    session_ids: list[str] = []
    seen: set[str] = set()
    with source.open("rb") as handle:
        while True:
            offset = handle.tell()
            raw_line = handle.readline()
            if not raw_line:
                break
            if not raw_line.endswith(b"\n"):
                raise ValueError(f"source contains a truncated final line: {source} (offset={offset})")
            try:
                record = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid JSONL record: {exc} (path={source}, offset={offset})") from exc
            if not isinstance(record, dict):
                continue
            value = record.get(field)
            if field == "threadId":
                params = record.get("params")
                value = params.get(field) if isinstance(params, dict) else None
            if isinstance(value, str) and value.strip() and value.strip() not in seen:
                canonical = value.strip()
                seen.add(canonical)
                session_ids.append(canonical)
    return tuple(session_ids)


def _scan(
    runtime: str,
    root: Path | tuple[Path, ...],
    state_dir: Path,
    max_files: int,
    max_file_bytes: int,
) -> dict[str, object]:
    if max_files < 1 or max_file_bytes < 1:
        raise ValueError("scan limits must be positive integers")
    roots = (root,) if isinstance(root, Path) else root
    files: list[Path] = []
    seen_files: set[Path] = set()
    for source_root in roots:
        if runtime == "dsh":
            source_root.stat()
        if not source_root.exists():
            candidates: list[Path] = []
        elif source_root.is_file():
            candidates = [source_root]
        elif source_root.is_dir():
            is_claude_desktop_root = runtime == "claude" and (
                source_root == CLAUDE_DESKTOP_ROOT or CLAUDE_DESKTOP_ROOT in source_root.parents
            )
            patterns = ("*.ldb", "*.log") if is_claude_desktop_root else ("*.jsonl",)
            candidates = [item for pattern in patterns for item in source_root.rglob(pattern) if item.is_file()]
        else:
            raise ValueError(f"source is not a regular file or directory: {source_root}")
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved not in seen_files:
                seen_files.add(resolved)
                files.append(resolved)
    files.sort(key=lambda item: (item.stat().st_mtime, str(item)), reverse=True)
    discovered = len(files)
    within_size = [item for item in files if item.stat().st_size <= max_file_bytes]
    selected = within_size[:max_files]
    errors: list[dict[str, str]] = []
    events = terminal = 0
    for source in selected:
        try:
            for session_id in _jsonl_session_ids(runtime, source):
                event_count, terminal_count = _collect_one(runtime, source, session_id, state_dir)
                events += event_count
                terminal += terminal_count
        except (OSError, RuntimeError, ValueError) as exc:
            summary = str(exc).splitlines()[0]
            errors.append({"path": str(source), "error_type": type(exc).__name__, "reason": summary})
    return {
        "status": "ok" if not errors else "partial", "runtime": runtime,
        "discovered": discovered, "selected": len(selected),
        "omitted_by_count": max(0, len(within_size) - max_files),
        "omitted_by_size": discovered - len(within_size), "errors": errors,
        "events": events, "terminal_enqueued": terminal,
    }


def _flush(state_dir: Path) -> dict[str, object]:
    outbox = LangfuseContentOutbox(state_dir / "langfuse-content-outbox.sqlite3")
    try:
        report = outbox.flush(LangfuseExporter(LangfuseConfig.from_env()).export, owner_id=f"observer-{os.getpid()}")
    finally:
        outbox.close()
    if report.failed:
        raise RuntimeError(f"{report.failed} Observer uploads failed and remain pending")
    return {"status": "ok", "delivered": report.delivered, "failed": report.failed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "doctor", "collect", "scan", "summarize", "flush", "run-once"))
    parser.add_argument("--runtime", choices=RUNTIMES)
    parser.add_argument("--source")
    parser.add_argument("--session-id")
    parser.add_argument("--state-dir", default=str(Path.home() / ".aihub/observer"))
    parser.add_argument("--max-files", type=int, default=100)
    parser.add_argument("--max-file-bytes", type=int, default=64 * 1024 * 1024)
    args = parser.parse_args(argv)
    try:
        roots = _source_roots()
        supplemental_roots = _supplemental_source_roots()
        state_dir = Path(args.state_dir).expanduser().resolve()
        if args.command == "status":
            payload = {"schema_version": "skill_mcp_observer_status.v1", "sources": {name: _source_status(_resolve_source(path)) for name, path in roots.items()}}
        elif args.command == "doctor":
            state_dir.mkdir(parents=True, exist_ok=True)
            payload = {"status": "ok", "platform": platform.system().lower(), "state_dir": str(state_dir), "observer": {"authorization": "company_site", "receiver": "internal"}, "sources": {name: str(path) for name, path in roots.items()}}
        elif args.command == "summarize":
            outbox = LangfuseContentOutbox(state_dir / "langfuse-content-outbox.sqlite3")
            try:
                delivery = outbox.delivery_status()
                payload = {
                    "status": "ok",
                    "outbox": {
                        "pending": delivery.pending,
                        "delivered": delivery.delivered,
                        "last_error": delivery.last_error,
                    },
                }
            finally:
                outbox.close()
        elif args.command == "flush":
            payload = _flush(state_dir)
        elif args.command == "collect":
            if not args.runtime or (args.runtime != "dsh" and not args.session_id):
                raise ValueError("collect requires --runtime and --session-id")
            source = Path(args.source).expanduser().resolve() if args.source else _resolve_source(roots[args.runtime])
            events, terminal = _collect_one(args.runtime, source, args.session_id or "native", state_dir)
            payload = {"status": "ok", "runtime": args.runtime, "events": events, "terminal_enqueued": terminal}
        else:
            runtimes = (args.runtime,) if args.runtime else tuple(runtime for runtime in RUNTIMES if runtime != "dsh" or platform.system() == "Darwin")
            reports = [
                _scan(
                    runtime,
                    Path(args.source).expanduser().resolve()
                    if args.source
                    else (roots[runtime], *supplemental_roots[runtime]),
                    state_dir,
                    args.max_files,
                    args.max_file_bytes,
                )
                for runtime in runtimes
            ]
            scan_payload = reports[0] if len(reports) == 1 else {"status": "ok" if all(item["status"] == "ok" for item in reports) else "partial", "scans": reports}
            payload = scan_payload if args.command == "scan" else {"status": scan_payload["status"], "scan": scan_payload, "flush": _flush(state_dir)}
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
