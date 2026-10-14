"""Approval and cancellation helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from cowork_runtime.contracts import Approval, RuntimeError, RuntimeErrorCode, RunStatus, ToolCall
from cowork_runtime.contracts.enums import ApprovalStatus
from cowork_runtime.contracts.ids import new_contract_id

from .types import LoopCheckpoint, LoopResume, PolicyDecision


def make_approval(
    run_id: str,
    call: ToolCall,
    decision: PolicyDecision,
    *,
    context_metadata: Mapping[str, Any] | None = None,
) -> Approval:
    payload: dict[str, Any] = {
        "tool_call_id": call.tool_call_id,
        "tool_name": call.tool_name,
        "arguments": call.arguments,
        "reason": decision.reason,
        "approval_kind": decision.approval_kind,
        # Renderer 依据该标记决定授权范围：非破坏性操作确认一次即可会话级
        # 免打扰；破坏性操作（delete/move/危险命令）保持逐次审批。
        "destructive": decision.destructive,
    }
    if call.tool_name == "cowork.skill.create":
        arguments = call.arguments if isinstance(call.arguments, Mapping) else {}
        skill_id = str(arguments.get("skill_id") or "").strip()
        raw_files = arguments.get("files")
        files: list[dict[str, Any]] = []
        if isinstance(raw_files, list):
            for item in raw_files[:32]:
                if not isinstance(item, Mapping):
                    continue
                path, content = item.get("path"), item.get("content")
                if isinstance(path, str) and isinstance(content, str):
                    files.append({"path": path, "size_bytes": len(content.encode("utf-8"))})
        metadata = context_metadata if isinstance(context_metadata, Mapping) else {}
        workspace_uri = str(metadata.get("workspace_uri") or "").strip()
        if not workspace_uri:
            root = str(metadata.get("workspace_root") or "").strip()
            if root:
                workspace_uri = Path(root).expanduser().resolve().as_uri()
        payload.update({
            "kind": "skill_authoring",
            "skill_id": skill_id,
            "target_directory": f"{workspace_uri.rstrip('/')}/.cowork/skills/{skill_id}" if workspace_uri else None,
            "files": files,
            "file_count": len(files),
            "permissions": ["file.write", "skill.authoring"],
            "status": "pending",
        })
    return Approval(
        approval_id=new_contract_id("approval"),
        run_id=run_id,
        status=ApprovalStatus.REQUESTED,
        kind=decision.approval_kind,
        resource_id=call.tool_call_id,
        payload=payload,
    )


def apply_denial(checkpoint: LoopCheckpoint, resume: LoopResume) -> RuntimeError:
    checkpoint.status = RunStatus.BLOCKED
    checkpoint.pending_tool_call = None
    checkpoint.pending_approval = None
    return RuntimeError(
        code=RuntimeErrorCode.POLICY_DENIED,
        message=resume.reason or "approval denied",
        retryable=False,
    )
