"""Small, deterministic scope guard for ordinary market-research requests.

Market research starts from public search results.  The Runtime may read a
small number of those results with its static, no-login crawler, but a model
must not turn that bounded step into browser takeover or arbitrary crawling.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_RESEARCH_MARKERS = ("调研", "研究", "市场分析", "market research", "market analysis")
_DEEP_COLLECTION_MARKERS = (
    "抓取", "爬取", "采集", "打开网页", "浏览网页", "读取网页", "访问链接",
    "fetch page", "crawl", "scrape", "open page", "browse page",
)
_RUNTIME_LIGHTWEIGHT_FETCH_IDS_KEY = "runtime_lightweight_fetch_call_ids"
_LIGHTWEIGHT_FETCH_METADATA = {
    "search_followup": True,
    "research_lightweight_public_fetch": True,
    "browser_fallback": "off",
    "skip_user_action_required": True,
}


def is_search_first_market_research_request(value: object) -> bool:
    """Return true for an ordinary market-research request without deep scope.

    A URL or an explicit collection verb is an unambiguous request for the
    existing controlled crawler path.  Everything else starts with the
    read-only search engine tool and can still explain its source limitations.
    """

    text = str(value or "").strip()
    normalized = text.lower()
    return (
        bool(text)
        and any(marker in normalized for marker in _RESEARCH_MARKERS)
        and _URL_RE.search(text) is None
        and not any(marker in normalized for marker in _DEEP_COLLECTION_MARKERS)
    )


def search_first_manifests(manifests: list[Any], metadata: Mapping[str, Any] | None) -> list[Any]:
    """Hide page/crawler tools for a search-first research Run.

    The immutable catalog still permits report-generation and ordinary local
    work.  Only network page collection is removed; ``cowork.web.search`` is
    intentionally retained as the public, no-login source of first resort.
    """

    if not isinstance(metadata, Mapping) or metadata.get("research_search_first") is not True:
        return manifests
    result: list[Any] = []
    for manifest in manifests:
        name = manifest.get("name") if isinstance(manifest, Mapping) else getattr(manifest, "name", None)
        if isinstance(name, str) and name.startswith("cowork.web.") and name not in {"cowork.web.search", "cowork.web.fetch"}:
            continue
        result.append(manifest)
    return result


def search_first_call_violation(call: Any, metadata: Mapping[str, Any] | None) -> tuple[str, str] | None:
    """Keep an out-of-catalog model call from reviving browser collection.

    A generated page read is authorized by an opaque, per-call id which is
    installed into the execution context immediately before the Runtime calls
    the tool.  The model never receives that id while choosing a capability.
    """

    if not isinstance(metadata, Mapping) or metadata.get("research_search_first") is not True:
        return None
    name = str(getattr(call, "tool_name", "") or "")
    if name == "cowork.web.search":
        return None
    if name == "cowork.web.fetch":
        # `web.fetch` is a single-URL, public, SSRF-checked read. It is the
        # explicit search->fetch step and does not grant site crawling.
        return None
    if name == "cowork.web.fetch_page" and _is_runtime_lightweight_fetch(call, metadata):
        return None
    if name.startswith("cowork.web."):
        return (
            "research.search_first_scope_denied",
            "ordinary market research only permits Runtime-owned public search-result reads; arbitrary page collection requires an explicit request",
        )
    return None


def _is_runtime_lightweight_fetch(call: Any, metadata: Mapping[str, Any]) -> bool:
    call_id = str(getattr(call, "tool_call_id", "") or "")
    call_ids = metadata.get(_RUNTIME_LIGHTWEIGHT_FETCH_IDS_KEY)
    if not isinstance(call_ids, list) or call_id not in call_ids:
        return False
    arguments = getattr(call, "arguments", {})
    request_metadata = arguments.get("metadata") if isinstance(arguments, Mapping) else None
    return isinstance(request_metadata, Mapping) and all(
        request_metadata.get(key) == value
        for key, value in _LIGHTWEIGHT_FETCH_METADATA.items()
    )


def search_first_system_message() -> str:
    """Give the model a product-level explanation matching the hard scope."""

    return (
        "这是常规市场调研。请先使用 cowork.web.search 获取公开搜索结果；Runtime 会按相关性"
        "对少量公开结果执行无登录、无浏览器接管的静态读取。不要自行请求页面抓取、站点爬行、"
        "浏览器接管、登录或网站授权；受限来源会被跳过。搜索结果中的标题、摘要、来源和"
        "查询词本身也是可引用的公开证据：即使页面读取失败，也要基于这些摘要给出带来源"
        "和统计口径说明的方向性区间/估算（例如某机构估算约为多少），不要笼统写成‘没有数据’。"
        "只有在搜索结果和摘要确实没有相关信号时，才说明公开信息不足。"
    )


__all__ = [
    "is_search_first_market_research_request",
    "search_first_call_violation",
    "search_first_manifests",
    "search_first_system_message",
]
