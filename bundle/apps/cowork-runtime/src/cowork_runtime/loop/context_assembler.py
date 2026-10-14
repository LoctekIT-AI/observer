"""Session-aware, tool-pair-safe model context assembly."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
from typing import Any, Dict, List, Optional

from cowork_contracts import AssetResolution, ContextSnapshot
from cowork_runtime.contracts import ArtifactRef, PinnedConstraint, SessionMessage
from cowork_runtime.analysis import CrawlerDatasetAnalysisService
from cowork_runtime.persistence.session_store import SessionMessageStore
from cowork_runtime.persistence.asset_catalog import asset_context_message, ensure_legacy_asset_catalog
from cowork_runtime.context import ContextPolicy, DEFAULT_CONTEXT_POLICY

from .crawler_reuse import dataset_context, dataset_from_crawl_result, is_export_request, user_request_text

from .types import LoopInput


@dataclass(frozen=True)
class ContextAssembly:
    messages: List[Dict[str, Any]]
    metadata: Dict[str, Any] = field(default_factory=dict)
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)


class ContextAssembler:
    """Build a new Run context from durable Session lineage.

    The catalog snapshot is treated as opaque data here. KH-007 owns its
    schema and fingerprint; this layer only freezes a deep copy into the Run.
    """

    def __init__(
        self,
        store: Optional[SessionMessageStore] = None,
        *,
        policy: ContextPolicy = DEFAULT_CONTEXT_POLICY,
        max_messages: Optional[int] = None,
    ) -> None:
        self.store = store
        self.policy = policy
        self.max_messages = max_messages

    def assemble(self, loop_input: LoopInput) -> ContextAssembly:
        session_id = loop_input.session_id
        snapshot = loop_input.context_snapshot
        if snapshot is not None and snapshot.conversation_id != session_id:
            raise ValueError("context snapshot conversation does not match loop input")
        fetch_limit = self.max_messages
        if fetch_limit is None and self.policy.max_history_fetch_messages > 0:
            fetch_limit = self.policy.max_history_fetch_messages
        prior = self.store.list_messages(session_id, limit=fetch_limit) if self.store else []
        # A supplied snapshot owns task meaning. Legacy pinned constraints are
        # read only when no snapshot exists, so follow-ups cannot be silently
        # reclassified from mutable Session text or keyword heuristics.
        constraints = self.store.list_pinned_constraints(session_id) if self.store and snapshot is None else []
        artifacts = self.store.list_artifact_refs(session_id, limit=fetch_limit) if self.store else []
        crawler_datasets = self.store.list_crawler_datasets(session_id, limit=fetch_limit) if self.store else []
        catalog_assets = ensure_legacy_asset_catalog(self.store, session_id) if self.store else []
        if self.store and is_export_request(loop_input.user_message) and not crawler_datasets:
            recovered = self._recover_crawler_dataset(prior)
            if recovered is not None:
                self.store.save_crawler_dataset(session_id, recovered)
                crawler_datasets = [recovered]
        messages: List[Dict[str, Any]] = []
        diagnostics: List[Dict[str, Any]] = []

        if snapshot is not None:
            messages.append(_task_context_system_message(snapshot))

        selected_asset = _resolved_asset(loop_input.metadata.get("asset_resolution"))
        if selected_asset is not None:
            # 资产目录只暴露稳定 ID 和摘要。文件 URI、Dataset 正文和历史工具
            # 输出仍留在 Runtime，模型必须经受控能力按范围读取。
            messages.append(
                {
                    "role": "system",
                    "content": asset_context_message(selected_asset),
                    "metadata": {
                        "_runtime_asset_ref": {
                            "asset_id": selected_asset.asset_id,
                            "asset_version_id": selected_asset.asset_version_id,
                        }
                    },
                }
            )

        # 用户随本次请求上传的 Desktop 附件必须对模型可见（run_e9dc9c93：
        # 「帮我安装这个skill」+ zip 附件，模型因完全看不到附件而只能追问
        # GitHub 链接）。这里只投影目录里的稳定 asset_id/摘要事实，本地 URI
        # 仍留在 Runtime 私有 source 里；后续 turn 的续接继续走 asset
        # resolution 路径，不会重复出现同一份附件清单。
        #
        # 归属判定使用 intake 写入的 turn_attachment_asset_ids（本轮登记的
        # 事实），不能比较 provenance.source_run_id：资产版本按内容去重，
        # 同一附件重复上传会复用首次注册的版本行，其 source_run_id 停留在
        # 旧 Run（run_04230b98：同 zip 次日重传被误判为“非本轮附件”）。
        turn_attachment_ids = _turn_attachment_asset_ids(loop_input.metadata)
        resolved_asset_ids = {selected_asset.asset_id} if selected_asset is not None else set()
        attachment_assets = [
            asset
            for asset in catalog_assets
            if asset.kind == "attachment"
            and asset.state == "ready"
            and asset.asset_id in turn_attachment_ids
            and asset.asset_id not in resolved_asset_ids
        ][:16]
        if attachment_assets:
            messages.append(
                {
                    "role": "system",
                    "content": _attachment_context_message(attachment_assets),
                    "metadata": {
                        "_runtime_attachment_refs": [asset.asset_id for asset in attachment_assets]
                    },
                }
            )

        for constraint in constraints:
            messages.append({"role": "system", "content": constraint.content, "metadata": {"constraint_id": constraint.constraint_id}})

        index = 0
        while index < len(prior):
            item = prior[index]
            if item.role == "assistant" and item.tool_calls:
                result_end = index + 1
                while result_end < len(prior) and prior[result_end].role == "tool":
                    result_end += 1
                result_messages = prior[index + 1 : result_end]
                result_ids = {
                    result.tool_call_id
                    for result in result_messages
                    if result.tool_call_id
                }
                expected_ids = [
                    str(tool_call["id"])
                    for tool_call in item.tool_calls
                    if isinstance(tool_call, dict) and tool_call.get("id")
                ]
                # Provider tool-calling requires a receipt for every call in
                # the assistant message. A partial subset is not a valid
                # historical protocol block, so preserve its completed calls
                # only as diagnostics instead of re-emitting an invalid pair.
                batch_complete = bool(expected_ids) and set(expected_ids).issubset(result_ids)
                valid_calls = list(item.tool_calls) if batch_complete else []
                valid_ids = set(expected_ids) if batch_complete else set()
                for tool_call in item.tool_calls:
                    tool_call_id = str(tool_call.get("id") or "") if isinstance(tool_call, dict) else ""
                    if tool_call_id and tool_call_id not in valid_ids:
                        diagnostics.append(
                            {
                                "code": (
                                    "partial_tool_call_batch"
                                    if result_ids and not batch_complete
                                    else "dangling_tool_call"
                                ),
                                "message_id": item.message_id,
                                "tool_call_id": tool_call_id,
                            }
                        )

                assistant_message = self._message_to_model(item)
                if valid_calls:
                    assistant_message["tool_calls"] = deepcopy(valid_calls)
                else:
                    assistant_message.pop("tool_calls", None)
                if assistant_message.get("content") or valid_calls:
                    messages.append(assistant_message)

                for result in result_messages:
                    if result.tool_call_id and result.tool_call_id in valid_ids:
                        messages.append(self._message_to_model(result))
                    else:
                        diagnostics.append(
                            {
                                "code": "orphan_tool_result",
                                "message_id": result.message_id,
                                "tool_call_id": result.tool_call_id,
                            }
                        )
                index = result_end
                continue
            if item.role == "tool":
                diagnostics.append(
                    {
                        "code": "orphan_tool_result",
                        "message_id": item.message_id,
                        "tool_call_id": item.tool_call_id,
                    }
                )
            else:
                messages.append(self._message_to_model(item))
            index += 1

        current_message_id = f"{loop_input.run_id}:user"
        current = SessionMessage(
            message_id=current_message_id,
            session_id=session_id,
            run_id=loop_input.run_id,
            role="user",
            content=loop_input.user_message,
            metadata=deepcopy(loop_input.metadata),
        )
        if not any(item.message_id == current_message_id for item in prior):
            if self.store:
                self.store.append_message(current)
            messages.append(self._message_to_model(current))

        asset_decision = loop_input.metadata.get("session_asset_decision")
        asset_action = asset_decision.get("action") if isinstance(asset_decision, dict) else None
        asset_dataset_id = asset_decision.get("dataset_id") if isinstance(asset_decision, dict) else None
        reuse_for_analysis = asset_action == "reuse_dataset" and isinstance(asset_dataset_id, str)
        reuse_for_export = asset_action == "export_dataset" and isinstance(asset_dataset_id, str)
        reusable_dataset = dataset_context(
            crawler_datasets,
            profile="reviews",
            dataset_id=asset_dataset_id if (reuse_for_analysis or reuse_for_export) else None,
            purpose="analysis" if reuse_for_analysis else "export",
        ) if (reuse_for_analysis or reuse_for_export or is_export_request(loop_input.user_message)) else None
        if reusable_dataset is not None and reuse_for_analysis:
            quality = reusable_dataset["dataset"].get("quality") if isinstance(reusable_dataset.get("dataset"), dict) else None
            if isinstance(quality, dict) and quality.get("status") == "export_only":
                messages.append(
                    {
                        "role": "system",
                        "content": reusable_dataset["instruction"],
                        "metadata": {"_runtime_crawler_dataset": reusable_dataset["dataset"]},
                    }
                )
                metadata = {
                    "session_context": {
                        "session_id": session_id,
                        "message_count": len(messages),
                        "artifact_ids": [artifact.artifact_id for artifact in artifacts],
                        "asset_ids": [asset.asset_id for asset in catalog_assets],
                        "crawler_dataset_ids": [dataset.ref.dataset_id for dataset in crawler_datasets],
                        "constraint_ids": [constraint.constraint_id for constraint in constraints],
                    },
                    "crawler_dataset": reusable_dataset["dataset"],
                    "crawler_dataset_quality_gate": quality,
                }
                return ContextAssembly(messages=messages, metadata=metadata, diagnostics=diagnostics)
        if reusable_dataset is not None and reuse_for_analysis and self.store is not None:
            # 统计覆盖完整 session 数据集，模型只接收结果与有上限的样本，避免
            # 把长评论正文重新拼回对话上下文。
            aggregate = CrawlerDatasetAnalysisService().analyze(self.store, session_id)
            if aggregate is not None:
                reusable_dataset["instruction"] += (
                    "\nRUNTIME_DATASET_AGGREGATE="
                    + json.dumps(aggregate.result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                )
        if reusable_dataset is not None:
            messages.append(
                {
                    "role": "system",
                    "content": reusable_dataset["instruction"],
                    "metadata": {"_runtime_crawler_dataset": reusable_dataset["dataset"]},
                }
            )

        metadata: Dict[str, Any] = {
            "session_context": {
                "session_id": session_id,
                "message_count": len(messages),
                "artifact_ids": [artifact.artifact_id for artifact in artifacts],
                "asset_ids": [asset.asset_id for asset in catalog_assets],
                "crawler_dataset_ids": [dataset.ref.dataset_id for dataset in crawler_datasets],
                "constraint_ids": [constraint.constraint_id for constraint in constraints],
            },
        }
        if snapshot is not None:
            metadata["task_context"] = {
                "task_id": snapshot.task_id,
                "snapshot_id": snapshot.snapshot_id,
                "revision": snapshot.revision,
                "source": snapshot.source,
                "selection_reason": snapshot.selection_reason,
                "execution_context_id": snapshot.execution_context.execution_context_id,
            }
        if reusable_dataset is not None:
            metadata["crawler_dataset"] = reusable_dataset["dataset"]
        catalog_snapshot = loop_input.metadata.get("catalog_snapshot")
        if catalog_snapshot is not None:
            metadata["catalog_snapshot"] = deepcopy(catalog_snapshot)
        if diagnostics:
            metadata["context_diagnostics"] = diagnostics
        return ContextAssembly(messages=messages, metadata=metadata, diagnostics=diagnostics)

    @staticmethod
    def _message_to_model(message: SessionMessage) -> Dict[str, Any]:
        content = deepcopy(message.content)
        if message.role == "user" and isinstance(content, str):
            # 历史 SQLite 可能包含 ISSUE-144 前的 Desktop 附件后缀。它是
            # 本机私有传输数据，重放给模型前仍须剥离，不能成为路径授权。
            content = user_request_text(content)
        output: Dict[str, Any] = {
            "role": message.role,
            "content": content,
            # Internal lineage is used for deterministic digests and never
            # forwarded as a provider-visible field by model_client.
            "_runtime": {
                "message_id": message.message_id,
                "session_id": message.session_id,
                "run_id": message.run_id,
            },
        }
        if message.tool_call_id:
            output["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            output["tool_calls"] = deepcopy(message.tool_calls)
        if message.metadata:
            output["metadata"] = deepcopy(message.metadata)
        return output

    @staticmethod
    def _recover_crawler_dataset(messages: List[SessionMessage]):
        """兼容修复发布前的 Session：从已持久化 tool result 重建一次数据集。"""

        for message in reversed(messages):
            if message.role != "tool" or not isinstance(message.content, dict):
                continue
            payload = message.content
            output = payload.get("output")
            if not isinstance(output, dict):
                continue
            source_url = next(
                (
                    str(ref.get("url"))
                    for ref in output.get("evidence_refs", [])
                    if isinstance(ref, dict) and isinstance(ref.get("url"), str)
                    and str(ref.get("url")).startswith(("http://", "https://"))
                ),
                None,
            )
            dataset = dataset_from_crawl_result(
                run_id=message.run_id,
                source_url=source_url,
                result=output,
            )
            if dataset is not None:
                return dataset
        return None


def _resolved_asset(value: object):
    """Parse persisted resolution metadata; malformed values never select a path."""

    if not isinstance(value, dict):
        return None
    try:
        resolution = AssetResolution.model_validate(value)
    except (TypeError, ValueError):
        return None
    return resolution.asset if resolution.status == "resolved" else None


def _turn_attachment_asset_ids(metadata: Dict[str, Any]) -> set[str]:
    """Read the Runtime-private intake key listing this turn's attachments."""

    raw = metadata.get("turn_attachment_asset_ids")
    if not isinstance(raw, list):
        return set()
    return {
        item.strip()
        for item in raw
        if isinstance(item, str) and item.strip()
    }


