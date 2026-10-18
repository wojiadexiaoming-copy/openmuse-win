"""web_search：接外部搜索 API。

M1：只实现 HTTP 透传（POST {query, count}），由配置决定接哪家。
未配置 api_url 时返回明确错误，不伪造结果（附3.3）。
"""
from __future__ import annotations

import httpx

from agent.tools_registry import ToolDefinition

# 由 tools/__init__.register_all 注入
_API_URL = ""
_API_KEY = ""


def _search(args: dict) -> str:
    if not _API_URL:
        return "[未配置] web_search 需要在 config.yaml 里配 tools.web_search.api_url"
    query: str = args.get("query", "")
    count: int = int(args.get("count", 5))
    try:
        r = httpx.post(_API_URL, json={"query": query, "count": count},
                       headers={"Authorization": f"Bearer({_API_KEY})"} if _API_KEY else {},
                       timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # noqa: BLE001
        return f"[搜索失败] {type(e).__name__}: {e}"
    items = data.get("results", data if isinstance(data, list) else [])
    lines = []
    for it in items[:count]:
        title = it.get("title", "")
        url = it.get("url", "")
        snippet = (it.get("snippet", "") or "")[:200]
        lines.append(f"- {title}\n  {url}\n  {snippet}")
    return "\n".join(lines) if lines else "[无结果]"


WEB_SEARCH_TOOL = ToolDefinition(
    name="web_search",
    description="联网搜索，返回标题+链接+摘要。未配置 API 时不可用。",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "count": {"type": "integer", "default": 5},
        },
        "required": ["query"],
    },
    func=_search,
    is_readonly=True,
    cacheable=True,
    cache_ttl_sec=3600,
    parallel_safe=True,
)


def configure(api_url: str, api_key: str):
    global _API_URL, _API_KEY
    _API_URL, _API_KEY = api_url, api_key
# [M4 完成] web_search 加缓存（TTL 1 小时，见 spec §7.3）已闭环生效。
