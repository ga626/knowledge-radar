"""Evidence envelope helpers for detail extraction results."""

from __future__ import annotations

import re
from typing import Any, Callable, Dict

from kr_core import EvidenceItem

PlatformInferer = Callable[[str], str]


def freshness_for_platform(platform: str, result: Dict[str, Any]) -> str:
    published_at = str(
        result.get("published_at")
        or result.get("publish_time")
        or result.get("created_at")
        or result.get("pubdate")
        or ""
    ).strip()
    if not published_at:
        return "unknown"
    if re.search(r"202[5-6]|刚刚|今天|昨天|小时前|分钟前", published_at):
        return "recent"
    return "evergreen"


def evidence_summary(result: Dict[str, Any]) -> str:
    title = str(result.get("title") or "").strip()
    desc = str(result.get("desc") or result.get("content") or result.get("transcript") or "").strip()
    text = f"{title}。{desc}" if title and desc else title or desc
    text = re.sub(r"\s+", " ", text)
    return text[:240]


def build_detail_evidence(url: str, platform: str, result: Dict[str, Any]) -> EvidenceItem:
    has_error = bool(result.get("error"))
    verification_status = "待验证" if has_error else "已验证"
    credibility = {
        "B站": "medium（平台元数据、转写和评论来自实时详情链路）",
        "知乎": "medium-high（正文来自登录态页面/API提取）",
        "小红书": "medium（正文/图片来自登录态页面和 bridge/CDP 提取）",
    }.get(platform, "medium")
    if has_error:
        credibility = "low（详情提取失败，仅保留错误上下文）"
    published_at = str(
        result.get("published_at")
        or result.get("publish_time")
        or result.get("created_at")
        or result.get("pubdate")
        or "unknown"
    )
    return EvidenceItem(
        source_url=url,
        source_platform=platform or str(result.get("platform") or "unknown"),
        published_at=published_at,
        summary=evidence_summary(result) if not has_error else str(result.get("error") or "")[:240],
        credibility=credibility,
        freshness=freshness_for_platform(platform, result),
        verification_status=verification_status,
        metadata={
            "title": result.get("title", ""),
            "author": result.get("author", ""),
            "content_chars": len(str(result.get("content") or "")),
            "transcript_chars": len(str(result.get("transcript") or "")),
            "comment_count": len(result.get("comments") or []) if isinstance(result.get("comments"), list) else 0,
            "has_routing": bool(result.get("routing")),
        },
    )


def attach_detail_evidence(
    url: str,
    result: Dict[str, Any],
    *,
    infer_platform: PlatformInferer,
) -> Dict[str, Any]:
    if not isinstance(result, dict):
        return result
    if result.get("evidence"):
        return result
    platform = str(result.get("platform") or infer_platform(url) or "unknown")
    result["evidence"] = build_detail_evidence(url, platform, result).to_mcp_dict()
    return result
