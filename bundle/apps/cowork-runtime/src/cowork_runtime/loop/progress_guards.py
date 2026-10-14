"""Agent Loop 无进展保护策略。

本模块只负责识别重复成功读取、生成抑制结果，以及发放一次性的工具失败
收官轮。事件持久化和终态切换仍由 :mod:`agent_loop` 负责，避免策略模块
反向依赖 Loop 编排器。
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from typing import Any, MutableMapping
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.request import url2pathname

from cowork_contracts.crawler import CRAWLER_REVIEW_EVIDENCE_TOOLS, canonical_crawl_url

from cowork_runtime.contracts import RuntimeError, ToolCall, ToolResult, digest_value
from cowork_runtime.contracts.enums import ToolCallStatus


_READ_ONLY_PROGRESS_TOOLS = frozenset(
    {
        "cowork.file.find_name",
        "cowork.file.list_dir",
        "cowork.file.preview",
        "cowork.file.read_file",
        "cowork.file.read_range",
        "cowork.file.search_text",
        "cowork.file.stat",
}
)

_URI_READ_BUDGET_TOOLS = frozenset(
    {
        "cowork.file.preview",
        "cowork.file.read_file",
        "cowork.file.read_range",
        "cowork.file.stat",
    }
)

# 写入风暴按目标 URI 计数（而非完整参数签名）：模型对同一文件的"修正"
# 每次参数都不同（run_272787f5 连续 5 次重写 LoginPage.jsx，每次 sha256
# 都变），参数级去重拦不住；同一 URI 的无进展重写才是可判定的风暴形态。
_FILE_WRITE_TOOLS = frozenset(
    {
        "cowork.file.write_base64",
        "cowork.file.write_text",
    }
)
_SUCCESSFUL_WRITES_BY_URI_KEY = "successful_writes_by_uri"
_WRITE_LIMIT_NUDGES_KEY = "write_limit_nudges"

_MARKET_RESEARCH_TOOL = "cowork.research.market"
_WEB_SEARCH_TOOL = "cowork.web.search"
_WEB_SEARCH_CALL_SIGNATURES_KEY = "successful_web_search_call_signatures"
_CRAWLER_CALL_SIGNATURES_KEY = "successful_crawler_call_signatures"
_FILE_CHANGE_VERSIONS_KEY = "file_change_versions"
_FILE_MUTATION_TOOLS = frozenset(
    {
        "cowork.file.edit_file",
        "cowork.file.move",
        "cowork.file.write_base64",
        "cowork.file.write_text",
        "cowork.script.run_bash",
        "cowork.script.run_node",
        "cowork.script.run_python",
    }
)


@dataclass(frozen=True)
class ToolFailureReprieve:
    """一次性工具失败收官轮的控制信息。"""

    message: dict[str, Any]
    event_payload: dict[str, Any]


def successful_web_search_count(metadata: MutableMapping[str, Any], signature: str) -> int:
    """读取当前 Run 内同一搜索结果集的成功次数。"""

    raw = metadata.get("successful_web_search_signatures")
    if not isinstance(raw, dict):
        return 0
    return int(raw.get(signature) or 0)


def web_search_call_signature(call: ToolCall) -> str | None:
    """返回一次网页搜索请求的稳定签名。

    结果集签名只能在 ToolHost 执行后得知，无法阻止同一请求再次触发网络调用。
    因此把请求参数单独记账：完全相同的成功搜索在当前 Run 内只应执行一次。
    """

    if call.tool_name != _WEB_SEARCH_TOOL:
        return None
    return digest_value({"tool_name": call.tool_name, "arguments": call.arguments})


def crawler_call_signature(call: ToolCall) -> str | None:
    """Return a stable signature for one crawler request.

    ``request_id`` is a transport correlation id and changes on every model
    retry. It must not make an otherwise identical network read look new. The
    remaining arguments describe the requested collection, so an explicit
    continuation with a different target remains a separate operation.
    """

    if call.tool_name not in {"cowork.web.fetch_page", "cowork.web.crawl_site", *CRAWLER_REVIEW_EVIDENCE_TOOLS}:
        return None
    arguments = dict(call.arguments)
    arguments.pop("request_id", None)
    arguments.pop("session_ref", None)
    raw_url = arguments.get("url")
    if isinstance(raw_url, str):
        try:
            arguments["url"] = canonical_crawl_url(raw_url)
        except ValueError:
            pass
    return digest_value({"tool_name": call.tool_name, "arguments": arguments})


def successful_crawler_call_count(metadata: MutableMapping[str, Any], signature: str) -> int:
    """Read successful crawler requests for the current Run."""

    raw = metadata.get(_CRAWLER_CALL_SIGNATURES_KEY)
    if not isinstance(raw, dict):
        return 0
    return int(raw.get(signature) or 0)


def record_successful_crawler_call(metadata: MutableMapping[str, Any], signature: str) -> None:
    """Persist a crawler request only after the network Host succeeded."""

    raw = metadata.get(_CRAWLER_CALL_SIGNATURES_KEY)
    if not isinstance(raw, dict):
        raw = {}
        metadata[_CRAWLER_CALL_SIGNATURES_KEY] = raw
    raw[signature] = int(raw.get(signature) or 0) + 1


def successful_web_search_call_count(metadata: MutableMapping[str, Any], signature: str) -> int:
    """读取当前 Run 内同一网页搜索请求的成功次数。"""

    raw = metadata.get(_WEB_SEARCH_CALL_SIGNATURES_KEY)
    if not isinstance(raw, dict):
        return 0
    return int(raw.get(signature) or 0)


def record_successful_web_search_call(metadata: MutableMapping[str, Any], signature: str) -> None:
    """只在 ToolHost 成功后记录网页搜索请求，失败请求仍可按恢复策略重试。"""

    raw = metadata.get(_WEB_SEARCH_CALL_SIGNATURES_KEY)
    if not isinstance(raw, dict):
        raw = {}
        metadata[_WEB_SEARCH_CALL_SIGNATURES_KEY] = raw
    raw[signature] = int(raw.get(signature) or 0) + 1


def successful_web_search_total(metadata: MutableMapping[str, Any]) -> int:
    """读取当前 Run 已成功执行的搜索总次数。"""

    return int(metadata.get("successful_web_search_total") or 0)


def record_successful_web_search_total(
    metadata: MutableMapping[str, Any],
    *,
    limit: int,
) -> dict[str, Any] | None:
    """在不同 query 也耗尽预算时，要求模型停止搜索并交付。"""

    if limit <= 0:
        return None
    count = successful_web_search_total(metadata) + 1
    metadata["successful_web_search_total"] = count
    if count != limit:
        return None
    return {
        "role": "system",
        "content": (
            "RUNTIME_WEB_SEARCH_TOTAL_BUDGET: The bounded web search budget for this run is complete. "
            "Do not call cowork.web.search again. Use the fetched page content and evidence already "
            "in context to answer now. If the evidence is insufficient, state the limitation clearly "
            "instead of issuing more query variants."
        ),
        "metadata": {"_runtime_web_search_total_budget": True},
    }


def suppressed_excessive_web_search_result(call: ToolCall) -> ToolResult:
    """闭合额度外的搜索调用，避免模型绕过目录继续搜索。"""

    return ToolResult(
        tool_call_id=call.tool_call_id,
        run_id=call.run_id,
        status=ToolCallStatus.SUCCEEDED,
        output={
            "status": "suppressed_web_search_budget",
            "diagnostics": [
                "The web search budget is exhausted. Use fetched page content and existing evidence to answer."
            ],
        },
        metadata={"no_progress_guard": True, "web_search_budget": True},
    )


def record_successful_web_search(
    metadata: MutableMapping[str, Any],
    *,
    signature: str,
    threshold: int,
) -> dict[str, Any] | None:
    """记录成功搜索，并在重复同一结果集时要求模型停止风暴。"""

    if threshold <= 0:
        return None
    raw = metadata.get("successful_web_search_signatures")
    if not isinstance(raw, dict):
        raw = {}
        metadata["successful_web_search_signatures"] = raw
    count = int(raw.get(signature) or 0) + 1
    raw[signature] = count
    if count < threshold:
        return None
    return {
        "role": "system",
        "content": (
            "RUNTIME_WEB_SEARCH_BUDGET: The same web search result bundle has already been returned. "
            "Do not repeat the search. Use the existing URLs and fetch the best candidates with "
            "cowork.web.fetch_page, then answer from the page content already in context."
        ),
        "metadata": {"_runtime_web_search_budget": True},
    }


def read_only_call_signature(
    call: ToolCall,
    metadata: MutableMapping[str, Any] | None = None,
) -> str | None:
    """返回包含资源版本的稳定签名，写操作不参与无进展判定。

    相同参数只有在目标文件没有被 Runtime 成功修改时才属于重复读取。
    """

    if call.tool_name not in _READ_ONLY_PROGRESS_TOOLS:
        return None
    versions = {
        uri: _file_change_version(metadata, uri)
        for uri in _observed_uris(call)
    }
    return digest_value(
        {
            "tool_name": call.tool_name,
            "arguments": call.arguments,
            "file_change_versions": versions,
        }
    )


def successful_read_count(metadata: MutableMapping[str, Any], signature: str) -> int:
    """读取当前 Run 内同一只读调用的成功次数。"""

    raw = metadata.get("successful_read_signatures")
    if not isinstance(raw, dict):
        return 0
    return int(raw.get(signature) or 0)


def successful_uri_read_count(metadata: MutableMapping[str, Any], call: ToolCall) -> int:
    """返回同一文件 URI 已成功执行的高体积读取次数。"""

    uri = _budgeted_read_uri(call)
    if uri is None:
        return 0
    raw = metadata.get(_uri_counter_key(call))
    if not isinstance(raw, dict):
        return 0
    return int(raw.get(uri) or 0)


def uri_read_budget_status(metadata: MutableMapping[str, Any], call: ToolCall, limit: int) -> str:
    """Return a diagnostic class without conflating it with permission denial."""

    count = successful_uri_read_count(metadata, call)
    if count >= max(1, limit):
        return "read_budget_suppressed"
    signature = read_only_call_signature(call, metadata)
    if signature is not None and successful_read_count(metadata, signature) > 0:
        return "duplicate_call_suppressed"
    return "allowed"


def successful_research_count(metadata: MutableMapping[str, Any]) -> int:
    """返回当前 Run 已完成的远程市场调研次数。"""

    return int(metadata.get("successful_market_research_count") or 0)


def successful_read_only_script_count(metadata: MutableMapping[str, Any]) -> int:
    """返回当前 Run 已成功完成的只读 Python 脚本数量。"""

    return int(metadata.get("successful_read_only_script_count") or 0)


def record_successful_read(
    metadata: MutableMapping[str, Any],
    call: ToolCall,
    *,
    threshold: int,
    uri_limit: int,
) -> dict[str, Any] | None:
    """记录成功读取，并在达到阈值时返回一次性推进指令。"""

    signature = read_only_call_signature(call, metadata)
    if signature is None:
        return None
    raw = metadata.get("successful_read_signatures")
    if not isinstance(raw, dict):
        raw = {}
        metadata["successful_read_signatures"] = raw
    count = int(raw.get(signature) or 0) + 1
    raw[signature] = count
    uri_instruction = None
    uri = _budgeted_read_uri(call)
    if uri is not None:
        counter_key = _uri_counter_key(call)
        by_uri = metadata.get(counter_key)
        if not isinstance(by_uri, dict):
            by_uri = {}
            metadata[counter_key] = by_uri
        by_uri[uri] = int(by_uri.get(uri) or 0) + 1
        if int(by_uri[uri]) >= uri_limit:
            uri_instruction = excessive_uri_read_instruction(metadata, call)
    if uri_instruction is not None:
        return uri_instruction
    if count < threshold:
        return None
    return no_progress_instruction(metadata, signature, call.tool_name)


def record_successful_file_mutations(
    metadata: MutableMapping[str, Any],
    call: ToolCall,
    result: ToolResult,
) -> None:
    """在文件成功变化后推进相关 URI 的版本。

    版本只保存在当前 Run 的 checkpoint 中，不改变文件契约或持久化边界。
    文件本身及其父目录都会推进，使 read_file 与 list/find 等目录观察在修改
    后都能重新执行；未变化文件的相同读取仍会被抑制。
    """

    changed_uris = _changed_uris(call, result)
    if not changed_uris:
        return
    changed_files = metadata.get("changed_file_uris")
    if not isinstance(changed_files, list):
        changed_files = []
        metadata["changed_file_uris"] = changed_files
    for uri in sorted(changed_uris):
        if uri not in changed_files and len(changed_files) < 64:
            changed_files.append(uri)
    raw = metadata.get(_FILE_CHANGE_VERSIONS_KEY)
    if not isinstance(raw, dict):
        raw = {}
        metadata[_FILE_CHANGE_VERSIONS_KEY] = raw
    affected: set[str] = set()
    for uri in changed_uris:
        affected.update(_uri_with_ancestors(uri))
    for uri in affected:
        raw[uri] = int(raw.get(uri) or 0) + 1

    # 单 URI 读取预算同样只描述当前文件版本。文件成功变化后清理关联计数，
    # 否则合法的写后验证会被旧版本已经消耗的额度误拦截。
    for counter_key in ("successful_reads_by_uri", "successful_searches_by_uri"):
        by_uri = metadata.get(counter_key)
        if not isinstance(by_uri, dict):
            continue
        for uri in affected:
            by_uri.pop(uri, None)


def record_successful_research(
    metadata: MutableMapping[str, Any],
    *,
    limit: int,
) -> dict[str, Any] | None:
    """记录已完成的远程调研，并在额度用尽时要求模型进入交付阶段。"""

    if limit <= 0:
        return None
    count = successful_research_count(metadata) + 1
    metadata["successful_market_research_count"] = count
    if count != limit:
        return None
    return {
        "role": "system",
        "content": (
            "RUNTIME_RESEARCH_BUDGET: The allowed source-grounded market research calls for this run "
            "are complete. Do not request more market research. Use the sources already returned to "
            "answer the user now. If the user requested a report or another artifact, create that "
            "deliverable in the authorized workspace before responding."
        ),
        "metadata": {"_runtime_research_budget": True},
    }


def record_successful_read_only_script(
    metadata: MutableMapping[str, Any],
    *,
    limit: int | None,
) -> dict[str, Any] | None:
    """记录 Artifact 任务中的只读脚本，并在额度用尽后强制进入写入阶段。"""

    if limit is None or limit <= 0:
        return None
    count = successful_read_only_script_count(metadata) + 1
    metadata["successful_read_only_script_count"] = count
    if count != limit:
        return None
    return {
        "role": "system",
        "content": (
            "RUNTIME_ARTIFACT_PROGRESS: Required inputs have been inspected. Do not run another "
            "read-only Python script with output_uris=[]. Create the requested new versioned artifact "
            "now with cowork.script.run_python. Include the authorized workspace_uri and a non-empty "
            "output_uris list containing the exact new file:// URI."
        ),
        "metadata": {"_runtime_artifact_progress": True},
    }


def no_progress_instruction(
    metadata: MutableMapping[str, Any],
    signature: str,
    tool_name: str,
) -> dict[str, Any] | None:
    """为同一重复读取生成至多一次的模型控制指令。"""

    nudged = metadata.get("no_progress_nudges")
    if not isinstance(nudged, list):
        nudged = []
        metadata["no_progress_nudges"] = nudged
    if signature in nudged:
        return None
    nudged.append(signature)
    return {
        "role": "system",
        "content": (
            "RUNTIME_NO_PROGRESS_GUARD: The identical read-only call "
            f"{tool_name} already succeeded. Do not call it again. Use the existing result and "
            "advance the user's current request now. A new read is only appropriate after a successful "
            "write or move changes the referenced file. If this turn requires a revised artifact, create "
            "a new versioned output file rather than rereading the unchanged input."
        ),
        "metadata": {"_runtime_no_progress_guard": True},
    }


def suppressed_duplicate_result(call: ToolCall) -> ToolResult:
    """构造闭合 provider tool-call 对的轻量成功结果。"""

    return ToolResult(
        tool_call_id=call.tool_call_id,
        run_id=call.run_id,
        status=ToolCallStatus.SUCCEEDED,
        output={
            "status": "suppressed_duplicate",
            "diagnostics": [
                "This identical read-only call already succeeded. Use its existing result and continue the task."
            ],
        },
        metadata={"no_progress_guard": True},
    )


def excessive_uri_read_instruction(
    metadata: MutableMapping[str, Any],
    call: ToolCall,
) -> dict[str, Any] | None:
    """为同一 URI 的读取风暴生成一次确定性推进指令。"""

    uri = _budgeted_read_uri(call)
    if uri is None:
        return None
    nudged = metadata.get("uri_read_limit_nudges")
    if not isinstance(nudged, list):
        nudged = []
        metadata["uri_read_limit_nudges"] = nudged
    nudge_key = f"{_uri_counter_key(call)}:{uri}"
    if nudge_key in nudged:
        return None
    nudged.append(nudge_key)
    if call.tool_name == "cowork.file.search_text":
        content = (
            "RUNTIME_NO_PROGRESS_GUARD: Enough targeted snippets have already been returned for "
            "this file. Do not search or reread it again. Use the existing snippets and advance "
            "directly to edit/create the requested versioned artifact now."
        )
    else:
        content = (
            "RUNTIME_NO_PROGRESS_GUARD: Enough content has already been read from this file. "
            "Do not call read_file, preview, read_range, or stat on this URI again. Use "
            "cowork.file.search_text to locate a specific snippet, use a read-only script for "
            "structured extraction, or advance directly to edit/create the requested versioned "
            "artifact now."
        )
    return {
        "role": "system",
        "content": content,
        "metadata": {"_runtime_uri_read_limit": True},
    }


def suppressed_excessive_uri_read_result(call: ToolCall) -> ToolResult:
    """闭合超过单 URI 预算的调用，不再重复读取或扩大上下文。"""

    return ToolResult(
        tool_call_id=call.tool_call_id,
        run_id=call.run_id,
        status=ToolCallStatus.SUCCEEDED,
        output={
            "status": "suppressed_read_uri_budget",
            "diagnostics": [
                "Enough content from this URI is already in context. Use search_text, a read-only script, or create the requested artifact now."
            ],
        },
        metadata={"no_progress_guard": True, "read_uri_budget": True},
    )


def suppressed_excessive_research_result(call: ToolCall) -> ToolResult:
    """闭合额度外的研究调用，避免再次占用远程研究服务。"""

    return ToolResult(
        tool_call_id=call.tool_call_id,
        run_id=call.run_id,
        status=ToolCallStatus.SUCCEEDED,
        output={
            "status": "suppressed_research_budget",
            "diagnostics": [
                "The market research budget is exhausted. Use the existing source-grounded results and create the requested deliverable now."
            ],
        },
        metadata={"no_progress_guard": True, "research_budget": True},
    )


def suppressed_excessive_read_only_script_result(call: ToolCall) -> ToolResult:
    """闭合超过额度的检查脚本，同时保留声明新输出的写入脚本通道。"""

    return ToolResult(
        tool_call_id=call.tool_call_id,
        run_id=call.run_id,
        status=ToolCallStatus.SUCCEEDED,
        output={
            "status": "suppressed_read_only_script_budget",
            "diagnostics": [
                "The artifact task has enough inspected evidence. Declare a new versioned output in output_uris and create it now."
            ],
        },
        metadata={"no_progress_guard": True, "read_only_script_budget": True},
    )


def write_target_uri(call: ToolCall) -> str | None:
    """返回写入调用的目标 URI；其它工具返回 None。"""

    if call.tool_name not in _FILE_WRITE_TOOLS:
        return None
    uri = call.arguments.get("uri")
    return uri if isinstance(uri, str) and uri else None


def successful_write_count(metadata: MutableMapping[str, Any], call: ToolCall) -> int:
    """返回同一 URI 已成功执行的写入次数。"""

    uri = write_target_uri(call)
    if uri is None:
        return 0
    raw = metadata.get(_SUCCESSFUL_WRITES_BY_URI_KEY)
    if not isinstance(raw, dict):
        return 0
    return int(raw.get(uri) or 0)


def record_successful_write(
    metadata: MutableMapping[str, Any],
    call: ToolCall,
    *,
    limit: int,
) -> dict[str, Any] | None:
    """记录成功写入，并在同一 URI 达到限额时要求模型停止重写。

    合法的多轮编辑（写入→验证→修正）是 coding 主路径，限额必须显著高于
    读取预算；这里只拦截"同一文件反复整体重写而无外部新证据"的风暴形态。
    """

    uri = write_target_uri(call)
    if uri is None or limit <= 0:
        return None
    raw = metadata.get(_SUCCESSFUL_WRITES_BY_URI_KEY)
    if not isinstance(raw, dict):
        raw = {}
        metadata[_SUCCESSFUL_WRITES_BY_URI_KEY] = raw
    count = int(raw.get(uri) or 0) + 1
    raw[uri] = count
    if count < limit:
        return None
    nudged = metadata.get(_WRITE_LIMIT_NUDGES_KEY)
    if not isinstance(nudged, list):
        nudged = []
        metadata[_WRITE_LIMIT_NUDGES_KEY] = nudged
    if uri in nudged:
        return None
    nudged.append(uri)
    return {
        "role": "system",
        "content": (
            "RUNTIME_WRITE_STORM_GUARD: The same target file has already been written successfully "
            f"{count} times in this run. The paired tool results confirm each write persisted with a "
            "real sha256 and byte size — historical write arguments are shown as omission placeholders "
            "in context, but the writes were NOT empty. Do not rewrite this file again from scratch. "
            "If something is wrong, use cowork.file.read_file once to inspect the current on-disk "
            "content, then make a targeted cowork.file.edit_file change or continue with the next task step."
        ),
        "metadata": {"_runtime_write_storm_guard": True, "uri": uri, "count": count},
    }


def suppressed_excessive_write_result(call: ToolCall) -> ToolResult:
    """闭合超过同一文件写入预算的调用，避免重写风暴烧穿 Run 预算。"""

    return ToolResult(
        tool_call_id=call.tool_call_id,
        run_id=call.run_id,
        status=ToolCallStatus.SUCCEEDED,
        output={
            "status": "suppressed_write_uri_budget",
            "diagnostics": [
                "This file has already been written successfully multiple times in this run; "
                "the on-disk content is the latest write. Read it once if you need to verify, "
                "then advance the task. Do not rewrite it from scratch."
            ],
        },
        metadata={"no_progress_guard": True, "write_uri_budget": True},
    )


def _budgeted_read_uri(call: ToolCall) -> str | None:
    if call.tool_name not in _URI_READ_BUDGET_TOOLS and call.tool_name != "cowork.file.search_text":
        return None
    uri = call.arguments.get("uri")
    return uri if isinstance(uri, str) and uri else None


def _observed_uris(call: ToolCall) -> list[str]:
    uri = call.arguments.get("uri")
    return [uri] if isinstance(uri, str) and uri else []


def _file_change_version(
    metadata: MutableMapping[str, Any] | None,
    uri: str,
) -> int:
    if metadata is None:
        return 0
    raw = metadata.get(_FILE_CHANGE_VERSIONS_KEY)
    if not isinstance(raw, dict):
        return 0
    return int(raw.get(uri) or 0)


def _changed_uris(call: ToolCall, result: ToolResult) -> set[str]:
    output = result.output if isinstance(result.output, dict) else {}
    uris: set[str] = set()
    if call.tool_name in _FILE_MUTATION_TOOLS:
        for key in ("uri", "target_uri", "output_uri", "target_directory_uri"):
            value = call.arguments.get(key)
            if isinstance(value, str) and value:
                uris.add(value)
        for key in ("source_uris", "output_uris"):
            values = call.arguments.get(key)
            if isinstance(values, list):
                uris.update(value for value in values if isinstance(value, str) and value)

    moved = output.get("moved")
    if isinstance(moved, list):
        for item in moved:
            if isinstance(item, dict) and isinstance(item.get("uri"), str) and item["uri"]:
                uris.add(item["uri"])

    artifacts = output.get("artifacts")
    if isinstance(artifacts, list):
        for item in artifacts:
            if not isinstance(item, dict):
                continue
            access = item.get("access")
            uri = access.get("uri") if isinstance(access, dict) else item.get("uri")
            if isinstance(uri, str) and uri:
                uris.add(uri)
    return uris


def _uri_with_ancestors(uri: str) -> set[str]:
    """返回 URI 本身和父目录，供文件与目录读取共享变更版本。"""

    values = {uri}
    parsed = urlsplit(uri)
    path = parsed.path if parsed.scheme else uri
    if not path:
        return values
    current = path.rstrip("/") or "/"
    while current not in {"", "/"}:
        parent = posixpath.dirname(current)
        if not parent or parent == current:
            break
        if parsed.scheme:
            values.add(urlunsplit((parsed.scheme, parsed.netloc, parent, "", "")))
        else:
            values.add(parent)
        current = parent
    return values


def _uri_counter_key(call: ToolCall) -> str:
    return (
        "successful_searches_by_uri"
        if call.tool_name == "cowork.file.search_text"
        else "successful_reads_by_uri"
    )


def grant_tool_failure_reprieve(
    metadata: MutableMapping[str, Any],
    *,
    tool_name: str,
    error_code: str,
    signature: str,
    trigger: str,
) -> ToolFailureReprieve | None:
    """发放一次只输出解释的收官轮，防止熔断直接产生空响应。"""

    if metadata.get("tool_storm_grace_armed") is True:
        return None
    metadata["tool_storm_grace_armed"] = True
    return ToolFailureReprieve(
        message={
            "role": "system",
            "content": (
                "RUNTIME_TOOL_STORM_GRACE: Tool execution has repeatedly failed and Runtime will not "
                "allow another repair loop. Do not call any tool. Respond once with a concise, truthful "
                "explanation of what could not be completed, what was attempted, and the concrete next "
                "step the user can take. Never claim the requested operation or artifact succeeded."
            ),
            "metadata": {"_runtime_tool_storm_grace": True},
        },
        event_payload={
            "stage": "tool_storm",
            "status": "grace_turn_armed",
            "tool_name": tool_name,
            "error_code": error_code,
            "signature": signature,
            "trigger": trigger,
        },
    )


def terminal_tool_failure_explanation(call: ToolCall, error: RuntimeError) -> str:
    """生成工具熔断后的确定性用户说明，不再依赖模型遵守收官指令。

    这里只翻译 Runtime 已确认的错误事实，不推断业务结果。文件不存在时附上
    已检查的本地路径，既满足可操作性，也避免把 URI 查询参数或远端凭证回显。
    """

    message = error.message.casefold()
    missing_file = call.tool_name.startswith("cowork.file.") and any(
        marker in message
        for marker in ("does not exist", "not found", "no such file", "不存在", "未找到")
    )
    if missing_file:
        location = _display_local_location(call.arguments)
        checked = f"\n已检查路径：`{location}`。" if location else ""
        return f"无法继续：文件不存在。{checked}\n请确认文件名或提供正确路径后重试。"

    reason_code = error.details.get("reason_code") if isinstance(error.details, dict) else None
    if call.tool_name.startswith("cowork.web.") and reason_code == "crawler.source.timeout":
        return "目标网页响应过慢，正文读取超时。Runtime 已停止重复读取该来源；请稍后重试或改用其它公开来源。"
    if call.tool_name.startswith("cowork.web.") and reason_code == "crawler.source.network_error":
        return "目标网页网络请求失败。Runtime 已停止重复读取该来源；请检查网络后重试或改用其它公开来源。"
    if call.tool_name.startswith("cowork.web.") and reason_code == "crawler.source.http_error":
        return "目标网页未成功响应。Runtime 已停止重复读取该来源；请稍后重试或改用其它公开来源。"
    if call.tool_name.startswith("cowork.web.") and reason_code == "crawler.provider.unavailable":
        return (
            "评论抓取未能取得可验证的页面证据，Runtime 已停止重试，避免重复访问或补造评论。"
            "这不等同于页面没有评论；请稍后重试，或检查页面是否需要登录、验证或其它人工操作。"
        )
    if call.tool_name.startswith("cowork.web.") and reason_code == "crawler.provider.rate_limited":
        return "目标页面暂时限制了抓取请求，Runtime 已停止重试。请稍后再试，不会绕过站点限制。"
    if call.tool_name.startswith("cowork.web.") and reason_code == "crawler.policy.robots_denied":
        return "目标页面明确禁止自动抓取，Runtime 已停止重试。不会绕过 robots 或站点访问策略。"

    tool_scope = "平台工具" if call.tool_name == _MARKET_RESEARCH_TOOL else "本地工具"
    return (
        f"操作未能完成：{tool_scope} `{call.tool_name}` 连续执行失败，Runtime 已停止重试以避免无限循环。"
        "请检查输入后重试；如果问题持续，请查看本次任务的结构化错误详情。"
    )


def _display_local_location(arguments: MutableMapping[str, Any]) -> str | None:
    """从工具参数提取可安全回显的本地路径，忽略 URI query/fragment。"""

    for key in ("uri", "path", "source_uri", "target_uri"):
        raw = arguments.get(key)
        if not isinstance(raw, str) or not raw:
            continue
        parsed = urlsplit(raw)
        if parsed.scheme == "file":
            if parsed.netloc not in {"", "localhost"}:
                continue
            value = url2pathname(unquote(parsed.path))
        elif not parsed.scheme and (raw.startswith("/") or raw.startswith("~/")):
            value = raw
        else:
            continue
        return value if len(value) <= 500 else f"…{value[-499:]}"
    return None
