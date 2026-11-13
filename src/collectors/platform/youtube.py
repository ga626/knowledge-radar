"""YouTube Data API v3 collection helpers."""

from __future__ import annotations

import concurrent.futures
import logging
import os
import re
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


def search_youtube(keyword: str, limit: int = 10, page_token: str = '') -> Dict:
    if not isinstance(page_token, str) or len(page_token) > 4096:
        raise ValueError('invalid_youtube_page_token')
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
                    **({'pageToken': page_token} if page_token else {}),
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
                "continuation": {
                    "status": ('cursor_not_advanced' if data.get('nextPageToken') == page_token and page_token
                               else 'more_available' if data.get('nextPageToken') else 'source_exhausted'),
                    "next_page_token": data.get('nextPageToken', ''),
                    "requested_page_token": page_token, "effective_limit": limit,
                    "scope": "same_query_and_filters_only",
                },
                "collection": {
                    "platform": "YouTube",
                    "strategy_tree": ["http_api", "youtube_data_api_v3", "search.list"],
                    "selected_strategy": "http_api",
                }
            },
        }
    except Exception as exc:
        error_type = _youtube_error_type(exc)
        return _youtube_error('YouTube rejected the page token; select a fresh continuation' if error_type == 'invalid_page_token' else str(exc), error_type=error_type)


def get_youtube_detail(video_id_or_url: str, *, include_transcript: bool = True, language: str | None = None) -> Dict:
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
            "duration_original": (item.get("contentDetails") or {}).get("duration", ""),
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
        from kr_core.content_contracts import duration_seconds
        detail["duration"] = duration_seconds(detail["duration_original"])
        detail["duration_unit"] = "seconds"
        captions = list_youtube_captions(video_id) if include_transcript else {"status": "skipped", "reason": "explicitly_disabled"}
        detail["captions"] = captions
        from runtime.asr_policy import AsrPolicy
        requested_language = AsrPolicy.for_request(language).language
        languages = [requested_language] if requested_language else None
        transcript = fetch_youtube_transcript(video_id, preferred_languages=languages) if include_transcript else {"text": "", "status": "skipped", "reason": "explicitly_disabled"}
        detail["transcript"] = transcript.get("text", "")
        detail["transcript_metadata"] = transcript
        return detail
    except Exception as exc:
        return _youtube_error(str(exc), error_type=_youtube_error_type(exc), url=canonical_youtube_url(video_id))


def list_youtube_captions(video_id: str) -> Dict:
    # captions.list需要OAuth且面向有权管理的视频；API Key不能代替授权。
    return {'status': 'authorization_required', 'reason': 'captions.list_requires_owner_oauth',
            'source': 'captions.list', 'tracks': []}


def fetch_youtube_transcript(video_id: str, preferred_languages: Optional[List[str]] = None) -> Dict:
    automatic = not preferred_languages
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
    # The library's default requests.Session has no HTTP timeout. A timed
    # thread.join merely abandoned that request, losing its failure stage.
    # Bound each real request and keep it inside the durable task process.
    from requests import Session, Timeout
    from urllib.parse import urlsplit

    started = time.monotonic()
    timeout_s = max(1.0, float(os.environ.get("KR_YOUTUBE_TRANSCRIPT_TIMEOUT_S", "20")))
    deadline = started + timeout_s
    requests_log = []

    class TranscriptSession(Session):
        def request(self, method, url, **kwargs):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Timeout("transcript overall request budget exhausted")
            kwargs["timeout"] = (min(3.0, remaining / 2), min(10.0, remaining / 2))
            stage = urlsplit(url).path
            event = {"stage": stage, "method": method}
            requests_log.append(event)
            request_started = time.monotonic()
            try:
                response = super().request(method, url, **kwargs)
                event["http_status"] = response.status_code
                return response
            except Exception as exc:
                event["error_code"] = type(exc).__name__
                raise
            finally:
                event["elapsed_s"] = round(time.monotonic() - request_started, 3)

    with TranscriptSession() as session:
        session.trust_env = False
        proxy = get_httpx_proxy()
        if proxy:
            session.proxies.update({"http": proxy, "https": proxy})
        try:
            api = YouTubeTranscriptApi(http_client=session)
            tracks = api.list(video_id)
            try:
                selected = tracks.find_transcript(languages)
            except Exception as exc:
                if type(exc).__name__ != 'NoTranscriptFound':
                    raise
                # 自动模式：首选中英未命中时读取现有原文字幕，不翻译、不重发探针。
                available = sorted(list(tracks), key=lambda track: bool(track.is_generated))
                families = {code.lower().replace('_', '-').split('-')[0] for code in languages}
                matching = [track for track in available
                            if track.language_code.lower().replace('_', '-').split('-')[0] in families]
                if matching:
                    selected = matching[0]
                elif automatic and available:
                    selected = available[0]
                else:
                    raise
            fetched = selected.fetch()
            transcript_language = getattr(fetched, 'language_code', '')
            snippets = list(fetched)
            text = "\n".join(str(row.text).strip() for row in snippets if getattr(row, "text", ""))
            from kr_core.evidence_locators import transcript_locators
            segments = [{'text': row.text.strip(), 'start_s': float(row.start),
                         'end_s': float(row.start + row.duration)} for row in snippets if getattr(row, 'text', '').strip()]
            mapping = transcript_locators(text, segments)
            result = {'status': 'ok', 'segments': len(snippets), 'text_chars': len(text), 'text': text,
                      'timestamped_segments': segments, 'locators': mapping,
                      'language': transcript_language}
        except Exception as exc:
            name = type(exc).__name__
            status = "timeout" if isinstance(exc, Timeout) else "failed"
            category = "network_timeout" if status == "timeout" else (
                "rate_limited_or_ip_blocked" if name in {"RequestBlocked", "IpBlocked", "TooManyRequests"}
                else "transcript_unavailable" if name in {"NoTranscriptFound", "TranscriptsDisabled", "VideoUnavailable"}
                else "transcript_request_failed")
            result = {"status": status, "error_code": name, "failure_type": category,
                      "error": f"{name}: {category}", "text": ""}
    return {**result, "source": "youtube_transcript_api", "languages": languages,
            "elapsed_s": round(time.monotonic() - started, 3), "timeout_s": timeout_s,
            "requests": requests_log, "proxy_configured": bool(proxy)}


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


