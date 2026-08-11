"""YouTube Data API v3 collection helpers."""

from __future__ import annotations

import concurrent.futures
import logging
import os
import re
import threading
import time
from typing import Dict, List, Optional

import httpx

from kr_core.affordance import attach_result_affordance
from runtime.proxy_config import get_httpx_proxy

log = logging.getLogger("mcp-server")

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"


def youtube_api_key() -> str:
    return os.environ.get("YOUTUBE_API_KEY") or ""


def youtube_configured() -> bool:
    return bool(youtube_api_key())


def extract_youtube_video_id(url: str) -> Optional[str]:
    text = str(url or "").strip()
    patterns = [
        r"(?:youtube\.com/watch\?[^#\s]*v=)([A-Za-z0-9_-]{6,})",
        r"(?:youtu\.be/)([A-Za-z0-9_-]{6,})",
        r"(?:youtube\.com/shorts/)([A-Za-z0-9_-]{6,})",
        r"(?:youtube\.com/embed/)([A-Za-z0-9_-]{6,})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", text):
        return text
    return None


def canonical_youtube_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def search_youtube(keyword: str, limit: int = 10) -> Dict:
    api_key = youtube_api_key()
    if not api_key:
        return _youtube_error("YOUTUBE_API_KEY is not configured", error_type="not_configured")
    limit = max(1, min(int(limit or 10), 25))
    try:
        data = _run_with_timeout(
            lambda: _youtube_get(
                "search",
                {
                    "part": "snippet",
                    "q": keyword,
                    "type": "video",
                    "maxResults": limit,
                    "key": api_key,
                },
            ),
            timeout_s=_youtube_operation_timeout_s(),
        )
        items: List[Dict] = []
        for item in data.get("items") or []:
            video_id = ((item.get("id") or {}).get("videoId") or "").strip()
            snippet = item.get("snippet") or {}
            if not video_id:
                continue
            items.append(
                attach_result_affordance(
                    "YouTube",
                    {
                    "title": snippet.get("title", ""),
                    "url": canonical_youtube_url(video_id),
                    "author": snippet.get("channelTitle", ""),
                    "desc": snippet.get("description", ""),
                    "platform": "YouTube",
                    "content_type": "video",
                    "published_at": snippet.get("publishedAt", ""),
                    "metadata": {
                        "video_id": video_id,
                        "channel_id": snippet.get("channelId", ""),
                        "published_at": snippet.get("publishedAt", ""),
                        "thumbnail": _best_thumbnail(snippet.get("thumbnails") or {}),
                        "source": "youtube_search_list",
                    },
                    },
                )
            )
        return {
            "items": items,
            "total": len(items),
            "platform": "YouTube",
            "metadata": {
                "collection": {
                    "platform": "YouTube",
                    "strategy_tree": ["http_api", "youtube_data_api_v3", "search.list"],
                    "selected_strategy": "http_api",
                }
            },
        }
    except Exception as exc:
        return _youtube_error(str(exc), error_type=_youtube_error_type(exc))


