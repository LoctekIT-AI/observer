"""Recovery guidance for a model that loses a trusted local workspace URI."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from cowork_runtime.contracts import RuntimeError, ToolCall
from cowork_runtime.contracts.recovery import RecoveryGuidance

_SINGLE_URI_FILE_TOOLS = frozenset(
    {
        "cowork.file.stat",
        "cowork.file.list_dir",
        "cowork.file.find_name",
        "cowork.file.search_text",
        "cowork.file.read_file",
        "cowork.file.read_range",
        "cowork.file.preview",
        "cowork.file.edit_file",
    }
)
_FILE_URI_ARGUMENTS = frozenset(
    {
        "uri",
        "target_uri",
        "workspace_uri",
        "left_uri",
        "right_uri",
        "source_uri",
    }
)


def workspace_uri_recovery_guidance(
    call: ToolCall,
    error: RuntimeError | None,
    *,
    run_metadata: Mapping[str, Any],
    resolve_authorized_directory_uri: Callable[[str], str | None] | None,
) -> RecoveryGuidance | None:
    """Build a recoverable correction for root/missing file URIs only.

    The candidate URI comes from Run metadata, which the Desktop Host sets
    before the turn starts.  It is re-authorized immediately through the
    injected registry callback.  Consequently, a model cannot turn this
    recovery path into an alternate-directory lookup or a silent URI rewrite.
    """

    if resolve_authorized_directory_uri is None or not call.tool_name.startswith("cowork.file."):
        return None
    if not (_uses_filesystem_root(call.arguments) or _missing_required_file_uri(call)):
        return None
    reason_code = _reason_code(error)
    if reason_code not in {"", "file.workspace.outside_authorized"}:
        return None
    raw_workspace_uri = run_metadata.get("workspace_uri")
    if not isinstance(raw_workspace_uri, str) or not raw_workspace_uri.strip():
        return None
    workspace_uri = resolve_authorized_directory_uri(raw_workspace_uri.strip())
    if not workspace_uri:
        return None
    return RecoveryGuidance(
        reason_code="file.workspace.outside_authorized",
        action="replan",
        instruction=(
            "本次 Run 已有 Host 验证且当前仍有效的本地工作区 URI："
            f"`{workspace_uri}`。刚才的文件调用使用了文件系统根目录或遗漏了 URI，"
            "这不是用户缺少文件授权。不要重试 `file:///`、不要猜测其他目录、不要请求扩大授权，"
            "也不要假定 Runtime 会改写参数。仅可在下一次文件调用中明确使用该工作区 URI，"
            "或使用其下已确认文件的 URI。"
        ),
    )


def _uses_filesystem_root(arguments: Mapping[str, Any]) -> bool:
    for key, value in arguments.items():
        if key not in _FILE_URI_ARGUMENTS:
            continue
        if isinstance(value, str) and _is_file_root_uri(value):
            return True
    return False


def _missing_required_file_uri(call: ToolCall) -> bool:
    if call.tool_name not in _SINGLE_URI_FILE_TOOLS:
        return False
    value = call.arguments.get("uri")
    return not isinstance(value, str) or not value.strip()


def _is_file_root_uri(value: str) -> bool:
    parsed = urlsplit(value.strip())
    return (
        parsed.scheme.casefold() == "file"
        and parsed.netloc.casefold() in {"", "localhost"}
        and parsed.path == "/"
        and not parsed.query
        and not parsed.fragment
    )


def _reason_code(error: RuntimeError | None) -> str:
    details = error.details if error is not None and isinstance(error.details, dict) else {}
    value = details.get("reason_code")
    return value if isinstance(value, str) else ""


__all__ = ["workspace_uri_recovery_guidance"]
