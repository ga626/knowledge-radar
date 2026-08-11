"""V2EX 招聘采集器 - 通过 V2EX v1 API 获取酷工作节点帖子。

V2EX jobs 节点：https://www.v2ex.com/go/jobs
API：/api/topics/show.json?node_name=jobs
"""

from __future__ import annotations

import logging
from typing import Dict, List

import httpx
from bs4 import BeautifulSoup

from kr_core.collection import CollectionTrace, format_search_error, format_search_response
from runtime.proxy_config import get_httpx_proxy, proxy_health_summary

log = logging.getLogger("mcp-server")
_LAST_DIRECT_ERRORS: List[Dict[str, str]] = []

V2EX_API_ENDPOINTS = [
    "https://www.v2ex.com/api/topics/show.json?node_name=jobs",
    "https://hk.v2ex.com/api/topics/show.json?node_name=jobs",
    "https://v2ex.com/api/topics/show.json?node_name=jobs",
]

V2EX_WEB_ENDPOINTS = [
    "https://www.v2ex.com/go/jobs",
    "https://hk.v2ex.com/go/jobs",
    "https://v2ex.com/go/jobs",
]

V2EX_TIMEOUT = httpx.Timeout(connect=4.0, read=8.0, write=4.0, pool=4.0)
V2EX_HEADERS = {
    "User-Agent": "Mozilla/5.0 KnowledgeRadar/1.0",
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _format_search_response(platform: str, items: List[Dict], *, trace: CollectionTrace | None = None) -> Dict:
    return format_search_response(platform, items, trace=trace)


def _format_search_error(platform: str, error_item: Dict, *, trace: CollectionTrace | None = None, strategy: str = "") -> Dict:
    return format_search_error(platform, error_item, trace=trace, strategy=strategy)


def _city_matches(text: str, city: str = "") -> bool:
    city_norm = (city or "").strip().removesuffix("市")
    if not city_norm:
        return True
    haystack = str(text or "")
    remote_markers = ("远程", "remote", "Remote", "REMOTE")
    return city_norm in haystack or any(marker in haystack for marker in remote_markers)


def v2ex_fetch_jobs(keyword: str = "", limit: int = 20, city: str = "") -> List[Dict]:
    """通过 V2EX API 获取招聘帖子。"""
    global _LAST_DIRECT_ERRORS
    _LAST_DIRECT_ERRORS = []
    kw = (keyword or "").lower()
    proxy = get_httpx_proxy()
    proxy_summary = proxy_health_summary()
    try:
        with httpx.Client(
            timeout=V2EX_TIMEOUT,
            follow_redirects=True,
            headers=V2EX_HEADERS,
            proxy=proxy,
            trust_env=True,
        ) as client:
            for endpoint in V2EX_API_ENDPOINTS:
                try:
                    resp = client.get(endpoint)
                    resp.raise_for_status()
                    topics = resp.json()
                    if not isinstance(topics, list):
                        log.debug(f"V2EX API 返回非列表: endpoint={endpoint}, type={type(topics).__name__}")
                        continue
                    items = []
                    for topic in topics:
                        if not isinstance(topic, dict):
                            continue
                        title = str(topic.get("title") or "")
                        content = str(topic.get("content") or topic.get("content_rendered") or "")
                        if kw and kw not in title.lower() and kw not in content.lower():
                            continue
                        if not _city_matches(f"{title}\n{content}", city):
                            continue
                        member = topic.get("member") if isinstance(topic.get("member"), dict) else {}
                        items.append({
                            "title": title,
                            "desc": content[:200],
                            "url": str(topic.get("url") or ""),
                            "author": str(member.get("username") or ""),
                            "replies": int(topic.get("replies") or 0),
                            "created": int(topic.get("created") or 0),
                            "platform": "V2EX",
                            "source": "http_api",
                        })
                        if len(items) >= limit:
                            break
                    if items:
                        log.info(f"V2EX API 返回 {len(items)} 条结果: endpoint={endpoint}")
                        return items[:limit]
                    log.debug(f"V2EX API 无匹配结果: endpoint={endpoint}")
                except Exception as e:
                    _LAST_DIRECT_ERRORS.append({
                        "strategy": "http_api",
                        "endpoint": endpoint,
                        "error_type": type(e).__name__,
                        "message": str(e)[:160],
                    })
                    log.debug(
                        "V2EX API 端点失败: endpoint=%s, proxy_configured=%s, error=%s",
                        endpoint,
                        bool(proxy_summary.get("configured")),
                        e,
                    )
    except Exception as e:
        log.debug(f"V2EX API 异常: {e}")
    return []


def v2ex_fetch_jobs_from_web(keyword: str = "", limit: int = 20, city: str = "") -> List[Dict]:
    """Fetch V2EX jobs from the public node page when the legacy API is empty."""
    proxy = get_httpx_proxy()
    proxy_summary = proxy_health_summary()
    try:
        with httpx.Client(
            timeout=V2EX_TIMEOUT,
            follow_redirects=True,
            headers=V2EX_HEADERS,
            proxy=proxy,
            trust_env=True,
        ) as client:
            for endpoint in V2EX_WEB_ENDPOINTS:
                try:
                    resp = client.get(endpoint)
                    resp.raise_for_status()
                    items = _parse_v2ex_jobs_html(resp.text, endpoint, keyword=keyword, city=city, limit=limit)
                    if items:
                        log.info(f"V2EX 网页 fallback 返回 {len(items)} 条结果: endpoint={endpoint}")
                        return items
                    log.debug(f"V2EX 网页 fallback 无匹配结果: endpoint={endpoint}")
                except Exception as e:
                    _LAST_DIRECT_ERRORS.append({
                        "strategy": "web_fallback",
                        "endpoint": endpoint,
                        "error_type": type(e).__name__,
                        "message": str(e)[:160],
                    })
                    log.debug(
                        "V2EX 网页 fallback 端点失败: endpoint=%s, proxy_configured=%s, error=%s",
                        endpoint,
                        bool(proxy_summary.get("configured")),
                        e,
                    )
    except Exception as e:
        log.debug(f"V2EX 网页 fallback 异常: {e}")
    return []


def _parse_v2ex_jobs_html(html: str, base_url: str, *, keyword: str = "", city: str = "", limit: int = 20) -> List[Dict]:
    soup = BeautifulSoup(html, "lxml")
    items: List[Dict] = []
    kw = (keyword or "").lower()
    root = base_url.split("/go/", 1)[0]
    for cell in soup.select("div.cell.item"):
        title_node = cell.select_one("span.item_title a")
        if not title_node:
            continue
        title = title_node.get_text(" ", strip=True)
        href = title_node.get("href") or ""
        url = href if href.startswith("http") else f"{root}{href}"
        content = cell.get_text(" ", strip=True)
        if kw and kw not in title.lower() and kw not in content.lower():
            continue
        if not _city_matches(f"{title}\n{content}", city):
            continue
        author_node = cell.select_one("strong a[href^='/member/']")
        reply_node = cell.select_one("a.count_livid")
        items.append({
            "title": title,
            "desc": content[:200],
            "url": url,
            "author": author_node.get_text(strip=True) if author_node else "",
            "replies": int(reply_node.get_text(strip=True)) if reply_node and reply_node.get_text(strip=True).isdigit() else 0,
            "platform": "V2EX",
            "source": "web_fallback",
        })
        if len(items) >= limit:
            break
    return items


def legacy_search_v2ex(keyword: str = "", limit: int = 10, city: str = "") -> Dict:
    """V2EX 招聘搜索入口函数。"""
    log.info(f"search_v2ex: keyword={keyword}, city={city}, limit={limit}")
    limit = min(limit, 30)
    trace = CollectionTrace("V2EX", ["http_api"])

    items = v2ex_fetch_jobs(keyword, limit, city=city)

    if items:
        trace.add("http_api", "ok", item_count=len(items))
        result = _format_search_response("V2EX", items, trace=trace)
        result.setdefault("metadata", {})["city_filter"] = {"applied": bool(city), "city": city}
        return result

    trace.add("http_api", "failed", detail="empty_results", error_type="empty_results", retryable=True)
    items = v2ex_fetch_jobs_from_web(keyword, limit, city=city)
    if items:
        trace.add("web_fallback", "ok", item_count=len(items))
        result = _format_search_response("V2EX", items, trace=trace)
        result.setdefault("metadata", {})["city_filter"] = {"applied": bool(city), "city": city}
        return result

    trace.add("web_fallback", "failed", detail="empty_results", error_type="empty_results", retryable=True)
    result = _format_search_error("V2EX", {
        "error": "V2EX 招聘帖子为空",
        "hint": "V2EX jobs 节点可能暂时无数据",
        "failure_type": "empty_results",
        "platform_state": "empty_results",
        "manual_action_required": False,
    }, trace=trace, strategy="http_api")
    result.setdefault("metadata", {})["direct_errors"] = list(_LAST_DIRECT_ERRORS[-6:])
    result["metadata"]["proxy"] = proxy_health_summary()
    result["metadata"]["city_filter"] = {"applied": bool(city), "city": city}
    return result