def get_youtube_detail(video_id_or_url: str) -> Dict:
    video_id = extract_youtube_video_id(video_id_or_url)
    if not video_id:
        return {"platform": "YouTube", "url": video_id_or_url, "error": "无法从 URL 提取 YouTube video_id"}
    api_key = youtube_api_key()
    if not api_key:
        return _youtube_error("YOUTUBE_API_KEY is not configured", error_type="not_configured", url=canonical_youtube_url(video_id))
    try:
        data = _run_with_timeout(
            lambda: _youtube_get(
                "videos",
                {
                    "part": "snippet,contentDetails,statistics",
                    "id": video_id,
                    "key": api_key,
                },
            ),
            timeout_s=_youtube_operation_timeout_s(),
        )
        items = data.get("items") or []
        if not items:
            return {"platform": "YouTube", "url": canonical_youtube_url(video_id), "error": "YouTube video not found"}
        item = items[0]
        snippet = item.get("snippet") or {}
        stats = item.get("statistics") or {}
        detail = {
            "platform": "YouTube",
            "title": snippet.get("title", ""),
            "desc": snippet.get("description", ""),
            "author": snippet.get("channelTitle", ""),
            "channel_id": snippet.get("channelId", ""),
            "published_at": snippet.get("publishedAt", ""),
            "duration": (item.get("contentDetails") or {}).get("duration", ""),
            "stats": stats,
            "video_play_count": _int_or_zero(stats.get("viewCount")),
            "liked_count": _int_or_zero(stats.get("likeCount")),
            "video_comment": _int_or_zero(stats.get("commentCount")),
            "cover": _best_thumbnail(snippet.get("thumbnails") or {}),
            "url": canonical_youtube_url(video_id),
            "video_id": video_id,
            "content_type": "video",
            "metadata": {
                "video_id": video_id,
                "source": "youtube_videos_list",
                "strategy": "http_api",
            },
        }
        captions = list_youtube_captions(video_id)
        detail["captions"] = captions
        transcript = fetch_youtube_transcript(video_id, preferred_languages=["zh-Hans", "zh", "en"])
        detail["transcript"] = transcript.get("text", "")
        detail["transcript_metadata"] = transcript
        return detail
    except Exception as exc:
        return _youtube_error(str(exc), error_type=_youtube_error_type(exc), url=canonical_youtube_url(video_id))


def list_youtube_captions(video_id: str) -> Dict:
    api_key = youtube_api_key()
    if not api_key:
        return {"status": "skipped", "reason": "YOUTUBE_API_KEY is not configured", "tracks": []}
    try:
        data = _youtube_get("captions", {"part": "snippet", "videoId": video_id, "key": api_key})
        tracks = []
        for item in data.get("items") or []:
            snippet = item.get("snippet") or {}
            tracks.append(
                {
                    "id": item.get("id", ""),
                    "language": snippet.get("language", ""),
                    "name": snippet.get("name", ""),
                    "track_kind": snippet.get("trackKind", ""),
                    "is_auto_synced": snippet.get("isAutoSynced"),
                    "status": snippet.get("status", ""),
                }
            )
        return {"status": "ok", "tracks": tracks, "source": "captions.list"}
    except Exception as exc:
        return {"status": "failed", "error": str(exc), "source": "captions.list", "tracks": []}


def fetch_youtube_transcript(video_id: str, preferred_languages: Optional[List[str]] = None) -> Dict:
    languages = preferred_languages or ["zh-Hans", "zh", "en"]
    try:
        from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore
    except Exception as exc:
        return {
            "status": "unavailable",
            "source": "youtube_transcript_api",
            "error_code": "transcript_fallback_unavailable",
            "error": f"youtube-transcript-api not installed: {exc}",
            "text": "",
        }
    result: Dict = {}

    def _run() -> None:
        try:
            api = YouTubeTranscriptApi()
            transcript = api.fetch(video_id, languages=languages)
            snippets = list(transcript)
            text = "\n".join(str(getattr(row, "text", "") or "").strip() for row in snippets if getattr(row, "text", ""))
            result.update({
                "status": "ok",
                "source": "youtube_transcript_api",
                "languages": languages,
                "segments": len(snippets),
                "text_chars": len(text),
                "text": text,
            })
        except Exception as exc:
            result.update({
                "status": "failed",
                "source": "youtube_transcript_api",
                "languages": languages,
                "error_code": type(exc).__name__,
                "error": str(exc),
                "text": "",
            })

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    timeout_s = float(os.environ.get("KR_YOUTUBE_TRANSCRIPT_TIMEOUT_S", "20"))
    thread.join(timeout_s)
    if thread.is_alive():
        log.warning(f"YouTube transcript fallback timed out after {timeout_s}s: {video_id}")
        return {
            "status": "timeout",
            "source": "youtube_transcript_api",
            "languages": languages,
            "error_code": "transcript_timeout",
            "error": f"youtube-transcript-api timed out after {timeout_s}s",
            "text": "",
        }
    return result


