#!/usr/bin/env python3
"""Install and inspect the Claude Desktop observer as a macOS LaunchAgent."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


_SCRIPT = Path(__file__).with_name("install_observer_launch_agent.py")
_SPEC = importlib.util.spec_from_file_location("claude_launch_agent_base", _SCRIPT)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"cannot load launch-agent installer: {_SCRIPT}")
_BASE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _BASE
_SPEC.loader.exec_module(_BASE)

_BASE.LABEL = "com.aihub.claude-observer"
_BASE._default_plist = lambda: Path.home() / "Library" / "LaunchAgents" / "com.aihub.claude-observer.plist"
_BASE._default_source = lambda: Path.home() / "Library" / "Application Support" / "Claude" / "IndexedDB" / "https_claude.ai_0.indexeddb.leveldb"
_BASE._default_state = lambda: Path.home() / "Library" / "Application Support" / "AIHub" / "claude-observer"
_BASE._default_worker = lambda: Path(__file__).with_name("claude_observer_worker.py")


if __name__ == "__main__":
    raise SystemExit(_BASE.main())
