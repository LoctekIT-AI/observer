"""JSON codec shared by Runtime checkpoint stores.

The Runtime loop keeps its checkpoint as a dataclass while persistence ports
store JSON documents.  Keeping the codec in the shared Runtime package avoids
API adapters reaching into the SQLite implementation's private helpers.
"""

from __future__ import annotations

from typing import Any, Mapping

from cowork_runtime.contracts import Approval, ArtifactRef, ToolCall
from cowork_runtime.contracts.enums import RunStatus
from cowork_runtime.loop.types import LoopCheckpoint, ToolCallIntent


def checkpoint_to_dict(checkpoint: LoopCheckpoint) -> dict[str, Any]:
    """Serialize the durable fields of one Runtime loop checkpoint."""
    return {
        "run_id": checkpoint.run_id,
        "checkpoint_key": checkpoint.checkpoint_key,
        "session_id": checkpoint.session_id,
        "messages": list(checkpoint.messages),
        "status": checkpoint.status.value,
        "model_turns": checkpoint.model_turns,
        "model_tokens": checkpoint.model_tokens,
        "tool_calls": checkpoint.tool_calls,
        "failures": checkpoint.failures,
        "repair_attempts": checkpoint.repair_attempts,
        "transient_retries": checkpoint.transient_retries,
        "model_unavailable_retries": checkpoint.model_unavailable_retries,
        "repair_error_signatures": dict(checkpoint.repair_error_signatures),
        "last_error_signature": checkpoint.last_error_signature,
        "recovery_state": dict(checkpoint.recovery_state),
        "pending_tool_call": (
            checkpoint.pending_tool_call.model_dump(mode="json")
            if checkpoint.pending_tool_call
            else None
        ),
        "pending_tool_intents": [
            {
                "tool_name": intent.tool_name,
                "arguments": dict(intent.arguments),
                "metadata": dict(intent.metadata),
            }
            for intent in checkpoint.pending_tool_intents
        ],
        "pending_approval": (
            checkpoint.pending_approval.model_dump(mode="json")
            if checkpoint.pending_approval
            else None
        ),
        "final_content": checkpoint.final_content,
        "artifact_refs": [artifact.model_dump(mode="json") for artifact in checkpoint.artifact_refs],
        "delivery_outcome": (
            checkpoint.delivery_outcome.model_dump(mode="json")
            if checkpoint.delivery_outcome
            else None
        ),
        "metadata": dict(checkpoint.metadata),
    }


def checkpoint_from_dict(data: Mapping[str, Any]) -> LoopCheckpoint:
    """Deserialize a checkpoint and fail closed on malformed status data."""
    return LoopCheckpoint(
        run_id=str(data["run_id"]),
        checkpoint_key=(str(data["checkpoint_key"]) if data.get("checkpoint_key") else None),
        session_id=str(data["session_id"]),
        messages=list(data.get("messages") or []),
        status=RunStatus(data.get("status", RunStatus.CREATED.value)),
        model_turns=int(data.get("model_turns", 0)),
        model_tokens=int(data.get("model_tokens", 0)),
        tool_calls=int(data.get("tool_calls", 0)),
        failures=int(data.get("failures", 0)),
        repair_attempts=int(data.get("repair_attempts", 0)),
        transient_retries=int(data.get("transient_retries", 0)),
        model_unavailable_retries=int(data.get("model_unavailable_retries", 0)),
        repair_error_signatures=dict(data.get("repair_error_signatures") or {}),
        last_error_signature=data.get("last_error_signature"),
        recovery_state=dict(data.get("recovery_state") or {}),
        pending_tool_call=(
            ToolCall.model_validate(data["pending_tool_call"])
            if data.get("pending_tool_call")
            else None
        ),
        pending_tool_intents=[
            ToolCallIntent(
                tool_name=str(item["tool_name"]),
                arguments=dict(item.get("arguments") or {}),
                metadata=dict(item.get("metadata") or {}),
            )
            for item in data.get("pending_tool_intents") or []
            if isinstance(item, Mapping)
            and isinstance(item.get("tool_name"), str)
            and item["tool_name"].strip()
            and isinstance(item.get("arguments") or {}, Mapping)
            and isinstance(item.get("metadata") or {}, Mapping)
        ],
        pending_approval=(
            Approval.model_validate(data["pending_approval"])
            if data.get("pending_approval")
            else None
        ),
        final_content=str(data.get("final_content") or ""),
        artifact_refs=[
            ArtifactRef.model_validate(item)
            for item in data.get("artifact_refs") or []
        ],
        delivery_outcome=(
            _parse_delivery_outcome(data.get("delivery_outcome"))
            if data.get("delivery_outcome")
            else None
        ),
        metadata=dict(data.get("metadata") or {}),
    )


def _parse_delivery_outcome(value: object):
    """Read v1 outcome data conservatively; old checkpoints remain readable."""

    from cowork_contracts import DeliveryOutcome

    try:
        return DeliveryOutcome.model_validate(value)
    except (TypeError, ValueError):
        return None