class _YouTubePageTokenError(RuntimeError):
    pass


def _youtube_get_once(url: str, params: Dict, *, api_key: str, timeout_s: float, proxy: str | None) -> Dict:
    timeout = httpx.Timeout(timeout_s, connect=min(timeout_s, 3.0), read=timeout_s, write=min(timeout_s, 3.0), pool=min(timeout_s, 3.0))
    headers = {"X-Goog-Api-Key": api_key} if api_key else {}
    # 已选择的出口失败就报告该出口的错误；不得偷偷换直连重放请求。
    with httpx.Client(timeout=timeout, proxy=proxy, trust_env=True, headers=headers) as client:
        resp = client.get(url, params=params)
    if resp.status_code >= 400:
        try:
            payload = resp.json()
        except Exception:
            payload = {}
        if _invalid_page_token(payload):
            raise _YouTubePageTokenError('YouTube rejected the page token')
        message = ((payload.get("error") or {}).get("message") if isinstance(payload, dict) else "") or resp.text[:300]
        if resp.status_code == 429:
            raise _YouTubeRateLimitError(f"YouTube API HTTP 429: {message}")
        raise RuntimeError(f"YouTube API HTTP {resp.status_code}: {message}")
    return resp.json()


def _youtube_error_type(exc: Exception) -> str:
    if isinstance(exc, _YouTubePageTokenError):
        return 'invalid_page_token'
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            if _invalid_page_token(exc.response.json()):
                return 'invalid_page_token'
        except ValueError:
            pass
    text = str(exc).lower()
    if isinstance(exc, _YouTubeRateLimitError) or "429" in text or "rate" in text or "quota" in text:
        return "rate_limited"
    if isinstance(exc, httpx.TimeoutException) or "timeout" in text or "timed out" in text:
        return "timeout"
    if isinstance(exc, (httpx.ConnectError, httpx.ProxyError)):
        return "network_error"
    return type(exc).__name__


def _invalid_page_token(payload) -> bool:
    error = payload.get('error') if isinstance(payload, dict) else None
    errors = error.get('errors', []) if isinstance(error, dict) else []
    return isinstance(errors, list) and any(isinstance(item, dict) and item.get('reason') == 'invalidPageToken' for item in errors)


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
    result = {
        "items": [],
        "total": 0,
        "platform": "YouTube",
        "url": url,
        "error": {
            "error": message,
            "type": error_type,
            "retryable": error_type not in {"not_configured", "invalid_page_token"},
            "hint": "Set YOUTUBE_API_KEY for YouTube Data API v3" if error_type == "not_configured" else "",
        },
    }
    if error_type == 'invalid_page_token':
        result['metadata'] = {'continuation': {'status': 'cursor_rejected', 'source_exhausted': False}}
    return result


def collect_video_detail(request):
    detail = get_youtube_detail(request.url, include_transcript=bool(request.options.get('include_transcript')),
                                language=request.options.get('language'))
    if detail.get('error'):
        return detail
    video_id = detail['video_id']
    detail['content_id'] = video_id
    detail['media_manifest'] = [{'type': kind, 'url': canonical_youtube_url(video_id)} for kind in ('audio', 'video')]
    if request.options.get('include_comments', request.enable_comment_filtering):
        detail.update(collect_comments(video_id, options=request.options.get('comments')))
    return detail


