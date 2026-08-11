"""Direct media URL discovery and reachability probes.

The functions here do not download media. They only extract direct URL
candidates, redact sensitive query strings, and run lightweight HEAD/range
reachability checks for routing and capability reporting.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlparse

import httpx


BILIBILI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com",
    "Origin": "https://www.bilibili.com",
}


@dataclass(frozen=True)
class DirectMediaCandidate:
    url: str
    kind: str = "video"
    platform: str = ""
    extractor: str = ""
    format_id: str = ""
    ext: str = ""
    vcodec: str = ""
    acodec: str = ""
    height: int = 0
    width: int = 0
    filesize: int = 0
    duration: int = 0
    expires_at: float | None = None
    metadata: dict[str, Any] | None = None

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "platform": self.platform,
            "extractor": self.extractor,
            "format_id": self.format_id,
            "ext": self.ext,
            "vcodec": self.vcodec,
            "acodec": self.acodec,
            "height": self.height,
            "width": self.width,
            "filesize": self.filesize,
            "duration": self.duration,
            "expires_at": self.expires_at,
            "redacted_url": redact_url(self.url),
            "metadata": dict(self.metadata or {}),
        }


def redact_url(url: str) -> dict[str, str]:
    parsed = urlparse(str(url or ""))
    safe_path = parsed.path or ""
    return {
        "scheme": parsed.scheme,
        "host": parsed.netloc,
        "path_suffix": safe_path[-48:],
        "sha256_16": hashlib.sha256(str(url or "").encode("utf-8", errors="ignore")).hexdigest()[:16],
    }


def probe_direct_url_reachability(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 12,
    range_bytes: int = 1024,
) -> dict[str, Any]:
    if not url:
        return {"status": "bad_request", "reason": "url is empty"}
    started = time.time()
    request_headers = dict(headers or {})
    if range_bytes > 0:
        request_headers.setdefault("Range", f"bytes=0-{range_bytes - 1}")
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout, headers=request_headers) as client:
            head = client.head(url)
            if head.status_code in {200, 206}:
                response = head
                method = "HEAD"
            else:
                response = client.get(url)
                method = "GET_RANGE"
        return {
            "status": "reachable" if response.status_code in {200, 206} else "unreachable",
            "method": method,
            "http_status": response.status_code,
            "content_type": response.headers.get("content-type", ""),
            "content_length": response.headers.get("content-length", ""),
            "accept_ranges": response.headers.get("accept-ranges", ""),
            "final_url": redact_url(str(response.url)),
            "elapsed_s": round(time.time() - started, 3),
        }
    except Exception as exc:
        return {
            "status": "unreachable",
            "method": "HEAD_THEN_GET_RANGE",
            "reason": str(exc)[:240],
            "elapsed_s": round(time.time() - started, 3),
        }


def iter_ytdlp_format_urls(info: dict[str, Any]) -> Iterable[dict[str, Any]]:
    requested = info.get("requested_downloads") or []
    formats = requested or info.get("formats") or []
    for item in formats:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not url:
            continue
        yield {
            "url": str(url),
            "format_id": item.get("format_id") or item.get("format") or "",
            "ext": item.get("ext") or "",
            "vcodec": item.get("vcodec") or "",
            "acodec": item.get("acodec") or "",
            "height": int(item.get("height") or 0),
            "width": int(item.get("width") or 0),
            "filesize": int(item.get("filesize") or item.get("filesize_approx") or 0),
        }


def select_video_candidate(info: dict[str, Any]) -> dict[str, Any]:
    candidates = list(iter_ytdlp_format_urls(info))
    if not candidates:
        return {}
    video_candidates = [item for item in candidates if item.get("vcodec") not in {"", "none", None}]
    selected = sorted(
        video_candidates or candidates,
        key=lambda item: (
            int(item.get("height") or 0) > 0,
            int(item.get("height") or 0) <= 720,
            int(item.get("filesize") or 0) > 0,
            -abs(int(item.get("height") or 360) - 360),
        ),
        reverse=True,
    )[0]
    return selected


def _expiry_from_url(url: str) -> float | None:
    match = re.search(r"(?:deadline|expires?|expire|wsTime)=([0-9]{10,})", url)
    if not match:
        return None
    try:
        raw = int(match.group(1))
    except ValueError:
        return None
    return float(raw / 1000 if raw > 10_000_000_000 else raw)


def bilibili_direct_candidate_with_ytdlp(target: str) -> dict[str, Any]:
    from yt_dlp import YoutubeDL

    url = target if target.startswith(("http://", "https://")) else f"https://www.bilibili.com/video/{target}"
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": "bv*[height<=720]+ba/b[height<=720]/best",
        "http_headers": dict(BILIBILI_HEADERS),
        "noplaylist": True,
    }
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        return {"status": "failed", "reason": str(exc)[:240], "extractor": "yt-dlp"}
    selected = select_video_candidate(info or {})
    if not selected:
        return {"status": "failed", "reason": "yt-dlp returned no playable video URL", "extractor": "yt-dlp"}
    candidate = DirectMediaCandidate(
        url=selected["url"],
        platform="bilibili",
        extractor="yt-dlp",
        format_id=str(selected.get("format_id") or ""),
        ext=str(selected.get("ext") or ""),
        vcodec=str(selected.get("vcodec") or ""),
        acodec=str(selected.get("acodec") or ""),
        height=int(selected.get("height") or 0),
        width=int(selected.get("width") or 0),
        filesize=int(selected.get("filesize") or 0),
        duration=int((info or {}).get("duration") or 0),
        expires_at=_expiry_from_url(selected["url"]),
    )
    return {
        "status": "ok",
        "extractor": "yt-dlp",
        "title": str((info or {}).get("title") or "")[:120],
        "duration": int((info or {}).get("duration") or 0),
        "candidate": candidate,
        "redacted_url": redact_url(candidate.url),
    }


def youtube_watch_url_candidate(video_id_or_url: str) -> dict[str, Any]:
    text = str(video_id_or_url or "").strip()
    if not text:
        return {"status": "bad_request", "reason": "video id/url is empty"}
    if text.startswith(("http://", "https://")):
        url = text
    else:
        url = f"https://www.youtube.com/watch?v={text}"
    return {
        "status": "watch_url_only",
        "reason": "YouTube Data API does not expose a direct playable video URL; yt-dlp extraction is required before native_media.",
        "candidate": DirectMediaCandidate(url=url, platform="youtube", extractor="watch_url", metadata={"direct": False}),
        "redacted_url": redact_url(url),
    }


def build_direct_media_probe(
    candidate_result: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    probe_reachability: bool = True,
) -> dict[str, Any]:
    output = {
        "schema": "knowledgeradar-direct-media/v1",
        "status": candidate_result.get("status", "failed"),
        "extractor": candidate_result.get("extractor", ""),
        "title": candidate_result.get("title", ""),
        "duration": candidate_result.get("duration", 0),
        "reason": candidate_result.get("reason", ""),
    }
    candidate = candidate_result.get("candidate")
    if isinstance(candidate, DirectMediaCandidate):
        output["candidates"] = [candidate.to_safe_dict()]
        if probe_reachability:
            output["reachability"] = probe_direct_url_reachability(candidate.url, headers=headers)
        else:
            output["reachability"] = {"status": "skipped", "reason": "disabled"}
        output["provider_downloadability"] = provider_downloadability(candidate, output["reachability"])
    else:
        output["candidates"] = []
        output["reachability"] = {"status": "skipped", "reason": "no candidate"}
        output["provider_downloadability"] = {"status": "not_downloadable", "reason": "no direct media candidate"}
    return output


def provider_downloadability(candidate: DirectMediaCandidate, reachability: dict[str, Any] | None = None) -> dict[str, Any]:
    """Classify whether a model provider should be allowed to fetch this URL.

    Local reachability is not enough. Bilibili CDN URLs can be reachable from
    the local browser/yt-dlp path but still fail in provider-side model fetches
    because they depend on headers, region, anti-hotlinking, or short-lived
    signed query parameters.
    """
    platform = (candidate.platform or "").lower()
    extractor = (candidate.extractor or "").lower()
    if platform == "youtube" and extractor == "watch_url":
        return {
            "status": "not_direct",
            "reason": "YouTube watch URLs are not direct playable media URLs",
            "allow_native_media": False,
        }
    if platform == "bilibili":
        return {
            "status": "provider_blocked",
            "reason": "Bilibili raw CDN URLs are locally probeable but failed provider-side video_url/audio_url fetch matrix; use derived_text or sampled fallback",
            "allow_native_media": False,
        }
    if (reachability or {}).get("status") == "reachable":
        return {
            "status": "provider_downloadable",
            "reason": "direct URL is locally reachable and not in a known provider-blocked platform class",
            "allow_native_media": True,
        }
    return {
        "status": "unknown_or_unreachable",
        "reason": "direct URL has not passed reachability or provider-downloadability gates",
        "allow_native_media": False,
    }