def _youtube_get(resource: str, params: Dict) -> Dict:
    url = f"{YOUTUBE_API_BASE}/{resource}"
    params = dict(params)
    api_key = str(params.pop("key", "") or "")
    proxy = get_httpx_proxy()
    timeout_s = float(os.environ.get("KR_YOUTUBE_API_TIMEOUT_S", "20"))
    retries = max(0, int(os.environ.get("KR_YOUTUBE_API_RETRIES", "2")))
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return _youtube_get_once(url, params, api_key=api_key, timeout_s=timeout_s, proxy=proxy)
        except _YouTubeRateLimitError:
            raise
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ProxyError) as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(min(4.0, 1.5 * (2**attempt)))
                continue
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError("YouTube API request failed")


def _youtube_operation_timeout_s() -> float:
    return float(os.environ.get("KR_YOUTUBE_OPERATION_TIMEOUT_S", "45"))


def _run_with_timeout(fn, *, timeout_s: float):
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(fn)
    try:
        return future.result(timeout=max(1.0, timeout_s))
    except concurrent.futures.TimeoutError as exc:
        future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        raise httpx.TimeoutException(f"YouTube operation timed out after {timeout_s}s") from exc
    finally:
        if future.done():
            executor.shutdown(wait=False, cancel_futures=True)


class _YouTubeRateLimitError(RuntimeError):
    pass


def _youtube_get_once(url: str, params: Dict, *, api_key: str, timeout_s: float, proxy: str | None) -> Dict:
    timeout = httpx.Timeout(timeout_s, connect=min(timeout_s, 3.0), read=timeout_s, write=min(timeout_s, 3.0), pool=min(timeout_s, 3.0))
    headers = {"X-Goog-Api-Key": api_key} if api_key else {}
    try:
        with httpx.Client(timeout=timeout, proxy=proxy, trust_env=True, headers=headers) as client:
            resp = client.get(url, params=params)
    except (httpx.TimeoutException, httpx.ConnectError, httpx.ProxyError) as exc:
        log.warning(f"代理不可用，已降级为直连: {exc}")
        with httpx.Client(timeout=timeout, trust_env=False, headers=headers) as client:
            resp = client.get(url, params=params)
    if resp.status_code >= 400:
        try:
            payload = resp.json()
        except Exception:
            payload = {}
        message = ((payload.get("error") or {}).get("message") if isinstance(payload, dict) else "") or resp.text[:300]
        if resp.status_code == 429:
            raise _YouTubeRateLimitError(f"YouTube API HTTP 429: {message}")
        raise RuntimeError(f"YouTube API HTTP {resp.status_code}: {message}")
    return resp.json()


def _youtube_error_type(exc: Exception) -> str:
    text = str(exc).lower()
    if isinstance(exc, _YouTubeRateLimitError) or "429" in text or "rate" in text or "quota" in text:
        return "rate_limited"
    if isinstance(exc, httpx.TimeoutException) or "timeout" in text or "timed out" in text:
        return "timeout"
    if isinstance(exc, (httpx.ConnectError, httpx.ProxyError)):
        return "network_error"
    return type(exc).__name__


def _best_thumbnail(thumbnails: Dict) -> str:
    for key in ("maxres", "standard", "high", "medium", "default"):
        item = thumbnails.get(key) if isinstance(thumbnails, dict) else None
        if isinstance(item, dict) and item.get("url"):
            return str(item.get("url"))
    return ""


def _int_or_zero(value) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _youtube_error(message: str, *, error_type: str = "request_failed", url: str = "") -> Dict:
    return {
        "items": [],
        "total": 0,
        "platform": "YouTube",
        "url": url,
        "error": {
            "error": message,
            "type": error_type,
            "retryable": error_type not in {"not_configured"},
            "hint": "Set YOUTUBE_API_KEY for YouTube Data API v3" if error_type == "not_configured" else "",
        },
    }
