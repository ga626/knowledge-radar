"""Evidence envelope helpers for detail extraction results."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict

from kr_core import EvidenceItem

PlatformInferer = Callable[[str], str]


def freshness_for_platform(platform: str, result: Dict[str, Any], *, now: datetime | None = None, recent_days: int = 30) -> str:
    published_at = str(
        result.get("published_at")
        or result.get("publish_time")
        or result.get("created_at")
        or result.get("pubdate")
        or ""
    ).strip()
    if not published_at:
        return "unknown"
    clock = now or datetime.now(timezone.utc)
    clock = clock.replace(tzinfo=timezone.utc) if clock.tzinfo is None else clock
    try:
        if published_at in {"刚刚", "今天", "昨天"}:
            date = clock - timedelta(days=int(published_at == "昨天"))
        elif match := re.fullmatch(r"(\d+)\s*(分钟|小时|天)前", published_at):
            date = clock - timedelta(seconds=int(match[1]) * {"分钟": 60, "小时": 3600, "天": 86400}[match[2]])
        elif re.fullmatch(r"\d{10}(?:\d{3})?(?:\.\d+)?", published_at):
            value = float(published_at)
            date = datetime.fromtimestamp(value / 1000 if value > 10**12 else value, timezone.utc)
        else:
            normalized = re.sub(r"年|月", "-", published_at).replace("日", "")
            try:
                date = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
            except ValueError:
                date = parsedate_to_datetime(published_at)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
        age = clock - date
        if age.total_seconds() < -300:
            return "future"
        return "recent" if age <= timedelta(days=recent_days) else "dated"
    except (ValueError, TypeError, OverflowError, OSError):
        return "unknown"


def evidence_summary(result: Dict[str, Any]) -> str:
    title = str(result.get("title") or "").strip()
    desc = str(result.get("desc") or result.get("content") or result.get("transcript") or "").strip()
    text = f"{title}。{desc}" if title and desc else title or desc
    text = re.sub(r"\s+", " ", text)
    return text[:240]


def build_detail_evidence(url: str, platform: str, result: Dict[str, Any], *, now: datetime | None = None) -> EvidenceItem:
    from generic_web.url_policy import canonical_url

    has_error = bool(result.get("error") or result.get("failure_type")) or result.get("status") in {"failed", "error", "empty", "needs_interaction"}
    body = re.sub(r"\s+", " ", str(result.get("content") or result.get("transcript") or "")).strip()
    extracted = not has_error and len(body) >= 80
    # 正文成功提取不代表主张已核实；旧中文字段只作兼容展示。
    verification_status = "待核验" if extracted else "待验证"
    credibility = {
        "B站": "medium（平台元数据、转写和评论来自实时详情链路）",
        "知乎": "medium-high（正文来自登录态页面/API提取）",
        "小红书": "medium（正文/图片来自登录态页面和 bridge/CDP 提取）",
    }.get(platform, "medium")
    if not extracted:
        credibility = "low（详情失败或有效正文不足，不能支持结论）"
    published_at = str(
        result.get("published_at")
        or result.get("publish_time")
        or result.get("created_at")
        or result.get("pubdate")
        or "unknown"
    )
    freshness = freshness_for_platform(platform, result, now=now)
    clock = now or datetime.now(timezone.utc)
    clock = clock.replace(tzinfo=timezone.utc) if clock.tzinfo is None else clock
    return EvidenceItem(
        source_url=canonical_url(url),
        source_platform=platform or str(result.get("platform") or "unknown"),
        published_at=published_at,
        summary=evidence_summary(result) if not has_error else str(result.get("error") or "")[:240],
        credibility=credibility,
        freshness=freshness,
        verification_status=verification_status,
        metadata={
            "schema": "knowledgeradar-evidence/v2",
            "extraction_status": "extracted" if extracted else "failed" if has_error else "insufficient_content",
            "retrieval_status": "failed" if has_error else "received",
            "identity_status": "unverified",
            "content_completeness": "substantive_text" if extracted else "insufficient",
            "published_at_confidence": freshness if freshness in {"future", "unknown"} else "parsed",
            "source_verification": "unverified",
            "claim_verification": "unverified",
            "evidence_strength": "detail_unverified" if extracted else "insufficient",
            "legacy_verification_field": "display_only_until_2027_01",
            "legacy_verification_remove_after": "2027-01-01T00:00:00Z",
            "legacy_verification_enabled": clock < datetime(2027, 1, 1, tzinfo=timezone.utc),
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
    platform = str(result.get("platform") or infer_platform(url) or "unknown")
    result["evidence"] = build_detail_evidence(url, platform, result).to_mcp_dict()
    return result
