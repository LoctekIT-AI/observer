"""Pure, bounded detection of repeated tool attempts.

Only hashes and stable categories are retained.  The detector deliberately
does not inspect model reasoning or persist tool arguments.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, MutableMapping

from cowork_runtime.contracts import RuntimeError, ToolCall, ToolResult, digest_value
from cowork_runtime.contracts.enums import ToolCallStatus
from cowork_runtime.contracts.recovery import RecoveryGuidance


_STATE_VERSION = 1
_WINDOW = 8
_MAX_HISTORY = 32
_SENSITIVE_KEYS = frozenset({"token", "access_token", "authorization", "password", "secret", "code", "content", "stderr", "stdout"})


@dataclass(frozen=True)
class RecoveryObservation:
    guidance: RecoveryGuidance | None = None
    fingerprint: dict[str, Any] | None = None
    pattern: str | None = None


class LoopPatternDetector:
    """Record safe attempt fingerprints and escalate repeated no-progress work."""

    def __init__(self, *, nudge_threshold: int = 2, restrict_threshold: int = 3, terminate_threshold: int = 5) -> None:
        self.nudge_threshold = max(2, nudge_threshold)
        self.restrict_threshold = max(self.nudge_threshold + 1, restrict_threshold)
        self.terminate_threshold = max(self.restrict_threshold + 1, terminate_threshold)

    def observe(
        self,
        state: MutableMapping[str, Any],
        call: ToolCall,
        result: ToolResult,
        error: RuntimeError | None,
        *,
        category: str | None = None,
        allow_guidance: bool = True,
    ) -> RecoveryObservation:
        _initialize_state(state)
        sequence = int(state.get("sequence") or 0) + 1
        fingerprint = attempt_fingerprint(sequence, call, result, error, category=category)
        history = state["history"]
        history.append(fingerprint)
        del history[:-_MAX_HISTORY]
        if fingerprint["result_kind"] != "failed":
            state["sequence"] = sequence
            state["last_attempt_key"] = None
            state["last_attempt_count"] = 0
            state.pop("last_pattern", None)
            return RecoveryObservation(fingerprint=fingerprint)

        pattern, count, key, blocked_tool = self._pattern(state, fingerprint)
        if pattern is None or not allow_guidance:
            state["sequence"] = sequence
            state["last_attempt_key"] = key
            state["last_attempt_count"] = count
            return RecoveryObservation(fingerprint=fingerprint)

        stage = "nudge"
        if count >= self.terminate_threshold:
            stage = "terminate"
        elif count >= self.restrict_threshold:
            stage = "restrict"
        emitted = state["emitted_stages"].setdefault(key, [])
        if stage in emitted:
            guidance = None
        else:
            emitted.append(stage)
            guidance = self._guidance(
                call,
                error,
                category=category,
                stage=stage,
                count=count,
                pattern=pattern,
                blocked_tool=blocked_tool,
                call_signature=fingerprint["call_signature"],
            )
        if stage in {"restrict", "terminate"}:
            if blocked_tool:
                family = (
                    {item["tool_name"] for item in history[-4:]}
                    if pattern == "alternating_tools"
                    else {call.tool_name}
                )
                for tool_name in sorted(family):
                    if tool_name not in state["blocked_tools"]:
                        state["blocked_tools"].append(tool_name)
                    state["blocked_tool_reasons"].setdefault(
                        tool_name,
                        fingerprint["reason_code"],
                    )
            elif fingerprint["call_signature"] not in state["blocked_signatures"]:
                state["blocked_signatures"].append(fingerprint["call_signature"])
                state["blocked_signature_reasons"].setdefault(
                    fingerprint["call_signature"],
                    fingerprint["reason_code"],
                )
        state["sequence"] = sequence
        state["last_attempt_key"] = key
        state["last_attempt_count"] = count
        state["last_pattern"] = pattern
        state["last_failure_reason_code"] = fingerprint["reason_code"]
        return RecoveryObservation(guidance=guidance, fingerprint=fingerprint, pattern=pattern)

    def guard(self, state: MutableMapping[str, Any], call: ToolCall) -> RecoveryGuidance | None:
        """Return the persisted restriction that applies before Host execution."""

        _initialize_state(state)
        fingerprint = call_fingerprint(call)
        resource_key = _resource_block_key(call)
        if call.tool_name in state["blocked_tools"]:
            terminate = _record_blocked_attempt(state, f"tool:{call.tool_name}")
            return RecoveryGuidance(
                reason_code="loop.pattern.tool_family_blocked",
                action="stop" if terminate else "switch_tool",
                stage="terminate" if terminate else "restrict",
                instruction=_restricted_instruction(call.tool_name, terminate),
                suggested_tools=[],
                forbidden_call_signature=fingerprint["call_signature"],
                occurrence_count=max(1, int(state.get("last_attempt_count") or 1)),
            )
        if resource_key in state["blocked_resources"]:
            terminate = _record_blocked_attempt(state, f"resource:{resource_key}")
            return RecoveryGuidance(
                reason_code="loop.pattern.resource_blocked",
                action="stop" if terminate else "switch_tool",
                stage="terminate" if terminate else "restrict",
                instruction=_restricted_instruction(call.tool_name, terminate),
                suggested_tools=[],
                forbidden_call_signature=fingerprint["call_signature"],
                occurrence_count=max(1, int(state.get("last_attempt_count") or 1)),
            )
        if fingerprint["call_signature"] in state["blocked_signatures"]:
            terminate = _record_blocked_attempt(state, f"call:{fingerprint['call_signature']}")
            return RecoveryGuidance(
                reason_code="loop.pattern.call_signature_blocked",
                action="stop" if terminate else "switch_tool",
                stage="terminate" if terminate else "restrict",
                instruction=_restricted_instruction(call.tool_name, terminate),
                suggested_tools=[],
                forbidden_call_signature=fingerprint["call_signature"],
                occurrence_count=max(1, int(state.get("last_attempt_count") or 1)),
            )
        return None

    def block_tool(
        self,
        state: MutableMapping[str, Any],
        tool_name: str,
        *,
        reason_code: str,
    ) -> None:
        """Persist an immediate tool-family boundary before any replay can reach Host."""

        _initialize_state(state)
        if tool_name not in state["blocked_tools"]:
            state["blocked_tools"].append(tool_name)
        state.setdefault("blocked_tool_reasons", {})[tool_name] = reason_code
        state["last_failure_reason_code"] = reason_code

    def block_call(
        self,
        state: MutableMapping[str, Any],
        call: ToolCall,
        *,
        reason_code: str,
    ) -> None:
        """Block one deterministic call without disabling the entire tool family.

        A crawler robots denial or exhausted quality recovery applies to one
        source request. A family-wide ban would incorrectly reject a later,
        different authorized source.
        """

        _initialize_state(state)
        signature = call_fingerprint(call)["call_signature"]
        if signature not in state["blocked_signatures"]:
            state["blocked_signatures"].append(signature)
        state.setdefault("blocked_signature_reasons", {})[signature] = reason_code
        state["last_failure_reason_code"] = reason_code

    def block_resource(
        self,
        state: MutableMapping[str, Any],
        call: ToolCall,
        *,
        reason_code: str,
    ) -> None:
        """Block future calls against one resource without retaining its value."""

        _initialize_state(state)
        resource_key = _resource_block_key(call)
        if resource_key not in state["blocked_resources"]:
            state["blocked_resources"].append(resource_key)
        state.setdefault("blocked_resource_reasons", {})[resource_key] = reason_code
        state["last_failure_reason_code"] = reason_code

    def blocked_reason_for_call(
        self,
        state: MutableMapping[str, Any],
        call: ToolCall,
    ) -> str | None:
        """Return the stable reason that caused a pre-Host block, if known.

        Recovery guards must not replace a previously classified crawler or
        provider failure with the generic loop-policy wording.  Only the
        reason code is returned; call arguments and resource values remain
        outside the projection.
        """

        _initialize_state(state)
        resource_reason = state.get("blocked_resource_reasons", {}).get(_resource_block_key(call))
        if isinstance(resource_reason, str) and resource_reason:
            return resource_reason
        signature_reason = state.get("blocked_signature_reasons", {}).get(
            call_fingerprint(call)["call_signature"]
        )
        if isinstance(signature_reason, str) and signature_reason:
            return signature_reason
        tool_reason = state.get("blocked_tool_reasons", {}).get(call.tool_name)
        return tool_reason if isinstance(tool_reason, str) and tool_reason else None

    def _pattern(self, state: MutableMapping[str, Any], current: dict[str, Any]) -> tuple[str | None, int, str, bool]:
        history = state["history"]
        key = current["failure_key"]
        count = 1
        if state.get("last_attempt_key") == key:
            count = int(state.get("last_attempt_count") or 0) + 1
        if current["result_kind"] != "failed":
            return None, 0, key, False
        if count >= 2:
            return "same_call_failure", count, key, False

        recent = history[-_WINDOW:]
        if len(recent) >= 2 and all(item["result_kind"] == "failed" for item in recent[-2:]):
            same_tool = all(item["tool_name"] == current["tool_name"] for item in recent[-2:])
            same_resource = all(item["resource_hash"] == current["resource_hash"] for item in recent[-2:])
            same_reason = all(item["reason_code"] == current["reason_code"] for item in recent[-2:])
            has_progress = any(item["progress_marker"] != "none" for item in recent[-2:])
            changed_arguments = len({item["argument_hash"] for item in recent[-2:]}) > 1
            if same_tool and same_resource and same_reason and changed_arguments and not has_progress:
                matching = _trailing_matching_failures(
                    history,
                    tool_name=current["tool_name"],
                    resource_hash=current["resource_hash"],
                    reason_code=current["reason_code"],
                )
                if len({item["argument_hash"] for item in matching}) > 1:
                    return "parameter_guessing", len(matching), f"jitter:{current['tool_name']}:{current['resource_hash']}:{current['reason_code']}", True

            alternating = [item["tool_name"] for item in recent[-4:]]
            if len(recent) >= 4 and alternating[0] == alternating[2] and alternating[1] == alternating[3] and alternating[0] != alternating[1]:
                if all(item["progress_marker"] == "none" for item in recent[-4:]):
                    return "alternating_tools", 4, f"alternating:{alternating[0]}:{alternating[1]}", True
        return None, count, key, False

    def _guidance(
        self,
        call: ToolCall,
        error: RuntimeError | None,
        *,
        category: str | None,
        stage: str,
        count: int,
        pattern: str,
        blocked_tool: bool,
        call_signature: str,
    ) -> RecoveryGuidance:
        if stage == "terminate":
            action = "stop"
            instruction = (
                f"检测到工具调用死循环：'{call.tool_name}' 的无进展失败已达到 {count} 次。"
                "立即停止所有工具调用，跳出当前错误；只能诚实说明已尝试的内容，并明确请求人工帮助。"
            )
        elif stage == "restrict":
            action = "switch_tool"
            instruction = (
                f"检测到无进展循环：工具 '{call.tool_name}' 已连续 {count} 次走入同一失败路径。"
                "禁止继续猜测参数或重放该工具/工具族。必须彻底改变策略；若 Catalog 中没有安全替代，"
                "请等待用户补充信息。"
            )
        else:
            action = "replan"
            instruction = (
                f"你似乎陷入了死循环：工具 '{call.tool_name}' 已连续 {count} 次失败。"
                "停止无效重试，停止猜测参数，跳出当前局部思维并彻底改变策略；如果系统工具无法解决，"
                "直接结束任务并说明需要什么人工帮助。"
            )
        reason_code = f"loop.pattern.{pattern}"
        suggested = _suggested_tools(call.tool_name, error)
        return RecoveryGuidance(
            reason_code=reason_code,
            action=action,
            stage=stage,
            instruction=instruction,
            suggested_tools=suggested,
            forbidden_call_signature=call_signature,
            occurrence_count=count,
        )


def attempt_fingerprint(
    sequence: int,
    call: ToolCall,
    result: ToolResult,
    error: RuntimeError | None,
    *,
    category: str | None,
) -> dict[str, Any]:
    args = _safe_normalize(call.arguments)
    argument_hash = _hash(args)
    resource_hash = _resource_hash(call.arguments)
    reason = _reason_code(call.tool_name, error, category)
    progress = _progress_marker(call, result, error)
    call_signature = _hash({"tool_name": call.tool_name, "argument_hash": argument_hash})[:32]
    return {
        "sequence": sequence,
        "tool_name": call.tool_name,
        "argument_hash": argument_hash,
        "resource_hash": resource_hash,
        "result_kind": "failed" if result.status is ToolCallStatus.FAILED else "succeeded",
        "reason_code": reason,
        "progress_marker": progress,
        "call_signature": call_signature,
        "failure_key": _hash({"call_signature": call_signature, "result_kind": "failed", "reason_code": reason}),
    }


def call_fingerprint(call: ToolCall) -> dict[str, str]:
    argument_hash = _hash(_safe_normalize(call.arguments))
    return {
        "argument_hash": argument_hash,
        "call_signature": _hash({"tool_name": call.tool_name, "argument_hash": argument_hash})[:32],
    }


def _initialize_state(state: MutableMapping[str, Any]) -> None:
    state["version"] = _STATE_VERSION
    if not isinstance(state.get("sequence"), int):
        state["sequence"] = 0
    if not isinstance(state.get("history"), list):
        state["history"] = []
    if not isinstance(state.get("emitted_stages"), dict):
        state["emitted_stages"] = {}
    if not isinstance(state.get("blocked_signatures"), list):
        state["blocked_signatures"] = []
    if not isinstance(state.get("blocked_tools"), list):
        state["blocked_tools"] = []
    if not isinstance(state.get("blocked_resources"), list):
        state["blocked_resources"] = []
    if not isinstance(state.get("blocked_attempts"), dict):
        state["blocked_attempts"] = {}
    if not isinstance(state.get("blocked_tool_reasons"), dict):
        state["blocked_tool_reasons"] = {}
    if not isinstance(state.get("blocked_signature_reasons"), dict):
        state["blocked_signature_reasons"] = {}
    if not isinstance(state.get("blocked_resource_reasons"), dict):
        state["blocked_resource_reasons"] = {}


def _record_blocked_attempt(state: MutableMapping[str, Any], key: str) -> bool:
    attempts = state["blocked_attempts"]
    count = int(attempts.get(key) or 0) + 1
    attempts[key] = count
    if count >= 2:
        state["terminate"] = True
        return True
    return False


def _trailing_matching_failures(
    history: list[dict[str, Any]],
    *,
    tool_name: str,
    resource_hash: str,
    reason_code: str,
) -> list[dict[str, Any]]:
    matching: list[dict[str, Any]] = []
    for item in reversed(history):
        if (
            item.get("result_kind") != "failed"
            or item.get("tool_name") != tool_name
            or item.get("resource_hash") != resource_hash
            or item.get("reason_code") != reason_code
            or item.get("progress_marker") != "none"
        ):
            break
        matching.append(item)
    matching.reverse()
    return matching


def _safe_normalize(value: Any, *, key: str = "") -> Any:
    if key in _SENSITIVE_KEYS:
        return {"omitted": True, "digest": digest_value(value)}
    if isinstance(value, dict):
        return {str(item_key): _safe_normalize(item_value, key=str(item_key).lower()) for item_key, item_value in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, list):
        return [_safe_normalize(item, key=key) for item in value]
    if isinstance(value, tuple):
        return [_safe_normalize(item, key=key) for item in value]
    if isinstance(value, str):
        return value[:2_000] if len(value) <= 2_000 else {"length": len(value), "digest": digest_value(value)}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return {"type": type(value).__name__, "digest": digest_value(repr(value))}


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _resource_hash(arguments: dict[str, Any]) -> str:
    for key in ("uri", "path", "url", "source_uri", "target_uri", "workspace_uri"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return _hash({"key": key, "value": value})[:32]
    return "none"


def _resource_block_key(call: ToolCall) -> str:
    return f"{call.tool_name}:{_resource_hash(call.arguments)}"


def _progress_marker(call: ToolCall, result: ToolResult, error: RuntimeError | None) -> str:
    for source in (result.metadata, error.details if error is not None else {}, call.arguments):
        if not isinstance(source, dict):
            continue
        for key in ("progress_marker", "cursor", "page", "offset", "range_start", "range_end", "start", "end"):
            if key in source and source[key] is not None:
                return _hash({key: _safe_normalize(source[key])})[:32]
    return "none"


def _reason_code(tool_name: str, error: RuntimeError | None, category: str | None) -> str:
    if error is None:
        return "tool.success"
    requested = error.details.get("reason_code") if isinstance(error.details, dict) else None
    if isinstance(requested, str) and requested and len(requested) <= 100:
        return requested
    message = error.message.casefold()
    if "permission" in message or "denied" in message:
        return "tool.permission_denied"
    if "not found" in message or "does not exist" in message or "no such file" in message:
        return "tool.resource_not_found"
    return f"tool.{category or error.category}.{error.code.value}"


def _suggested_tools(tool_name: str, error: RuntimeError | None) -> list[str]:
    reason = _reason_code(tool_name, error, None)
    if tool_name.endswith("edit_file") or "old_text" in reason or "ambiguous" in reason:
        return ["cowork.file.read_file", "cowork.file.search_text"]
    if reason == "file.resource_exists":
        return ["cowork.file.read_file", "cowork.file.edit_file", "cowork.file.list_dir"]
    if reason == "tool.resource_not_found" and tool_name.startswith("cowork.file."):
        return ["cowork.file.list_dir", "cowork.file.find_name"]
    return []


def _restricted_instruction(tool_name: str, terminate: bool) -> str:
    if terminate:
        return "Runtime 已终止工具恢复阶段。禁止继续调用工具，只能诚实收官并说明需要人工帮助。"
    return f"Runtime 已禁止再次调用 '{tool_name}' 的危险重复路径。请改用安全替代或等待用户补充信息。"