def _attachment_context_message(assets: List[Any]) -> str:
    """Provider-safe projection of this turn's Desktop attachments.

    只含稳定 ID 与摘要事实；本地路径留在 Runtime 私有 source 中，模型对
    附件内容的任何读取都必须经受控工具按 asset_id 解析。
    """

    payload = [
        {
            "asset_id": asset.asset_id,
            "asset_version_id": asset.asset_version_id,
            "title": asset.title,
            "content_type": asset.content_type,
            "size_bytes": asset.size_bytes,
            "sha256": asset.sha256,
            "capabilities": asset.capabilities,
        }
        for asset in assets
    ]
    return (
        "RUNTIME_TURN_ATTACHMENTS="
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\nThe user attached these files with the current request. They are real local "
        "assets already ingested by the Runtime, not missing uploads. Use their asset_id "
        "with the matching Runtime tool instead of asking the user to re-upload, re-state, "
        "or provide an external URL for them."
    )


def _task_context_system_message(snapshot: ContextSnapshot) -> Dict[str, Any]:
    """Project only the compiled task facts into the provider-visible context."""

    task = snapshot.task_envelope
    execution = snapshot.execution_context
    sections = [
        f"Active task goal: {task.active_goal}",
        (
            "Runtime execution context (authoritative; do not reinterpret): "
            f"target={execution.target}, surface={execution.surface}, "
            f"capability_status={execution.capability_status}"
            + (
                f", reason={execution.capability_reason_code}"
                if execution.capability_reason_code
                else ""
            )
        ),
    ]
    if task.confirmed_clarifications:
        sections.append("Confirmed clarifications:\n" + "\n".join(f"- {item}" for item in task.confirmed_clarifications))
    if task.confirmed_clarification_values:
        sections.append(
            "Confirmed clarification values:\n"
            + json.dumps(task.confirmed_clarification_values, ensure_ascii=False, sort_keys=True)
        )
    if task.pending_clarification is not None:
        # ISSUE-174：模型可见投影只含问题与选项摘要。完整内部 JSON（含
        # clarification_id/task_id/conversation_id）曾被模型原样回显给用户。
        pending = task.pending_clarification
        option_lines = "\n".join(
            f"- {option.display_index}. {option.label}"
            + (f"：{option.description}" if option.description else "")
            for option in sorted(pending.options, key=lambda item: item.display_index)
        )
        sections.append(
            "Pending clarification (Runtime-owned; answer by repeating the question and options, "
            "never by echoing internal identifiers):\n"
            f"Question: {pending.question}\n"
            f"Options:\n{option_lines}\n"
            "When the user's latest message answers this clarification in free-form text (not an "
            "option number), interpret it against the question and options using the conversation, "
            "record the resolution as a confirmed clarification, and continue the task. Only re-ask "
            "when the reply genuinely fails to resolve it."
        )
    if task.pinned_constraints:
        sections.append("Pinned constraints:\n" + "\n".join(f"- {item.content}" for item in task.pinned_constraints))
    if task.pending_deliverables:
        sections.append("Pending deliverables:\n" + "\n".join(f"- {item}" for item in task.pending_deliverables))
    module = task.module_constraints
    if module.development_mode == "modular":
        sections.append(
            "Modular development contract (Runtime-enforced):\n"
            f"- Change at least {module.minimum_changed_files} meaningful files with separate responsibilities.\n"
            "- Do not concentrate the implementation in one entry file; inspect the existing structure first.\n"
            "- Prefer existing component/composable/style directories when they are present.\n"
            "- Before claiming completion, verify the final changed-file set and tests/build."
        )
    return {
        "role": "system",
        "content": "Runtime task context (authoritative local snapshot):\n" + "\n\n".join(sections),
        "metadata": {
            "_runtime_task_context": {
                "task_id": snapshot.task_id,
                "revision": snapshot.revision,
                "selection_reason": snapshot.selection_reason,
                "execution_target": execution.target,
                "execution_surface": execution.surface,
                "capability_status": execution.capability_status,
            }
        },
    }
