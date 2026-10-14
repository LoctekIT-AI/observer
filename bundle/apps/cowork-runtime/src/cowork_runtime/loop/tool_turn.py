"""Tool turn execution with policy gating."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from cowork_runtime.contracts import Approval, RuntimeError, RuntimeErrorCode, ToolCall, ToolResult
from cowork_runtime.contracts.enums import ToolCallStatus

from .interruption import make_approval
from .types import (
    PROVIDER_TOOL_ARGUMENTS_ERROR_METADATA_KEY,
    Policy,
    ToolExecutionContext,
    ToolHost,
)


@dataclass(frozen=True)
class ToolTurnOutcome:
    result: Optional[ToolResult] = None
    approval: Optional[Approval] = None
    execution_contract: dict[str, Any] | None = None


class ToolTurnRunner:
    def __init__(self, tool_host: ToolHost, policy: Policy) -> None:
        self.tool_host = tool_host
        self.policy = policy

    async def run(
        self,
        call: ToolCall,
        context: ToolExecutionContext,
        *,
        approval_already_granted: bool = False,
    ) -> ToolTurnOutcome:
        execution_contract = _execution_contract(self.tool_host, call, context)
        provider_arguments_error = _provider_tool_arguments_error(call)
        if provider_arguments_error is not None:
            return ToolTurnOutcome(
                result=ToolResult(
                    tool_call_id=call.tool_call_id,
                    run_id=call.run_id,
                    status=ToolCallStatus.FAILED,
                    error=provider_arguments_error,
                    metadata={"provider_tool_arguments_rejected": True},
                ),
                execution_contract=execution_contract,
            )
        if not approval_already_granted:
            decision = await context.cancellation_token.run(
                self.policy.check_tool_call(call, context)
            )
            if not decision.allowed:
                return ToolTurnOutcome(
                    result=ToolResult(
                        tool_call_id=call.tool_call_id,
                        run_id=call.run_id,
                        status=ToolCallStatus.FAILED,
                        error=RuntimeError(
                            code=RuntimeErrorCode.POLICY_DENIED,
                            message=decision.reason or "policy denied tool call",
                            details=(
                                {"reason_code": decision.reason_code, "do_not_retry": True}
                                if decision.reason_code
                                else {}
                            ),
                        ),
                    )
                )
            if decision.requires_approval:
                return ToolTurnOutcome(
                    approval=make_approval(
                        call.run_id,
                        call,
                        decision,
                        context_metadata=context.metadata,
                    ),
                    execution_contract=execution_contract,
                )
        return ToolTurnOutcome(
            result=await context.cancellation_token.run(
                self.tool_host.execute(call, context)
            ),
            execution_contract=execution_contract,
        )


def _provider_tool_arguments_error(call: ToolCall) -> RuntimeError | None:
    raw = call.metadata.get(PROVIDER_TOOL_ARGUMENTS_ERROR_METADATA_KEY)
    if not isinstance(raw, dict):
        return None
    kind = raw.get("argument_error_kind")
    safe_kind = kind if isinstance(kind, str) and kind in {
        "invalid_json",
        "missing",
        "missing_required",
        "not_object",
    } else "invalid"
    missing = raw.get("missing_arguments")
    missing_arguments = [
        name
        for name in missing[:32]
        if isinstance(name, str) and name and len(name) <= 128
    ] if isinstance(missing, list) else []
    detail = (
        f"; missing required arguments: {', '.join(missing_arguments)}"
        if missing_arguments
        else ""
    )
    return RuntimeError(
        code=RuntimeErrorCode.INVALID_REQUEST,
        message=f"provider returned invalid tool arguments{detail}",
        retryable=False,
        category="model_repairable",
        details={
            "reason_code": "provider.tool_arguments_invalid",
            "argument_error_kind": safe_kind,
            **({"missing_arguments": missing_arguments} if missing_arguments else {}),
        },
    )


def _execution_contract(
    tool_host: ToolHost,
    call: ToolCall,
    context: ToolExecutionContext,
) -> dict[str, Any]:
    """Read only a trusted in-process manifest contract, never model fields."""

    resolver = getattr(tool_host, "execution_contract_for", None)
    if not callable(resolver):
        return {}
    try:
        raw = resolver(call.tool_name, context)
    except TypeError:
        # Compatibility with external ToolHost implementations that predate
        # snapshot-aware execution contracts.
        raw = resolver(call.tool_name)
    if not isinstance(raw, dict):
        return {}
    idempotency = raw.get("idempotency")
    side_effect = raw.get("side_effect")
    stable_error_codes = raw.get("stable_error_codes")
    return {
        "idempotency": idempotency if idempotency in {"none", "keyed", "intrinsic"} else "none",
        "side_effect": side_effect if side_effect in {"none", "local_mutation", "remote_mutation"} else "none",
        "requires_stable_reason_code": raw.get("requires_stable_reason_code") is True,
        "stable_error_codes": (
            [
                code
                for code in stable_error_codes[:32]
                if isinstance(code, str) and code and len(code) <= 120
            ]
            if isinstance(stable_error_codes, list)
            else []
        ),
    }
