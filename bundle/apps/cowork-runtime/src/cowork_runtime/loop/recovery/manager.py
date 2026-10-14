"""Recovery guidance selection and pre-Host enforcement."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, MutableMapping

from cowork_runtime.contracts import RuntimeError, ToolCall, ToolResult
from cowork_runtime.contracts.enums import RuntimeErrorCode
from cowork_runtime.contracts.recovery import RecoveryGuidance

from .detector import LoopPatternDetector, RecoveryObservation


@dataclass(frozen=True)
class RecoveryGuardDecision:
    guidance: RecoveryGuidance
    terminal: bool = False


class RecoveryManager:
    """Stateless facade around the detector and stable failure rules."""

    def __init__(self, detector: LoopPatternDetector | None = None) -> None:
        self.detector = detector or LoopPatternDetector()

    def observe(
        self,
        state: MutableMapping[str, Any],
        call: ToolCall,
        result: ToolResult,
        error: RuntimeError | None,
        *,
        category: str | None,
        allow_guidance: bool = True,
        available_tools: Iterable[str] | None = None,
    ) -> RecoveryObservation:
        observation = self.detector.observe(
            state,
            call,
            result,
            error,
            category=category,
            allow_guidance=allow_guidance,
        )
        if observation.guidance is None and error is not None and allow_guidance:
            observation = RecoveryObservation(
                guidance=self.failure_guidance(
                    call,
                    error,
                    category=category,
                    available_tools=available_tools,
                ),
                fingerprint=observation.fingerprint,
                pattern=observation.pattern,
            )
        if observation.guidance is not None and observation.guidance.stage == "terminate":
            state["terminate"] = True
        return observation

    def failure_guidance(
        self,
        call: ToolCall,
        error: RuntimeError,
        *,
        category: str | None,
        available_tools: Iterable[str] | None = None,
    ) -> RecoveryGuidance:
        reason = _stable_failure_reason(call.tool_name, error, category)
        do_not_retry = bool(error.details.get("do_not_retry")) if isinstance(error.details, dict) else False
        suggested = _filter_available_tools(
            [
                *_error_suggested_tools(error),
                *_suggested_tools(call.tool_name, reason),
            ],
            available_tools,
        )
        action = _failure_action(
            reason,
            do_not_retry=do_not_retry,
            error_code=error.code,
            suggested_tools=suggested,
        )
        if do_not_retry or error.code in {
            RuntimeErrorCode.POLICY_DENIED,
            RuntimeErrorCode.CANCELLED,
            RuntimeErrorCode.APPROVAL_EXPIRED,
        }:
            action = "wait_for_user"
        instruction = _failure_instruction(call.tool_name, reason, action)
        return RecoveryGuidance(
            reason_code=reason,
            action=action,
            stage="nudge",
            instruction=instruction,
            suggested_tools=suggested,
            do_not_retry=do_not_retry,
        )

    def block_tool(
        self,
        state: MutableMapping[str, Any],
        tool_name: str,
        *,
        reason_code: str,
    ) -> None:
        self.detector.block_tool(state, tool_name, reason_code=reason_code)

    def block_call(
        self,
        state: MutableMapping[str, Any],
        call: ToolCall,
        *,
        reason_code: str,
    ) -> None:
        self.detector.block_call(state, call, reason_code=reason_code)

    def block_resource(
        self,
        state: MutableMapping[str, Any],
        call: ToolCall,
        *,
        reason_code: str,
    ) -> None:
        self.detector.block_resource(state, call, reason_code=reason_code)

    def guard(self, state: MutableMapping[str, Any], call: ToolCall) -> RecoveryGuardDecision | None:
        guidance = self.detector.guard(state, call)
        if guidance is None:
            return None
        return RecoveryGuardDecision(guidance=guidance, terminal=guidance.stage == "terminate")

    def blocked_reason_for_call(self, state: MutableMapping[str, Any], call: ToolCall) -> str | None:
        """Expose only the stable reason behind a pre-Host recovery block."""

        return self.detector.blocked_reason_for_call(state, call)


def _stable_failure_reason(tool_name: str, error: RuntimeError, category: str | None) -> str:
    requested = error.details.get("reason_code") if isinstance(error.details, dict) else None
    if isinstance(requested, str) and requested and len(requested) <= 100:
        return requested
    message = error.message.casefold()
    if "permission" in message or "denied" in message:
        return "tool.permission_denied"
    if "not found" in message or "does not exist" in message or "no such file" in message:
        return "tool.resource_not_found"
    if "timeout" in message or "timed out" in message:
        return "tool.timeout"
    return f"tool.{category or error.category}.{error.code.value}"


def _failure_instruction(tool_name: str, reason: str, action: str) -> str:
    targeted = {
        "file.edit.old_text_not_found": "不要重放原 edit。先用 `cowork.file.read_file` 或 `cowork.file.search_text` 读取当前内容，选取唯一且完整的 old_text 后再编辑。",
        "file.edit.old_text_ambiguous": "不要重放原 edit。old_text 命中了多处；扩大上下相邻代码或文本上下文，确保替换片段唯一。",
        "file.resource_not_found": "不要猜测路径。只在已授权目录内用 `cowork.file.list_dir` 或 `cowork.file.find_name` 确认文件名和位置。",
        # write_text 本身支持 overwrite 参数；引导必须把这条最短修复路径
        # 告诉模型，否则模型会反复盲试同一写入并耗尽修复预算。
        "file.resource_exists": "目标文件已经存在。若要整文件替换，用 `cowork.file.write_text` 显式传 `overwrite: true` 重写；若要局部修改，先用 `cowork.file.read_file` 读取当前内容，再用 `cowork.file.edit_file` 精确编辑；不要不带 overwrite 重复写入同一路径。",
        "file.workspace.outside_authorized": "这是 Workspace Grant 边界。不要换字符串重试或绕过授权；只能改用已授权文件，或请求用户授予正确目录。",
        "file.workspace.read_only": "当前目录只读。不要尝试提权或覆盖；选择一个已授权的可写新输出位置，或请求用户处理权限。",
        "file.workspace.permission_denied": "这是受控权限边界。停止重试，不要绕过权限；使用已有授权范围或请求人工处理。",
        "file.integrity.sha256_mismatch": "文件已变化或期望版本不一致。先重新读取当前文件并获取新的 SHA-256，再基于当前版本重新规划。",
        "file.format.unsupported": "不要把不支持的格式当作文本反复读取。使用 Catalog 中可用的受支持读取能力，或向用户说明需要转换后的文件。",
        "file.size_limit_exceeded": "不要放大同一读取请求。改用范围读取、文本搜索或更小的受支持输入。",
        "script.uri.percent_encoded": "不要手工去掉 `file://` 前缀或猜路径。使用脚本 runner 注入的 URI 转换 helper 处理已授权 URI。",
        "script.output.declaration_missing": "声明的输出必须在脚本结束前以普通文件创建在 workspace 内。不要把用户产物留在临时目录，也不要把输入文件列为 output_uris。",
        "script.module.unavailable": "该运行时不提供所需模块。不要安装依赖或调用包管理器；改用现有内置能力或把小型逻辑写入脚本。",
        "script.syntax.invalid": "这是脚本语法错误。只修正代码语法和字符串/引号处理，再进行一次有界执行。",
        "script.timeout": "受控脚本已超时。不要转为后台进程；缩小输入、减少工作量，或使用当前 Catalog 中更专用的能力。",
        "script.bash.command_not_allowed": "Bash 命令不在部署拥有的 allow-list 中。不要改写成 shell 逃逸、安装命令或宿主机调用；改用可用文件工具或请求人工处理。",
        "script.bash.command_not_found": "受控 Bash sandbox 中没有该命令。不要安装命令或改走宿主机；仅使用当前 allow-list 和 Catalog 中已可用的能力。",
        "script.bash.syntax_not_allowed": "Bash 只接受受限的简单命令。移除控制符、管道、重定向外的复杂语法，或改用受控文件工具。",
        "provider.platform.unavailable": "Platform 调研能力当前不可用。不要重复调用或猜测认证参数；仅在当前 Catalog 中选择可用的实时信息能力。没有可用替代时，应明确说明无法取得新的外部资料。",
        "provider.platform.session_unavailable": "Platform 会话当前不可用。不要重试、重新登录或绕过认证；使用当前 Catalog 中已可用的公开信息能力，或请求用户在受控登录流程完成后重试。",
        "provider.search.unavailable": "所有受控搜索源当前不可用。不要反复换关键词碰撞同一 Provider；仅切换到当前 Catalog 中可用的独立研究能力。没有替代时，明确停止并说明无法获取实时来源。",
        "provider.search.no_results": "当前搜索没有得到可验证结果。不要重复相同查询；可以在当前 Catalog 中选择独立来源，或请用户补充准确的主体、时间范围或公开链接。",
        "provider.health.degraded": "Provider 正在降级。Runtime 只会在可信的只读幂等契约内完成有限重试；不要手工高频重放。若仍不可用，改用当前 Catalog 中的替代能力。",
        "provider.health.unavailable": "Provider 当前不可用。不要重试、安装、重启服务或绕过网络/认证边界；只使用当前 Catalog 中可用的替代能力。没有替代时，诚实说明需要人工处理。",
        "provider.tool.unavailable": "该 Provider 工具当前不可用。不要原样重放或设法绕过 Provider 边界；选择当前 Catalog 中的替代能力，或说明无法完成所需外部操作。",
        "provider.tool_arguments_invalid": "模型供应商返回的工具参数格式错误或缺少必填字段。不要重放相同空参数；根据 ToolResult 中的 missing_arguments 和当前 Catalog schema 重新生成一次完整参数。若仍无法生成合法参数，应停止并如实说明模型工具协议异常。",
        "crawler.interaction.login_required": "页面需要在受控浏览器中完成登录或授权。不要尝试绕过登录、验证码、MFA 或权限；等待用户在 Desktop 接管窗口完成操作后再继续。",
        "crawler.interaction.captcha_required": "页面要求人工验证。不要尝试绕过验证码或改变请求规避限制；等待用户在受控浏览器中完成验证后再继续。",
        "crawler.interaction.user_action_required": "抓取需要用户在受控浏览器中完成操作。不要自动重试或规避页面限制；等待用户完成操作后再继续。",
        "crawler.policy.robots_denied": "目标明确拒绝自动抓取。不要重试、规避 robots 或切换到未授权抓取方式；可使用当前 Catalog 中独立且允许的公开来源，或请求用户提供授权材料。",
        "crawler.policy.session_scope_denied": "当前浏览器会话不属于该目标范围。不要复用、猜测或绕过会话；等待用户在受控流程中提供匹配授权，或选择其它允许来源。",
        "crawler.quality.insufficient": "Crawler 已完成其有界质量恢复但证据仍不足。不要重复同一抓取参数或自行扩展浏览器动作；基于已验证的部分证据如实回答，或使用当前 Catalog 中独立的替代来源。",
        "crawler.provider.rate_limited": "目标暂时限制了抓取请求。不要原样重放或规避站点限制；使用当前 Catalog 中独立来源，或稍后由用户决定是否重新尝试。",
        "crawler.provider.unavailable": "Crawler 未取得可用页面。不要重复同一请求或绕过站点限制；改用当前 Catalog 中独立来源，或明确说明该来源当前不可用。",
        "crawler.source.timeout": "目标网页响应过慢，正文读取已超时。不要重复读取同一来源；改用当前 Catalog 中独立的公开来源，或请用户稍后重试。",
        "crawler.source.network_error": "目标网页的网络请求失败。不要重复读取同一来源；改用当前 Catalog 中独立的公开来源，或请用户检查网络后重试。",
        "crawler.source.http_error": "目标网页未成功响应。不要重复读取同一来源或绕过站点限制；改用当前 Catalog 中独立的公开来源，或请用户稍后重试。",
    }
    if reason in targeted:
        return targeted[reason]
    if action == "wait_for_user":
        return f"工具 '{tool_name}' 的失败属于不可自动修复的受控边界（{reason}）。不要重试或绕过权限/审批；请等待用户处理或说明需要人工帮助。"
    if reason == "tool.resource_not_found":
        return f"工具 '{tool_name}' 找不到目标资源。停止猜测路径或参数，先用可靠的目录/查找工具确认当前事实，再重新规划；无法确认时请求用户提供准确位置。"
    if reason == "tool.permission_denied":
        return f"工具 '{tool_name}' 触碰权限边界。停止重复调用，不要绕过授权、沙箱或 Workspace Grant；改用已授权的安全方案或请求人工帮助。"
    if reason == "tool.timeout":
        return f"工具 '{tool_name}' 执行超时。不要原样重放；缩小任务、改用有界的替代工具，或说明需要人工处理。"
    if reason.startswith(("script.security.", "script.workspace.", "script.sandbox.")):
        return f"工具 '{tool_name}' 触碰受控脚本安全边界（{reason}）。立即停止本 Run 内的脚本重试；不要绕过沙箱、Workspace Grant、输出声明、allow-list、网络或进程限制。改用已授权工具，或说明需要人工帮助。"
    return f"工具 '{tool_name}' 执行失败（{reason}）。原始错误不足以指导恢复：停止无效重试，跳出局部思维并彻底改变策略；若无法通过系统工具解决，直接结束任务并说明需要的人工帮助。"


def _suggested_tools(tool_name: str, reason: str) -> list[str]:
    if reason == "file.edit.old_text_not_found":
        return ["cowork.file.read_file", "cowork.file.search_text"]
    if reason == "file.edit.old_text_ambiguous":
        return ["cowork.file.search_text", "cowork.file.read_file"]
    if reason == "file.resource_exists":
        return ["cowork.file.read_file", "cowork.file.edit_file", "cowork.file.list_dir"]
    if reason in {"tool.resource_not_found", "file.resource_not_found"} and tool_name.startswith("cowork.file."):
        return ["cowork.file.list_dir", "cowork.file.find_name"]
    if reason in {"provider.platform.unavailable", "provider.platform.session_unavailable"}:
        return ["cowork.web.search"]
    if reason == "provider.search.unavailable":
        return []
    if reason == "crawler.policy.robots_denied":
        return ["cowork.web.search"]
    if reason in {
        "crawler.quality.insufficient",
        "crawler.provider.rate_limited",
        "crawler.provider.unavailable",
        "crawler.source.timeout",
        "crawler.source.network_error",
        "crawler.source.http_error",
    }:
        return ["cowork.web.search"]
    return []


def _failure_action(
    reason: str,
    *,
    do_not_retry: bool,
    error_code: RuntimeErrorCode,
    suggested_tools: list[str],
) -> str:
    if do_not_retry or error_code in {
        RuntimeErrorCode.POLICY_DENIED,
        RuntimeErrorCode.CANCELLED,
        RuntimeErrorCode.APPROVAL_EXPIRED,
    }:
        return "wait_for_user"
    if reason in {
        "provider.platform.unavailable",
        "provider.platform.session_unavailable",
        "provider.search.unavailable",
        "provider.health.degraded",
        "provider.health.unavailable",
        "provider.tool.unavailable",
        "crawler.policy.robots_denied",
        "crawler.provider.rate_limited",
        "crawler.provider.unavailable",
        "crawler.source.timeout",
        "crawler.source.network_error",
        "crawler.source.http_error",
        "crawler.quality.insufficient",
    }:
        return "switch_tool" if suggested_tools else "stop"
    return "replan"


def _filter_available_tools(candidates: list[str], available_tools: Iterable[str] | None) -> list[str]:
    unique = list(dict.fromkeys(candidates))
    if available_tools is None:
        return unique
    allowed = {str(item) for item in available_tools}
    return [tool for tool in unique if tool in allowed]


def _error_suggested_tools(error: RuntimeError) -> list[str]:
    """Use only ToolHost/manifest-declared alternatives, never error prose."""

    details = error.details if isinstance(error.details, dict) else {}
    raw = details.get("suggested_tools")
    if not isinstance(raw, list):
        return []
    return [item for item in raw[:8] if isinstance(item, str) and item and len(item) <= 160]


__all__ = ["RecoveryManager", "RecoveryGuardDecision"]
