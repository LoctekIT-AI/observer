"""Context assembly for model turns."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict

from cowork_runtime.contracts import ToolResult, safe_error_context, safe_failure_output_context
from cowork_runtime.contracts.enums import ToolCallStatus
from cowork_runtime.contracts import SessionMessage
from cowork_runtime.persistence.session_store import SessionMessageStore
from cowork_runtime.context import (
    PROVIDER_REASONING_CONTENT_METADATA_KEY,
    ContextCompactionCoordinator,
    ContextPolicy,
    DEFAULT_CONTEXT_POLICY,
    build_context_budget,
    record_provider_prompt_usage,
)
from cowork_runtime.context.tool_refs import build_tool_result_context

from .cancellation import CancellationToken
from .context_assembler import ContextAssembler
from .context_window import record_compaction_window
from .execution_trace import append_trace
from .types import LoopCheckpoint, LoopContext, LoopInput, ModelTurnOutput


class ContextBuilder:
    def __init__(
        self,
        session_store: SessionMessageStore | None = None,
        *,
        policy: ContextPolicy = DEFAULT_CONTEXT_POLICY,
        compaction: ContextCompactionCoordinator | None = None,
    ) -> None:
        self.session_store = session_store
        self.policy = policy
        self.context_assembler = ContextAssembler(session_store, policy=policy)
        self.compaction = compaction or ContextCompactionCoordinator(session_store, policy=policy)

    def new_checkpoint(self, loop_input: LoopInput) -> LoopCheckpoint:
        assembly = self.context_assembler.assemble(loop_input)
        initial_system_messages = [
            {
                "role": "system",
                "content": content,
                "metadata": {"_runtime_initial_context": True},
            }
            for content in loop_input.initial_system_messages
            if isinstance(content, str) and content
        ]
        metadata = {**dict(loop_input.metadata), **assembly.metadata}
        if loop_input.context_snapshot is not None:
            # Persisted Run metadata makes trace/recovery inspect the exact
            # revision the provider consumed, without asking the renderer to
            # recreate task semantics after a sidecar restart.
            metadata["task_envelope"] = loop_input.context_snapshot.task_envelope.model_dump(mode="json")
            metadata["context_snapshot"] = loop_input.context_snapshot.model_dump(mode="json")
            module_constraints = loop_input.context_snapshot.task_envelope.module_constraints
            metadata["module_constraints"] = module_constraints.model_dump(mode="json")
        return LoopCheckpoint(
            run_id=loop_input.run_id,
            session_id=loop_input.session_id,
            messages=[*initial_system_messages, *assembly.messages],
            checkpoint_key=loop_input.checkpoint_key,
            metadata=metadata,
        )

    def build(
        self,
        checkpoint: LoopCheckpoint,
        cancellation_token: CancellationToken,
    ) -> LoopContext:
        # Compaction is evaluated immediately before each provider call. The
        # checkpoint remains the durable run source of truth; only its
        # provider-facing message view is replaced.
        compaction = self.compaction.prepare(checkpoint)
        if compaction is not None and compaction.applied:
            window = record_compaction_window(
                checkpoint.metadata,
                compaction_id=compaction.compaction_id,
                tokens_before=compaction.tokens_before,
                tokens_after=compaction.tokens_after,
            )
            append_trace(
                checkpoint.metadata,
                kind="compacted",
                run_id=checkpoint.run_id,
                turn_id=checkpoint.run_id,
                payload={
                    "compaction_id": compaction.compaction_id,
                    "window_number": window.get("window_number"),
                    "tokens_before": compaction.tokens_before,
                    "tokens_after": compaction.tokens_after,
                },
                status="completed",
            )
        return LoopContext(
            run_id=checkpoint.run_id,
            session_id=checkpoint.session_id,
            messages=deepcopy(checkpoint.messages),
            metadata=dict(checkpoint.metadata),
            cancellation_token=cancellation_token,
        )

    def append_assistant(
        self,
        checkpoint: LoopCheckpoint,
        content: str,
        *,
        metadata: Dict[str, Any] | None = None,
    ) -> None:
        private_metadata = _provider_reasoning_metadata(metadata)
        if content or private_metadata:
            self._append_message(
                checkpoint,
                {
                    "role": "assistant",
                    "content": content or None,
                    **({"metadata": private_metadata} if private_metadata else {}),
                },
            )
        if content:
            checkpoint.final_content = content
        self.compaction.observe(checkpoint)

    def append_system(
        self,
        checkpoint: LoopCheckpoint,
        content: str,
        *,
        metadata: Dict[str, Any] | None = None,
    ) -> None:
        """Persist a Runtime-originated control instruction with the context."""

        self._append_message(
            checkpoint,
            {"role": "system", "content": content, "metadata": dict(metadata or {})},
        )
        self.compaction.observe(checkpoint)

    def append_model_output(
        self,
        checkpoint: LoopCheckpoint,
        output: ModelTurnOutput,
        *,
        record_final_content: bool = True,
    ) -> None:
        message: Dict[str, Any] = {
            "role": "assistant",
            "content": output.content or None,
        }
        private_metadata = _provider_reasoning_metadata(output.metadata)
        reasoning_content = private_metadata.get(PROVIDER_REASONING_CONTENT_METADATA_KEY)
        if reasoning_content:
            # 仅将 Think 协议字段保存为私有会话 metadata。后续由 provider
            # projection 精确回传，任何 Runtime 用户事件都不会包含它。
            message["metadata"] = private_metadata
        tool_calls = []
        for intent in output.tool_calls:
            provider_call_id = intent.metadata.get("provider_tool_call_id")
            if not provider_call_id:
                continue
            tool_calls.append(
                {
                    "id": provider_call_id,
                    "type": "function",
                    "function": {
                        "name": intent.tool_name,
                        "arguments": intent.arguments,
                    },
                }
            )
        if tool_calls:
            message["tool_calls"] = tool_calls
        if output.content or tool_calls or reasoning_content:
            self._append_message(checkpoint, message)
        # Tool-call turns may contain a natural-language preface such as
        # "I will generate the report". It remains part of the provider
        # context, but only a no-tool turn is a candidate terminal response.
        if record_final_content and output.content and not output.tool_calls:
            checkpoint.final_content = output.content
        self.compaction.observe(checkpoint)

    def record_provider_usage(
        self,
        checkpoint: LoopCheckpoint,
        metadata: Dict[str, Any],
    ) -> None:
        """Calibrate future context checks from trusted provider usage only."""

        # The gateway returns the resolved model and its capability profile.
        # Persist these facts before the next compaction check so a model
        # upgrade immediately changes the local context budget.
        for key in (
            "model",
            "selection_mode",
            "requested_model",
            "routing_reason",
            "routing_tier",
            "model_context_profile",
        ):
            value = metadata.get(key)
            if value is not None:
                checkpoint.metadata[key] = value
                if key == "model":
                    checkpoint.metadata["resolved_model"] = value

        effective_max_tokens = metadata.get("provider_effective_max_tokens")
        if (
            isinstance(effective_max_tokens, int)
            and not isinstance(effective_max_tokens, bool)
            and effective_max_tokens >= 1
        ):
            checkpoint.metadata["provider_effective_max_tokens"] = effective_max_tokens
        usage = metadata.get("usage")
        if not isinstance(usage, dict):
            return
        budget = build_context_budget(
            checkpoint.messages,
            metadata=checkpoint.metadata,
            policy=self.policy,
        )
        overhead = metadata.get("provider_request_fixed_overhead_tokens")
        record_provider_prompt_usage(
            checkpoint.metadata,
            usage,
            local_estimated_input_tokens=budget.local_estimated_input_tokens,
            fixed_overhead_tokens=overhead if isinstance(overhead, int) and not isinstance(overhead, bool) else 0,
        )

    def append_tool_result(self, checkpoint: LoopCheckpoint, result: ToolResult) -> None:
        payload = result.model_dump(mode="json")
        if result.error is not None:
            payload["error"] = safe_error_context(result.error)
        if result.status is ToolCallStatus.FAILED:
            payload["output"] = safe_failure_output_context(result.output)
        payload["context_ref"] = build_tool_result_context(
            session_id=checkpoint.session_id,
            tool_call_id=result.tool_call_id,
            payload=payload,
            policy=self.policy,
        )
        self._append_message(
            checkpoint,
            {
                "role": "tool",
                "tool_call_id": result.tool_call_id,
                "content": payload,
            },
        )
        self.compaction.observe(checkpoint)

    def _append_message(self, checkpoint: LoopCheckpoint, message: Dict[str, Any]) -> None:
        checkpoint.messages.append(message)
        if self.session_store is None:
            return
        self.session_store.append_message(
            SessionMessage(
                message_id=f"{checkpoint.run_id}:message:{len(checkpoint.messages)}",
                session_id=checkpoint.session_id,
                run_id=checkpoint.run_id,
                role=message["role"],
                content=deepcopy(message.get("content")),
                tool_call_id=message.get("tool_call_id"),
                tool_calls=deepcopy(message.get("tool_calls", [])),
                metadata=deepcopy(message.get("metadata", {})),
            )
        )


def _provider_reasoning_metadata(metadata: Dict[str, Any] | None) -> Dict[str, str]:
    """筛出需跨轮回传、但绝不能公开的 Think 协议字段。"""

    reasoning_content = (metadata or {}).get(PROVIDER_REASONING_CONTENT_METADATA_KEY)
    return (
        {PROVIDER_REASONING_CONTENT_METADATA_KEY: reasoning_content}
        if isinstance(reasoning_content, str) and reasoning_content
        else {}
    )


def tool_result_payload(result: ToolResult) -> Dict[str, Any]:
    payload = {
        "tool_call_id": result.tool_call_id,
        "status": result.status.value,
        "output": result.output,
        "metadata": result.metadata,
    }
    if result.error is not None:
        payload["error"] = safe_error_context(result.error)
    if result.status is ToolCallStatus.FAILED:
        payload["output"] = safe_failure_output_context(result.output)
    if result.recovery is not None:
        payload["recovery"] = result.recovery.model_dump(mode="json")
    return payload
