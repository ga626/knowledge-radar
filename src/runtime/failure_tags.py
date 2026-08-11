"""Shared failure tag taxonomy for KnowledgeRadar observability."""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List

from .chrome_manager import XHS_CHROME_DEBUG_PORT


FAILURE_TAGS: Dict[str, Dict[str, Any]] = {
    "dead_link": {
        "category": "content_access",
        "patterns": ["404", "not found", "页面不存在", "内容不存在", "链接失效", "dead link"],
    },
    "empty_detail": {
        "category": "content_access",
        "patterns": ["详情为空", "empty detail", "returned empty", "too little content", "无有效正文"],
    },
    "empty_results": {
        "category": "search",
        "patterns": ["empty_results", "空结果", "无结果", "returned no results"],
    },
    "bridge_parse_failed": {
        "category": "bridge",
        "patterns": ["bridge", "parse", "解析失败", "json parse", "bridge_parse_failed"],
    },
    "anti_bot_verification": {
        "category": "platform_guard",
        "patterns": ["captcha", "verification", "verify", "风控", "验证", "扫码查看", "安全校验"],
    },
    "login_required": {
        "category": "auth",
        "patterns": ["login_required", "login", "cookie", "登录", "鉴权", "unauthorized", "401", "403"],
    },
    "rate_limited": {
        "category": "traffic",
        "patterns": ["rate_limited", "rate limit", "429", "限流", "频率"],
    },
    "cdp_unavailable": {
        "category": "runtime",
        "patterns": ["cdp", "chrome", XHS_CHROME_DEBUG_PORT, "12734", "调试端口"],
    },
    "provider_unavailable": {
        "category": "provider",
        "patterns": ["not_configured", "provider", "all providers failed", "fetch failed"],
    },
    "network_timeout": {
        "category": "network",
        "patterns": ["timeout", "timed out", "超时"],
    },
    "network_error": {
        "category": "network",
        "patterns": ["connect", "connection", "网络", "ssl", "request failed"],
    },
    "model_unavailable": {
        "category": "model",
        "patterns": ["model does not exist", "model_unavailable", "siliconflow", "llm"],
    },
}


def _text_parts(values: Iterable[Any]) -> str:
    parts: List[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, dict):
            parts.extend(str(v) for v in value.values())
        elif isinstance(value, (list, tuple, set)):
            parts.extend(str(v) for v in value)
        else:
            parts.append(str(value))
    return " ".join(parts).lower()


def detect_failure_tags(*values: Any) -> List[str]:
    """Return stable failure tags inferred from free-form errors/metadata."""
    text = _text_parts(values)
    if not text:
        return []
    tags: List[str] = []
    for tag, spec in FAILURE_TAGS.items():
        for pattern in spec.get("patterns", []):
            if re.search(re.escape(str(pattern).lower()), text):
                tags.append(tag)
                break
    return tags


def describe_failure_tags(tags: Iterable[str]) -> List[Dict[str, Any]]:
    """Expand tags into report-friendly taxonomy entries."""
    described: List[Dict[str, Any]] = []
    for tag in tags:
        spec = FAILURE_TAGS.get(tag, {})
        described.append({"tag": tag, "category": spec.get("category", "unknown")})
    return described