def collect_comments(video_id, *, options=None):
    from runtime.read_options import CommentReadOptions, normalized_options
    from runtime.execution_context import check_execution, ExecutionStopped
    scope = normalized_options(CommentReadOptions, options)
    rows, gaps, replies = [], [], []
    pages, roots, token, exhausted = 0, 0, '', False
    seen = set()
    def get(resource, params):
        nonlocal pages
        check_execution()
        if pages >= scope['max_pages']:
            raise ValueError('comment_page_budget')
        pages += 1
        return _youtube_get(resource, {**params, 'key': youtube_api_key()})
    def row(item, root=None):
        snippet = item.get('snippet') or {}
        identifier = str(item['id'])
        picture = snippet.get('imageUrl')
        images = ([picture] if isinstance(picture, str) else list(picture or []) if isinstance(picture, list) else [])
        return {'comment_id': identifier, 'content': snippet.get('textOriginal', snippet.get('textDisplay', '')),
                'author': snippet.get('authorDisplayName', ''), 'created_at': snippet.get('publishedAt'),
                'updated_at': snippet.get('updatedAt'), 'likes': snippet.get('likeCount'),
                'parent_comment_id': str(snippet.get('parentId') or root) if root else None,
                'root_comment_id': root or identifier, 'is_reply': bool(root), 'images': images,
                'relationship_confidence': 'native_parent' if root else 'root',
                'media_provenance': {'image_url_ttl_s': 21600, 'source': 'CommentSnippet.imageUrl',
                                     'audio_video': 'not_exposed_by_comment_api'}}
    try:
        if not youtube_api_key():
            raise ValueError('youtube_api_key_missing')
        while roots < scope['root_limit'] and len(rows) < scope['total_limit']:
            data = get('commentThreads', {'part': 'snippet', 'videoId': video_id, 'textFormat': 'plainText',
                'maxResults': min(100, scope['root_limit']-roots, scope['total_limit']-len(rows)),
                'order': scope['order'], **({'pageToken': token} if token else {})})
            page_truncated = False
            for thread in data.get('items') or []:
                top = thread['snippet']['topLevelComment']
                if roots >= scope['root_limit'] or len(rows) >= scope['total_limit']:
                    page_truncated = True
                    break
                if str(top['id']) in seen:
                    continue
                seen.add(str(top['id']))
                rows.append(row(top)); roots += 1
                if thread['snippet'].get('totalReplyCount', 0):
                    replies.append((str(top['id']), int(thread['snippet']['totalReplyCount'])))
            token = data.get('nextPageToken', '')
            exhausted = not token and not page_truncated
            if exhausted:
                break
        reply_coverage = []
        for parent, total in replies:
            got, next_token = 0, ''
            while got < min(total, scope['reply_limit']) and len(rows) < scope['total_limit']:
                data = get('comments', {'part': 'snippet', 'parentId': parent, 'textFormat': 'plainText',
                    'maxResults': min(100, scope['reply_limit']-got, scope['total_limit']-len(rows)),
                    **({'pageToken': next_token} if next_token else {})})
                for child in data.get('items') or []:
                    if got >= min(total, scope['reply_limit']) or len(rows) >= scope['total_limit']:
                        break
                    if str(child['id']) in seen:
                        continue
                    seen.add(str(child['id']))
                    rows.append(row(child, parent)); got += 1
                next_token = data.get('nextPageToken', '')
                if not next_token:
                    break
            reply_coverage.append({'root_comment_id': parent, 'observed_total': total, 'returned': got,
                'requested_window_complete': got >= min(total, scope['reply_limit']),
                'exhausted': got >= total and not next_token, 'has_more': got < total or bool(next_token)})
        satisfied = (roots >= scope['root_limit'] or exhausted) and all(r['requested_window_complete'] for r in reply_coverage)
        source_done = exhausted and all(r['exhausted'] for r in reply_coverage)
    except ExecutionStopped:
        raise
    except Exception as exc:
        gaps.append({'reason': str(exc) if isinstance(exc, ValueError) else _youtube_error_type(exc),
                     'layer': 'youtube_comments', 'automatic_retry': False})
        satisfied, source_done = False, False
        reply_coverage = locals().get('reply_coverage', [])
    return {'comments': rows, 'comments_complete': satisfied, 'comments_exhausted': source_done,
            'comments_empty_confirmed': source_done and not rows,
            'comments_coverage': 'observed_zero_comments' if source_done and not rows else 'bounded_api_window',
            'comments_metadata': {'pages': pages, 'actual_order':scope['order'], 'returned_roots': roots, 'reply_coverage': reply_coverage,
                'gaps': gaps, 'next_root_token': token, 'stop_reason': gaps[0]['reason'] if gaps else 'source_exhausted' if source_done else 'requested_window',
                'relationship_scope': 'api_parent_is_top_level; UI_mentions_do_not_define_deeper_parent'}}


def deep_analyze_youtube(video_id, basic_info, research_session_id='', scope_options=None):
    from runtime.video_tasks import submit_video_detail
    return submit_video_detail('YouTube', canonical_youtube_url(video_id),
        research_session_id=research_session_id, scope=(scope_options or {}).get('task_scope'), deep=True)
