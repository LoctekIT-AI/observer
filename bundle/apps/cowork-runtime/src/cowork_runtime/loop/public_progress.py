"""把工具调用与工具结果投影为安全、可验证的用户进度。

该模块只读取工具契约中的目标标识和已经发生的结果事实，不把原始参数、
脚本正文、完整路径或模型私有推理暴露给 Renderer。
"""

from __future__ import annotations

from pathlib import PurePosixPath
import re
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from cowork_runtime.contracts import ToolCall, ToolResult
from cowork_runtime.contracts.enums import ToolCallStatus


def _text(value: Any) -> str:
    return str(value or "").strip()


def _label_from_uri(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    parsed = urlsplit(raw)
    path = unquote(parsed.path) if parsed.scheme else raw
    return PurePosixPath(path.rstrip("/")).name[:160]


def _first_label(values: Any) -> str:
    if isinstance(values, list):
        return next((_label_from_uri(item) for item in values if _label_from_uri(item)), "")
    return ""


def _target(kind: str, label: str) -> dict[str, str] | None:
    return {"kind": kind, "label": label} if label else None


def _display(
    action: str,
    *,
    tool_name: str,
    target: dict[str, str] | None = None,
    command: str | None = None,
    result_summary: str | None = None,
    facts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": action,
        # 工具标识来自已创建的 ToolCall，而不是由模型说明文字推测。
        "tool_name": tool_name,
        "message_source": "tool",
    }
    if target:
        payload["target"] = target
    if command:
        payload["command"] = command
    if result_summary:
        payload["result_summary"] = result_summary
    if facts:
        payload["facts"] = dict(facts)
    return payload


_TOOL_LABELS = {
    "cowork.file.read_file": "读取文件",
    "cowork.file.find_name": "查找文件",
    "cowork.file.search_text": "搜索文件内容",
    "cowork.web.search": "搜索公开资料",
    "cowork.web.fetch": "查阅网页内容",
    "cowork.web.fetch_page": "查阅网页内容",
    "cowork.web.crawl_site": "整理网站资料",
    "cowork.research.market": "收集市场资料",
    "cowork.file.move": "移动文件",
    "cowork.skill.create": "创建 Skill 文件",
    "cowork.skill.install_from_url": "安装 Skill",
    "cowork.skill.load": "加载 Skill 指令",
    "cowork.script.run_python": "运行受控 Python 脚本",
    "cowork.script.run_node": "运行受控 Node 脚本",
    "cowork.script.run_bash": "运行沙箱 Bash 命令",
    "tool_cowork_terminal": "运行终端命令",
}


def _tool_label(name: str) -> str:
    # 该字段可能被调试或管理界面使用；未知名称不能原样落到用户可见事件。
    return _TOOL_LABELS.get(name, "处理任务")


def _safe_command_preview(value: Any) -> str:
    """公开 Bash/terminal 命令时移除常见凭证值，绝不输出完整脚本。"""

    command = _text(value).replace("\n", " ; ")[:500]
    if not command:
        return ""
    command = re.sub(
        r"(?i)(--?(?:token|password|secret|api[-_]?key|authorization)\s*(?:=|\s+)\s*)[^\s;]+",
        r"\1***",
        command,
    )
    command = re.sub(
        r"(?i)(\b(?:token|password|secret|api[-_]?key)\b\s*[=:]\s*)[^\s;]+",
        r"\1***",
        command,
    )
    command = re.sub(r"(?i)(authorization:\s*bearer\s+)[^\s;]+", r"\1***", command)
    return command


def _tool_display(call: ToolCall, action: str, **kwargs: Any) -> dict[str, Any]:
    command = None
    if call.tool_name in {"cowork.script.run_bash", "tool_cowork_terminal"}:
        command = _safe_command_preview(call.arguments.get("command") or call.arguments.get("code"))
    return _display(action, tool_name=_tool_label(call.tool_name), command=command, **kwargs)


def tool_started_progress(call: ToolCall) -> dict[str, Any]:
    """返回由工具调用事实生成的开始步骤，不声明尚未发生的结果。"""

    name = call.tool_name
    arguments = call.arguments
    if name == "cowork.file.read_file":
        label = _label_from_uri(arguments.get("uri"))
        return {
            "title": "读取文件",
            "content": f"正在读取《{label}》" if label else "正在读取已授权文件",
            "display": _tool_display(call, "read_file", target=_target("file", label)),
        }
    if name == "cowork.file.find_name":
        query = _text(arguments.get("pattern"))[:120]
        return {
            "title": "查找文件",
            "content": f"正在查找名称包含“{query}”的文件" if query else "正在查找相关文件",
            "display": _tool_display(call, "find_file", facts={"query": query} if query else None),
        }
    if name == "cowork.file.search_text":
        label = _label_from_uri(arguments.get("uri"))
        query = _text(arguments.get("query"))[:120]
        target_text = f"《{label}》" if label else "文件"
        return {
            "title": "搜索文件内容",
            "content": f"正在{target_text}中查找“{query}”" if query else f"正在搜索{target_text}内容",
            "display": _tool_display(call, "search_file", target=_target("file", label), facts={"query": query} if query else None),
        }
    if name == "cowork.web.search":
        query = _text(arguments.get("query"))[:160]
        return {
            "title": "搜索公开资料",
            "content": f"正在搜索“{query}”" if query else "正在搜索公开资料",
            "display": _tool_display(call, "web_search", target=_target("query", query)),
        }
    if name == "cowork.web.fetch":
        url = _text(arguments.get("url"))[:180]
        return {
            "title": "查阅网页内容",
            "content": f"正在读取 {url}" if url else "正在查阅网页内容",
            "display": _tool_display(call, "read_webpage", target=_target("url", url) if url else None),
        }
    if name == "cowork.web.fetch_page":
        return {
            "title": "查阅网页内容",
            "content": "正在查阅网页内容并提取相关资料",
            "display": _tool_display(call, "read_webpage"),
        }
    if name == "cowork.web.crawl_site":
        return {
            "title": "整理网站资料",
            "content": "正在整理网站中的相关公开资料",
            "display": _tool_display(call, "collect_webpages"),
        }
    if name == "cowork.research.market":
        return {
            "title": "收集市场资料",
            "content": "正在收集与任务相关的市场资料",
            "display": _tool_display(call, "market_research"),
        }
    if name.startswith("cowork.script.run_"):
        label = _first_label(arguments.get("output_uris"))
        generating = bool(label)
        return {
            "title": "生成文件" if generating else "分析数据",
            "content": f"正在生成《{label}》" if generating else "正在运行受控分析",
            "display": _tool_display(
                call,
                "create_artifact" if generating else "analyze_data",
                target=_target("file", label),
            ),
        }
    if name == "cowork.skill.create":
        skill_id = _text(arguments.get("skill_id"))
        return {
            "title": "生成 Skill 文件",
            "content": f"正在生成 Skill《{skill_id}》" if skill_id else "正在生成 Skill 文件",
            "display": _tool_display(call, "create_artifact", target=_target("file", f"{skill_id}.zip" if skill_id else "Skill 包")),
        }
    if name == "cowork.skill.install_from_url":
        url = _text(arguments.get("url"))
        return {
            "title": "安装 Skill",
            "content": f"正在从 {url} 安装 Skill" if url else "正在安装 Skill",
            "display": _tool_display(call, "download_resource", target=_target("url", url or "Skill 包")),
        }
    if name == "cowork.file.move":
        label = _first_label(arguments.get("source_uris"))
        return {
            "title": "移动文件",
            "content": f"正在移动《{label}》" if label else "正在移动已授权文件",
            "display": _tool_display(call, "desktop_action", target=_target("file", label)),
        }
    return {
        "title": "处理任务",
        "content": "正在完成必要的处理步骤",
        "display": _tool_display(call, "task_step"),
    }


def _result_error(result: ToolResult) -> str:
    if result.error is None:
        return "工具未返回可用结果"
    reason_code = result.error.details.get("reason_code") if isinstance(result.error.details, Mapping) else None
    crawler_messages = {
        "crawler.source.timeout": "目标网页响应过慢，正文读取超时；已跳过该来源",
        "crawler.source.network_error": "目标网页网络请求失败；已跳过该来源",
        "crawler.source.http_error": "目标网页未成功响应；已跳过该来源",
    }
    if isinstance(reason_code, str) and reason_code in crawler_messages:
        return crawler_messages[reason_code]
    return _text(result.error.message)[:240] or "工具执行失败"


def _artifact_labels(output: Any) -> list[str]:
    if not isinstance(output, Mapping) or not isinstance(output.get("artifacts"), list):
        return []
    labels: list[str] = []
    for item in output["artifacts"]:
        if not isinstance(item, Mapping):
            continue
        access = item.get("access") if isinstance(item.get("access"), Mapping) else {}
        label = _text(item.get("title")) or _label_from_uri(item.get("uri") or access.get("uri"))
        if label:
            labels.append(label[:160])
    return labels[:20]


def tool_completed_progress(call: ToolCall, result: ToolResult) -> dict[str, Any]:
    """仅依据真实 ToolResult 生成完成或失败文案。"""

    started = tool_started_progress(call)
    display = dict(started["display"])
    failed = result.status not in {ToolCallStatus.SUCCEEDED}
    if failed:
        content = f"{started['title']}失败：{_result_error(result)}"
        display["result_summary"] = content
        display["result"] = {"status": "failed"}
        return {"title": f"{started['title']}失败", "content": content, "status": "failed", "display": display}

    output = result.output if isinstance(result.output, Mapping) else {}
    name = call.tool_name
    if name == "cowork.file.read_file":
        label = _label_from_uri(call.arguments.get("uri"))
        snapshot = output.get("file_snapshot") if isinstance(output.get("file_snapshot"), Mapping) else {}
        summary = snapshot.get("summary") if isinstance(snapshot.get("summary"), Mapping) else {}
        sheet_count = summary.get("sheet_count")
        page_count = summary.get("page_count")
        facts: dict[str, Any] = {}
        if isinstance(sheet_count, int):
            facts["sheets"] = sheet_count
        if isinstance(page_count, int):
            facts["pages"] = page_count
        suffix = f"，包含 {sheet_count} 个工作表" if isinstance(sheet_count, int) else (
            f"，包含 {page_count} 页" if isinstance(page_count, int) else ""
        )
        content = f"已读取《{label}》{suffix}" if label else f"文件已读取{suffix}"
        display.update({"result_summary": content, "facts": facts, "result": {"status": "completed"}})
        return {"title": "文件已读取", "content": content, "status": "completed", "display": display}
    if name == "cowork.file.find_name":
        count = output.get("count")
        matches = output.get("matches") if isinstance(output.get("matches"), list) else []
        items = [_text(item.get("name"))[:120] for item in matches if isinstance(item, Mapping) and _text(item.get("name"))][:4]
        count_value = count if isinstance(count, int) else len(matches)
        suffix = f"：{'、'.join(items)}" if items else ""
        content = f"已找到 {count_value} 个相关文件{suffix}"
        display.update({"result_summary": content, "facts": {"count": count_value, "items": items}, "result": {"status": "completed"}})
        return {"title": "文件查找完成", "content": content, "status": "completed", "display": display}
    if name == "cowork.file.search_text":
        count = output.get("match_count")
        count_value = count if isinstance(count, int) else len(output.get("matches") or [])
        content = f"已找到 {count_value} 处相关内容"
        display.update({"result_summary": content, "facts": {"count": count_value}, "result": {"status": "completed"}})
        return {"title": "文件搜索完成", "content": content, "status": "completed", "display": display}
    if name == "cowork.web.search":
        results = output.get("results") if isinstance(output.get("results"), list) else []
        content = f"已找到 {len(results)} 条公开资料"
        facts = {"count": len(results)}
        if provider := _text(output.get("provider")):
            facts["provider"] = provider
        display.update({"result_summary": content, "facts": facts, "result": {"status": "completed"}})
        return {"title": "公开资料已找到", "content": content, "status": "completed", "display": display}
    if name == "cowork.web.fetch":
        text = _text(output.get("text"))
        content = "已读取网页正文" if text else "已查阅网页内容，但页面没有可提取正文"
        display.update({"result_summary": content, "facts": {"has_text": bool(text)}, "result": {"status": "completed"}})
        return {"title": "网页内容已读取", "content": content, "status": "completed", "display": display}
    if name in {"cowork.web.fetch_page", "cowork.web.crawl_site"}:
        pages = output.get("pages") if isinstance(output.get("pages"), list) else []
        page_count = output.get("page_count")
        count = page_count if isinstance(page_count, int) else len(pages)
        content = f"已整理 {count} 个网页中的公开资料" if count else "已查阅网页内容并整理相关资料"
        display.update({"result_summary": content, "facts": {"count": count} if count else None, "result": {"status": "completed"}})
        return {"title": "网页资料已整理", "content": content, "status": "completed", "display": display}
    if name == "cowork.research.market":
        content = "已收集市场资料，正在用于后续分析"
        display.update({"result_summary": content, "result": {"status": "completed"}})
        return {"title": "市场资料已收集", "content": content, "status": "completed", "display": display}
    artifacts = _artifact_labels(output)
    if artifacts:
        content = f"已生成《{artifacts[0]}》" if len(artifacts) == 1 else f"已生成 {len(artifacts)} 个文件"
        display.update({
            "action": "create_artifact",
            "target": _target("file", artifacts[0]),
            "result_summary": content,
            "facts": {"count": len(artifacts), "items": artifacts},
            "result": {"status": "completed", "verification_status": "passed"},
        })
        return {"title": "文件已生成", "content": content, "status": "completed", "display": display}
    content = "必要的处理步骤已完成"
    display.update({"result_summary": content, "result": {"status": "completed"}})
    return {"title": "处理步骤已完成", "content": content, "status": "completed", "display": display}


__all__ = ["tool_completed_progress", "tool_started_progress"]
