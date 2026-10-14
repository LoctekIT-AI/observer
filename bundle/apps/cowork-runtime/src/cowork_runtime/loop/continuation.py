"""Loop continuation decisions."""

from __future__ import annotations

from cowork_runtime.contracts import ToolResult
from cowork_runtime.contracts.enums import ToolCallStatus

from .types import ModelTurnOutput


def model_requested_tools(output: ModelTurnOutput) -> bool:
    return bool(output.tool_calls)


def tool_failed(result: ToolResult) -> bool:
    return result.status == ToolCallStatus.FAILED
