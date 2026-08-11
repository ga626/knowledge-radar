"""Shared URL normalization, short-link expansion, and preflight checks."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable, Optional

import httpx

log = logging.getLogger("mcp-server")


@dataclass(frozen=True)
class UrlPreflightResult:
    url: str
    final_url: str
    available: bool
    reason: str = ""
    status_code: int = 0

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "available": self.available,
            "reason": self.reason,
            "status_code": self.status_code,
        }


XHS_NOTE_URL_RE = re.compile(
    r"https?://(?:www\.)?xiaohongshu\.com/(?:explore|discovery/item)/[A-Za-z0-9_-]+(?:\?[^\s\"'<>]*)?",
    re.IGNORECASE,
)


def normalize_xhs_note_url(url: str) -> str:
    url = str(url or "").strip().split("#", 1)[0]
    match = re.search(r"xiaohongshu\.com/(?:explore|discovery/item)/([A-Za-z0-9_-]+)", url, re.IGNORECASE)
    if not match:
        return ""
    note_id = match.group(1)
    query = ""
    if "?" in url:
        query = "?" + url.split("?", 1)[1]
    return f"https://www.xiaohongshu.com/explore/{note_id}{query}"


def expand_short_url(url: str, *, platform: str = "", timeout: float = 8.0) -> str:
    url = str(url or "").strip()
    if not url:
        return ""
    platform = platform.lower()
    if platform in {"xhs", "xiaohongshu", "小红书"}:
        return expand_xhs_short_url(url, timeout=timeout)
    return url


def expand_xhs_short_url(url: str, *, timeout: float = 8.0) -> str:
    url = str(url or "").strip()
    if not url:
        return ""
    if "xhslink.com" not in url and "xiaohongshu.com" not in url:
        return url
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
            response = client.get(url)
            final_url = str(response.url or url)
            if "xiaohongshu.com" in final_url:
                return final_url
            match = XHS_NOTE_URL_RE.search(response.text or "")
            if match:
                return match.group(0)
    except Exception as exc:
        log.debug(f"短链展开失败: platform=xhs, error={exc}")
    return url


def preflight_url(
    url: str,
    *,
    platform: str = "",
    negative_cache_lookup: Optional[Callable[[str], bool]] = None,
    negative_cache_record: Optional[Callable[[str, str], None]] = None,
    timeout: float = 8.0,
) -> UrlPreflightResult:
    platform = platform.lower()
    if platform in {"xhs", "xiaohongshu", "小红书"}:
        return preflight_xhs_url(
            url,
            negative_cache_lookup=negative_cache_lookup,
            negative_cache_record=negative_cache_record,
            timeout=timeout,
        )
    return UrlPreflightResult(url=str(url or ""), final_url=str(url or ""), available=bool(str(url or "").strip()))


def preflight_xhs_url(
    url: str,
    *,
    negative_cache_lookup: Optional[Callable[[str], bool]] = None,
    negative_cache_record: Optional[Callable[[str, str], None]] = None,
    timeout: float = 8.0,
) -> UrlPreflightResult:
    url = str(url or "").strip()
    if not url:
        return UrlPreflightResult(url="", final_url="", available=False, reason="empty_url")
    if negative_cache_lookup and negative_cache_lookup(url):
        return UrlPreflightResult(url=url, final_url=url, available=False, reason="negative_cached")
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
            response = client.get(url)
            final_url = str(response.url or url)
            text = (response.text or "")[:4000]
            if response.status_code >= 400:
                return UrlPreflightResult(url=url, final_url=final_url, available=False, reason="http_error", status_code=response.status_code)
            if "页面不见了" in text or "你访问的页面不见了" in text or "not found" in text.lower():
                if negative_cache_record:
                    negative_cache_record(final_url or url, "dead_page")
                return UrlPreflightResult(url=url, final_url=final_url, available=False, reason="dead_page", status_code=response.status_code)
            if "xiaohongshu.com" not in final_url:
                return UrlPreflightResult(url=url, final_url=final_url, available=False, reason="off_platform", status_code=response.status_code)
            return UrlPreflightResult(url=url, final_url=final_url, available=True, status_code=response.status_code)
    except Exception as exc:
        log.debug(f"URL 预检失败: platform=xhs, error={exc}")
        return UrlPreflightResult(url=url, final_url=url, available=False, reason="preflight_error")
