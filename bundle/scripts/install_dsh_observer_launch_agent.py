#!/usr/bin/env python3
"""Install the DSH observer worker as a macOS LaunchAgent."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

_SCRIPT = Path(__file__).with_name("install_observer_launch_agent.py")
_SPEC = importlib.util.spec_from_file_location("dsh_launch_agent_base", _SCRIPT)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"cannot load launch-agent installer: {_SCRIPT}")
_BASE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _BASE
_SPEC.loader.exec_module(_BASE)
_BASE.LABEL = "com.observer.dsh-observer"
_BASE._default_plist = lambda: Path.home() / "Library/LaunchAgents/com.observer.dsh-observer.plist"
_BASE._default_source = lambda: Path.home() / ".aihub/observer/dsh"
_BASE._default_state = lambda: Path.home() / "Library/Application Support/AIHub/dsh-observer"
_BASE._default_worker = lambda: Path(__file__).with_name("dsh_observer_worker.py")

if __name__ == "__main__":
    raise SystemExit(_BASE.main())
