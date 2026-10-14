"""Inspect WorkBuddy's normal desktop MCP launch boundary."""

from __future__ import annotations

import shlex
from collections.abc import Sequence


def inspect_workbuddy_command(command: str | Sequence[str]) -> dict[str, object]:
    """Describe strict MCP configuration without reading MCP secrets."""

    argv = shlex.split(command) if isinstance(command, str) else [str(item) for item in command]
    strict = "--strict-mcp-config" in argv
    config_path: str | None = None
    for index, item in enumerate(argv[:-1]):
        if item == "--mcp-config":
            config_path = argv[index + 1]
            break
    if config_path:
        mode = "explicit_mcp_config"
    elif strict:
        # WorkBuddy's desktop host can inject the effective config at runtime;
        # a plugin-root .mcp.json is intentionally not treated as authoritative.
        mode = "application_projected_runtime_config"
    else:
        mode = "default_client_config"
    return {
        "strict_mode": strict,
        "mcp_config_path": config_path,
        "config_mode": mode,
        "observer_registration": "native_command_hooks",
    }


__all__ = ["inspect_workbuddy_command"]
