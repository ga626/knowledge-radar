"""Bilibili collection helpers migrated out of server.py."""

from __future__ import annotations

import hashlib
import glob
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional

import httpx
from kr_core.collection import CollectionTrace, format_search_error, format_search_response
from runtime.asr_policy import AsrPolicy
from runtime.media_cache import media_cache_subdir, record_media_cache_entry
from runtime.task_adapter import LocalTaskAdapter, LocalTaskSpec
from runtime.task_scope import make_task_scope, merge_scope_metadata
from runtime.tasks import compact_task_ref, get_task_store

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
log = logging.getLogger("mcp-server")
L2_MULTIMODAL_SCHEMA_VERSION = "knowledgeradar-l2-video-analysis/v1"
L2_ANALYSIS_STRATEGY = "bilibili_qwen_video_analysis"
L2_MODEL_VERSION = os.environ.get("KR_L2_VIDEO_MODEL_VERSION", "qwen-vl-default-2026-05")

BILI_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://search.bilibili.com/",
}


def extract_bvid(url: str) -> Optional[str]:
    """从 URL 或文本中提取 BVID，兼容 av/aid 形式。"""
    m = re.search(r"BV[a-zA-Z0-9]{10,12}", url)
    if m:
        return m.group(0)
    aid_match = re.search(r"(?:av|aid=)(\d+)", url, flags=re.IGNORECASE)
    if not aid_match:
        return None
    try:
        resp = httpx.get(
            "https://api.bilibili.com/x/web-interface/view",
            params={"aid": aid_match.group(1)},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 0:
            bvid = (data.get("data") or {}).get("bvid")
            return bvid if isinstance(bvid, str) and bvid.startswith("BV") else None
    except Exception as e:
        log.warning(f"从 AV 号转换 BVID 失败: {e}")
    return None


def legacy_search_bilibili(keyword: str, page_size: int = 10) -> Dict:
    log.info(f"search_bilibili: {keyword}, page_size={page_size}")
    trace = CollectionTrace("B站", ["http_api", "wbi_signature"])
    try:
        resp = httpx.get(
            "https://api.bilibili.com/x/web-interface/search/all/v2",
            params={"keyword": keyword, "page": 1},
            headers=BILI_HEADERS,
            timeout=15,
        )
        if resp.status_code == 429:
            trace.add("http_api", "failed", detail="HTTP 429", error_type="rate_limited", retryable=True)
            return _format_search_error("B站", {"error": "B站搜索被限流 (HTTP 429)", "retryable": True, "hint": "稍后重试或降低请求频率"}, trace=trace, strategy="http_api")
        if resp.status_code >= 400:
            trace.add("http_api", "failed", detail=f"HTTP {resp.status_code}", error_type="anti_bot", retryable=True)
            return _format_search_error("B站", {"error": f"B站搜索被风控/拦截 (HTTP {resp.status_code})", "retryable": True, "hint": "稍后重试或更换网络环境"}, trace=trace, strategy="http_api")
        data = resp.json()
        if data.get("code") != 0:
            trace.add("http_api", "failed", detail=str(data.get("message", "")), error_type="request_failed", retryable=True)
            return _format_search_error("B站", {"error": f"B站 API 返回错误: {data.get('message', '')}"}, trace=trace, strategy="http_api")

        items: List[Dict] = []
        for block in (data.get("data") or {}).get("result") or []:
            block_type = block.get("result_type") or block.get("type") or ""
            if block_type != "video":
                continue
            block_items = block.get("data") or []
            if isinstance(block_items, dict):
                block_items = [block_items]
            for item in block_items:
                if not isinstance(item, dict):
                    continue
                bvid = item.get("bvid") or extract_bvid(item.get("url", "")) or extract_bvid(item.get("arcurl", "")) or ""
                if not bvid:
                    continue
                title = re.sub(r"<[^>]+>", "", str(item.get("title", "")))
                url = item.get("url") or item.get("arcurl") or f"https://www.bilibili.com/video/{bvid}"
                if url.startswith("//"):
                    url = "https:" + url
                elif url.startswith("http://www.bilibili.com/"):
                    url = url.replace("http://", "https://", 1)
                items.append(
                    {
                        "title": title,
                        "url": url,
                        "author": item.get("author", ""),
                        "desc": (item.get("description") or item.get("desc") or "")[:200],
                        "platform": "B站",
                        "play": item.get("play", 0),
                        "like": item.get("like", 0),
                        "reply": item.get("review", item.get("reply", 0)),
                        "favorite": item.get("favorites", item.get("favorite", 0)),
                        "duration": item.get("duration", ""),
                        "published_at": item.get("pubdate", ""),
                        "metadata": {
                            "bvid": bvid,
                            "type": item.get("type", block_type),
                            "source": "bilibili_search",
                            "duration": item.get("duration", ""),
                            "pubdate": item.get("pubdate", ""),
                        },
                    }
                )
                if len(items) >= page_size:
                    break
            if len(items) >= page_size:
                break
        trace.add("http_api", "ok", item_count=len(items))
        return _format_search_response("B站", items, trace=trace)
    except httpx.TimeoutException:
        log.error("B站搜索超时")
        trace.add("http_api", "failed", detail="timeout", error_type="request_failed", retryable=True)
        return _format_search_error("B站", {"error": "B站搜索请求超时", "retryable": True, "hint": "网络慢或平台响应慢，稍后重试"}, trace=trace, strategy="http_api")
    except httpx.RequestError as e:
        log.error(f"B站搜索网络连接失败: {e}")
        trace.add("http_api", "failed", detail=str(e), error_type="request_failed", retryable=True)
        return _format_search_error("B站", {"error": f"B站搜索网络连接失败: {e}", "retryable": True}, trace=trace, strategy="http_api")
    except Exception as e:
        log.error(f"B站搜索失败: {e}")
        trace.add("http_api", "failed", detail=str(e), error_type="request_failed", retryable=True)
        return _format_search_error("B站", {"error": f"B站搜索异常: {str(e)}"}, trace=trace, strategy="http_api")


def get_bilibili_info(bvid: str) -> Optional[Dict]:
    """获取B站视频基本信息"""
    try:
        resp = httpx.get(
            f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"},
            timeout=15,
        )
        data = resp.json()
        if data.get("code") == 0:
            d = data["data"]
            stat = d.get("stat", {}) or {}
            return {
                "title": d.get("title", ""),
                "desc": d.get("desc", "")[:300],
                "author": d.get("owner", {}).get("name", ""),
                "duration": d.get("duration", 0),
                "cover": d.get("pic", ""),
                "aid": d.get("aid", 0),
                "cid": d.get("cid", 0),
                "stats": stat,
                "video_play_count": stat.get("view", 0),
                "liked_count": stat.get("like", 0),
                "video_coin_count": stat.get("coin", 0),
                "video_favorite_count": stat.get("favorite", 0),
                "video_share_count": stat.get("share", 0),
                "video_danmaku": stat.get("danmaku", 0),
                "video_comment": stat.get("reply", 0),
            }
    except Exception as e:
        log.warning(f"获取视频信息失败: {e}")
    return None


def _empty_asr_timing() -> Dict[str, float]:
    return {
        "subtitle_probe_s": 0.0,
        "download_s": 0.0,
        "model_load_s": 0.0,
        "transcribe_s": 0.0,
        "total_s": 0.0,
    }


def _task_scope_metadata(
    *,
    source_url: str,
    content_id: str,
    platform: str = "B站",
    scope_kind: str = "detail_request",
    research_session_id: str = "",
    scope_metadata: Dict | None = None,
) -> Dict:
    existing = dict(scope_metadata or {})
    scope = make_task_scope(
        source_url=existing.get("source_url") or source_url,
        content_id=existing.get("content_id") or content_id,
        platform=platform,
        scope_kind=existing.get("scope_kind") or scope_kind,
        work_scope_id=existing.get("work_scope_id") or "",
        task_scope_id=existing.get("task_scope_id") or "",
        research_session_id=research_session_id or existing.get("research_session_id_alias") or existing.get("research_session_id") or "",
    )
    return merge_scope_metadata(scope, existing)


def _asr_task_metadata(
    bvid: str,
    *,
    research_session_id: str = "",
    scope_metadata: Dict | None = None,
    source_url: str = "",
    result_path: str = "",
    timing: Dict[str, float] | None = None,
    **extra: object,
) -> Dict:
    policy = AsrPolicy.from_env()
    metadata: Dict[str, object] = {
        "bvid": bvid,
        "source_url": source_url or f"https://www.bilibili.com/video/{bvid}",
        "approach": "derived_text",
        "blocks_final_report": True,
        "result_reread_tool": "get_content_detail",
        "result_path": result_path,
        **policy.compact(),
    }
    metadata = merge_scope_metadata(
        _task_scope_metadata(
            source_url=str(metadata["source_url"]),
            content_id=bvid,
            research_session_id=research_session_id,
            scope_metadata=scope_metadata,
        ),
        metadata,
    )
    if research_session_id:
        metadata["research_session_id"] = research_session_id
    metadata.update(timing or _empty_asr_timing())
    metadata.update({key: value for key, value in extra.items() if value is not None})
    return metadata


def _write_transcript_cache(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _read_transcript_cache(path: str) -> str:
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _probe_bilibili_subtitle(bvid: str, *, output_dir: str, task_id: str) -> Dict:
    started = time.time()
    info = get_bilibili_info(bvid) or {}
    cid = info.get("cid")
    if not cid:
        return {"hit": False, "duration_s": round(time.time() - started, 3), "reason": "missing_cid"}
    try:
        player_resp = httpx.get(
            "https://api.bilibili.com/x/player/v2",
            params={"bvid": bvid, "cid": cid},
            headers={"User-Agent": "Mozilla/5.0", "Referer": f"https://www.bilibili.com/video/{bvid}"},
            timeout=10,
        )
        player = player_resp.json()
        subtitles = (((player.get("data") or {}).get("subtitle") or {}).get("subtitles") or [])
        if not subtitles:
            return {"hit": False, "duration_s": round(time.time() - started, 3), "reason": "no_subtitles"}
        subtitle = subtitles[0] or {}
        subtitle_url = str(subtitle.get("subtitle_url") or "")
        if subtitle_url.startswith("//"):
            subtitle_url = "https:" + subtitle_url
        if not subtitle_url:
            return {"hit": False, "duration_s": round(time.time() - started, 3), "reason": "missing_subtitle_url"}
        subtitle_resp = httpx.get(
            subtitle_url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": f"https://www.bilibili.com/video/{bvid}"},
            timeout=15,
        )
        subtitle_data = subtitle_resp.json()
        lines = []
        for item in subtitle_data.get("body") or []:
            content = str(item.get("content") or "").strip()
            if content:
                lines.append(content)
        text = "\n".join(lines).strip()
        if not text:
            return {"hit": False, "duration_s": round(time.time() - started, 3), "reason": "empty_subtitle"}
        subtitle_path = os.path.join(output_dir, f"{bvid}_subtitle.json")
        try:
            with open(subtitle_path, "w", encoding="utf-8") as f:
                json.dump(subtitle_data, f, ensure_ascii=False)
            record_media_cache_entry(
                subtitle_path,
                kind="subtitle",
                content_id=bvid,
                task_id=task_id,
                source_url=f"https://www.bilibili.com/video/{bvid}",
                metadata={"subtitle_url": subtitle_url, "line_count": len(lines)},
            )
        except Exception:
            pass
        return {
            "hit": True,
            "text": text,
            "duration_s": round(time.time() - started, 3),
            "subtitle_url": subtitle_url,
            "line_count": len(lines),
        }
    except Exception as exc:
        return {
            "hit": False,
            "duration_s": round(time.time() - started, 3),
            "reason": str(exc)[:160],
        }


def transcribe_bilibili(
    bvid: str,
    output_dir: str = "",
    research_session_id: str = "",
    scope_metadata: Dict | None = None,
) -> str:
    """B站视频语音转写，返回文本（下载和转录都后台执行，避免 MCP 超时）"""
    try:
        started_total = time.time()
        task_store = get_task_store()
        task_id = f"bilibili_transcribe_{bvid}"
        if not output_dir:
            output_dir = str(media_cache_subdir("transcripts", content_id=bvid, task_id=task_id))
        os.makedirs(output_dir, exist_ok=True)
        cache_path = os.path.join(output_dir, f"{bvid}_transcript.txt")
        cache_m4a = os.path.join(output_dir, f"{bvid}.m4a")
        in_progress_path = os.path.join(output_dir, f"{bvid}_transcript.inprogress")

        def _find_existing_audio() -> str:
            candidates = [cache_m4a]
            candidates.extend(sorted(glob.glob(os.path.join(output_dir, f"{bvid}_p*.m4a"))))
            existing = [path for path in candidates if os.path.exists(path) and os.path.getsize(path) > 0]
            if not existing:
                return ""
            p1_audio = os.path.join(output_dir, f"{bvid}_p1.m4a")
            if p1_audio in existing:
                return p1_audio
            return existing[0]

        cached_text = _read_transcript_cache(cache_path)
        if cached_text:
            timing = _empty_asr_timing()
            timing["total_s"] = round(time.time() - started_total, 3)
            record_media_cache_entry(
                cache_path,
                kind="transcript",
                content_id=bvid,
                task_id=task_id,
                source_url=f"https://www.bilibili.com/video/{bvid}",
                metadata={"cache_hit": True},
            )
            task_store.upsert_task(
                task_id=task_id,
                task_type="bilibili_transcribe",
                platform="B站",
                target=bvid,
                content_id=bvid,
                status="completed",
                result_path=cache_path,
                metadata=_asr_task_metadata(
                    bvid,
                    research_session_id=research_session_id,
                    scope_metadata=scope_metadata,
                    result_path=cache_path,
                    timing=timing,
                    cache_hit=True,
                    subtitle_hit=False,
                    phase="cache_hit",
                ),
            )
            task_store.mark_completed(task_id, result_path=cache_path, metadata={"cache_hit": True, **timing})
            return cached_text

        subtitle = _probe_bilibili_subtitle(bvid, output_dir=output_dir, task_id=task_id)
        if subtitle.get("hit"):
            txt = str(subtitle.get("text") or "")
            _write_transcript_cache(cache_path, txt)
            timing = _empty_asr_timing()
            timing["subtitle_probe_s"] = float(subtitle.get("duration_s") or 0)
            timing["total_s"] = round(time.time() - started_total, 3)
            record_media_cache_entry(
                cache_path,
                kind="transcript",
                content_id=bvid,
                task_id=task_id,
                source_url=f"https://www.bilibili.com/video/{bvid}",
                metadata={"subtitle_hit": True, "transcript_chars": len(txt), "line_count": subtitle.get("line_count")},
            )
            task_store.upsert_task(
                task_id=task_id,
                task_type="bilibili_transcribe",
                platform="B站",
                target=bvid,
                content_id=bvid,
                status="completed",
                result_path=cache_path,
                metadata=_asr_task_metadata(
                    bvid,
                    research_session_id=research_session_id,
                    scope_metadata=scope_metadata,
                    result_path=cache_path,
                    timing=timing,
                    cache_hit=False,
                    subtitle_hit=True,
                    phase="subtitle_hit",
                    transcript_chars=len(txt),
                    subtitle_url=subtitle.get("subtitle_url"),
                ),
            )
            task_store.mark_completed(task_id, result_path=cache_path, metadata={"subtitle_hit": True, "transcript_chars": len(txt), **timing})
            return txt

        if os.path.exists(in_progress_path):
            # Stale detection: if .inprogress file is old, the background
            # thread was likely killed by MCP server restart or one-shot validation.
            # the background thread was likely killed by MCP server restart.
            STALE_SECONDS = int(os.environ.get("KR_BILIBILI_TRANSCRIBE_STALE_SECONDS", "600"))
            try:
                in_progress_age = time.time() - os.path.getmtime(in_progress_path)
                if in_progress_age > STALE_SECONDS:
                    log.warning(f"[transcribe] Stale .inprogress detected ({in_progress_age:.0f}s old), cleaning up: {bvid}")
                    try:
                        os.remove(in_progress_path)
                    except Exception:
                        pass
                    # Also clean up partial audio download
                    for suffix in [".part", ".m4a.part", ".p1.m4a.part"]:
                        partial = cache_m4a + suffix
                        if os.path.exists(partial):
                            try:
                                os.remove(partial)
                            except Exception:
                                pass
                    # Mark the stale task as failed
                    task_store.mark_failed(task_id, error="stale: background thread killed by server restart", metadata={"bvid": bvid, "stale_age_s": in_progress_age})
                    # Fall through to start a new transcription
                else:
                    task_store.upsert_task(
                        task_id=task_id,
                        task_type="bilibili_transcribe",
                        platform="B站",
                        target=bvid,
                        content_id=bvid,
                        status="running",
                        result_path=cache_path,
                        metadata=_asr_task_metadata(
                            bvid,
                            research_session_id=research_session_id,
                            scope_metadata=scope_metadata,
                            result_path=cache_path,
                            subtitle_probe_s=float(subtitle.get("duration_s") or 0),
                            in_progress_path=in_progress_path,
                            phase="running",
                        ),
                    )
                    return ("[transcribe] 语音转写已在后台处理中，"
                            f"完成后可重新调用 get_content_detail 获取转写文本")
            except Exception:
                # If we can't check the file age, treat as stale
                try:
                    os.remove(in_progress_path)
                except Exception:
                    pass

        task_store.upsert_task(
            task_id=task_id,
            task_type="bilibili_transcribe",
            platform="B站",
            target=bvid,
            content_id=bvid,
            status="queued",
            result_path=cache_path,
            metadata=_asr_task_metadata(
                bvid,
                research_session_id=research_session_id,
                scope_metadata=scope_metadata,
                result_path=cache_path,
                subtitle_probe_s=float(subtitle.get("duration_s") or 0),
                audio_path=cache_m4a,
                in_progress_path=in_progress_path,
                phase="queued",
            ),
        )

        def _do_transcribe():
            timing = _empty_asr_timing()
            timing["subtitle_probe_s"] = float(subtitle.get("duration_s") or 0)
            task_started = time.time()
            try:
                policy = AsrPolicy.from_env()
                task_store.mark_running(task_id, metadata={**policy.compact(), **timing, "phase": "running"})
                task_store.heartbeat(task_id, metadata={"phase": "download"})
                with open(in_progress_path, "w", encoding="utf-8") as f:
                    f.write(str(time.time()))
                from yt_dlp import YoutubeDL
                existing_audio = _find_existing_audio()
                if not existing_audio:
                    log.info(f"[bg] 音频下载开始: {bvid}")
                    download_started = time.time()
                    ydl_opts = {
                        "format": "bestaudio/best",
                        "outtmpl": os.path.join(output_dir, "%(id)s.%(ext)s"),
                        "noplaylist": True,
                        "playlist_items": "1",
                        "http_headers": {
                            "Origin": "https://www.bilibili.com",
                            "Referer": "https://www.bilibili.com/",
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36",
                        },
                        "quiet": True,
                        "no_warnings": True,
                    }
                    with YoutubeDL(ydl_opts) as ydl:
                        ydl.extract_info(f"https://www.bilibili.com/video/{bvid}", download=True)
                    timing["download_s"] = round(time.time() - download_started, 3)
                existing_audio = _find_existing_audio()
                if existing_audio:
                    log.info(f"[bg] 音频文件就绪: {existing_audio}")
                    record_media_cache_entry(
                        existing_audio,
                        kind="audio",
                        content_id=bvid,
                        task_id=task_id,
                        source_url=f"https://www.bilibili.com/video/{bvid}",
                    )
                task_store.heartbeat(task_id, metadata={"phase": "transcribe"})
                log.info(f"[bg] 转录开始: {bvid}")
                os.environ.setdefault("PYTHONIOENCODING", "utf-8")
                if hasattr(sys.stdout, "reconfigure"):
                    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
                if hasattr(sys.stderr, "reconfigure"):
                    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
                if not existing_audio:
                    raise FileNotFoundError(f"Audio download finished but no audio file found for {bvid}")
                from transcribe_bilibili import transcribe as transcribe_audio
                result = transcribe_audio(existing_audio, model_size=policy.primary_model, policy=policy)
                txt = result.get("txt", "")
                timing["download_s"] = round(float(result.get("download_s") or timing["download_s"]), 3)
                timing["model_load_s"] = round(float(result.get("model_load_s") or 0), 3)
                timing["transcribe_s"] = round(float(result.get("transcribe_s") or result.get("duration_s") or 0), 3)
                timing["total_s"] = round(time.time() - task_started, 3)
                if txt and not os.path.exists(cache_path):
                    with open(cache_path, "w", encoding="utf-8") as f:
                        f.write(txt)
                srt = result.get("srt", "")
                if srt:
                    with open(os.path.join(output_dir, f"{bvid}_transcript.srt"), "w", encoding="utf-8") as f:
                        f.write(srt)
                if txt:
                    record_media_cache_entry(
                        cache_path,
                        kind="transcript",
                        content_id=bvid,
                        task_id=task_id,
                        source_url=f"https://www.bilibili.com/video/{bvid}",
                        metadata={"transcript_chars": len(txt)},
                    )
                task_store.mark_completed(
                    task_id,
                    result_path=cache_path,
                    metadata={
                        "bvid": bvid,
                        "transcript_chars": len(txt),
                        "cache_hit": False,
                        "subtitle_hit": False,
                        "phase": "completed",
                        **policy.compact(),
                        **timing,
                    },
                )
                log.info(f"[bg] 转录完成: {bvid} ({len(txt)} chars)")
            except Exception as e:
                timing["total_s"] = round(time.time() - task_started, 3)
                task_store.mark_failed(task_id, error=str(e), result_path=cache_path, metadata={"bvid": bvid, **timing})
                log.error(f"[bg] 转录失败: {bvid}: {e}")
            finally:
                try:
                    if os.path.exists(in_progress_path):
                        os.remove(in_progress_path)
                except Exception:
                    pass

        LocalTaskAdapter(task_store).submit_lifecycle(
            LocalTaskSpec(
                task_id=task_id,
                task_type="bilibili_transcribe",
                platform="B站",
                target=bvid,
                content_id=bvid,
                result_path=cache_path,
                max_attempts=1,
                timeout_s=float(os.environ.get("KR_ASR_TASK_TIMEOUT_SECONDS", "900")),
                metadata=_asr_task_metadata(
                    bvid,
                    research_session_id=research_session_id,
                    scope_metadata=scope_metadata,
                    result_path=cache_path,
                    subtitle_probe_s=float(subtitle.get("duration_s") or 0),
                    audio_path=cache_m4a,
                    in_progress_path=in_progress_path,
                    phase="queued",
                ),
            ),
            _do_transcribe,
        )

        return ("[transcribe] 语音下载和转写已在后台启动（约1-2分钟），"
                f"完成后可重新调用 get_content_detail 获取转写文本")
    except Exception as e:
        log.warning(f"转写失败: {e}")
        return f"[transcribe] 转写失败: {str(e)}"


def get_bilibili_comments(bvid: str, limit: int = 20) -> List[Dict]:
    """获取B站视频评论"""
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}
    try:
        resp = httpx.get(
            f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
            headers=headers, timeout=15,
        )
        data = resp.json()
        if data.get("code") != 0:
            return []
        aid = data["data"]["aid"]

        all_comments = []
        seen = set()
        for mode in [3, 2]:
            resp2 = httpx.get(
                f"https://api.bilibili.com/x/v2/reply/main?oid={aid}&type=1&mode={mode}&ps=5",
                headers=headers, timeout=15,
            )
            d2 = resp2.json()
            if d2.get("code") == 0:
                for r in d2.get("data", {}).get("replies", []):
                    msg = r.get("content", {}).get("message", "").strip()
                    if msg and msg not in seen:
                        seen.add(msg)
                        all_comments.append({
                            "user": r.get("member", {}).get("uname", ""),
                            "content": msg,
                            "likes": r.get("like", 0),
                        })
                    if len(all_comments) >= limit:
                        break
            if len(all_comments) >= limit:
                break
        return all_comments
    except Exception as e:
        log.warning(f"获取评论失败: {e}")
        return []


def filter_bilibili_comments(
    comments: List[Dict],
    output_dir: str = "",
    scope_options: Dict | None = None,
    bvid: str = "",
    source_url: str = "",
) -> List[Dict]:
    """评论知识价值过滤（后台异步 + 缓存，避免 MCP 超时）"""
    if not comments:
        return []

    if not output_dir:
        output_dir = str(media_cache_subdir("comments"))
    os.makedirs(output_dir, exist_ok=True)
    try:
        content_sig = hashlib.md5(json.dumps(comments, ensure_ascii=False).encode()).hexdigest()[:16]
    except Exception:
        content_sig = str(len(comments))
    cache_path = os.path.join(output_dir, f"filtered_{content_sig}.json")

    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
                log.info(f"评论过滤: 缓存命中, 返回 {len(cached)} 条")
                return cached
        except Exception:
            pass

    import time as _time
    result_container = {"done": False, "data": None}
    task_store = get_task_store()
    task_id = f"bilibili_comment_filter_{content_sig}"
    scope_metadata = (scope_options or {}).get("task_scope") if isinstance(scope_options, dict) else None
    task_source_url = source_url or (f"https://www.bilibili.com/video/{bvid}" if bvid else "")
    task_content_id = bvid or content_sig
    task_scope = _task_scope_metadata(
        source_url=task_source_url,
        content_id=task_content_id,
        scope_metadata=scope_metadata if isinstance(scope_metadata, dict) else None,
    )

    def _do_filter():
        try:
            task_store.mark_running(task_id, metadata={"phase": "running"})
            sys.path.insert(0, PROJECT_ROOT)
            from filter_comments import filter_valuable_comments
            log.info(f"[bg] 评论过滤开始: {len(comments)} 条")
            r = filter_valuable_comments(comments, verbose=False)
            kept = r.get("kept_comments", [])
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(kept, f, ensure_ascii=False, indent=2)
            record_media_cache_entry(
                cache_path,
                kind="comments",
                content_id=task_content_id,
                metadata={"comment_count": len(comments), "kept_count": len(kept)},
            )
            result_container["data"] = kept
            task_store.mark_completed(
                task_id,
                result_path=cache_path,
                metadata={"phase": "completed", "kept_count": len(kept), "comment_count": len(comments)},
            )
            log.info(f"[bg] 评论过滤完成: {len(kept)}/{len(comments)} 条")
        except Exception as e:
            import traceback
            log.error(f"[bg] 评论过滤异常: {e} | {traceback.format_exc()}")
            degraded = [{"user": c.get("user", ""), "content": c.get("content", ""),
                         "likes": c.get("likes", 0), "verdict": "unknown", "reason": "过滤异常降级"}
                        for c in comments]
            try:
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(degraded, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
            result_container["data"] = degraded
            task_store.mark_failed(
                task_id,
                error=str(e),
                result_path=cache_path,
                metadata={"phase": "failed", "comment_count": len(comments)},
            )
        finally:
            result_container["done"] = True

    LocalTaskAdapter(task_store).submit_lifecycle(
        LocalTaskSpec(
            task_id=task_id,
            task_type="bilibili_comment_filter",
            platform="B站",
            target=content_sig,
            content_id=task_content_id,
            result_path=cache_path,
            max_attempts=1,
            timeout_s=60.0,
            metadata={
                **task_scope,
                "comment_count": len(comments),
                "cache_path": cache_path,
                "blocks_final_report": False,
                "result_reread_tool": "get_content_detail",
                "approach": "basic_text",
            },
        ),
        _do_filter,
    )

    timeout = 20
    t0 = _time.time()
    while not result_container["done"] and _time.time() - t0 < timeout:
        _time.sleep(0.5)

    if result_container["done"] and result_container["data"] is not None:
        return result_container["data"]

    log.warning(f"评论过滤超时（>{timeout}s），返回原始评论，后台继续处理")
    return comments[:]


def deep_analyze_bilibili(
    bvid: str,
    basic_info: Dict,
    research_session_id: str = "",
    scope_options: Dict | None = None,
) -> Dict:
    """Qwen/SiliconFlow 多模态深度视频分析（后台异步模式）"""
    return _deep_analyze_video_platform(
        platform="B站",
        video_key=bvid,
        video_url=f"https://www.bilibili.com/video/{bvid}",
        basic_info=basic_info,
        task_type="bilibili_qwen_video_analysis",
        strategy=L2_ANALYSIS_STRATEGY,
        result_prefix="l2_bilibili_video",
        research_session_id=research_session_id,
        scope_metadata=(scope_options or {}).get("task_scope") if isinstance(scope_options, dict) else None,
    )


def deep_analyze_youtube(
    video_id: str,
    basic_info: Dict,
    research_session_id: str = "",
    scope_options: Dict | None = None,
) -> Dict:
    """Qwen/SiliconFlow YouTube video analysis through the shared L2 pipeline."""
    return _deep_analyze_video_platform(
        platform="YouTube",
        video_key=video_id,
        video_url=f"https://www.youtube.com/watch?v={video_id}",
        basic_info=basic_info,
        task_type="youtube_qwen_video_analysis",
        strategy="youtube_qwen_video_analysis",
        result_prefix="l2_youtube_video",
        research_session_id=research_session_id,
        scope_metadata=(scope_options or {}).get("task_scope") if isinstance(scope_options, dict) else None,
    )


def _deep_analyze_video_platform(
    *,
    platform: str,
    video_key: str,
    video_url: str,
    basic_info: Dict,
    task_type: str,
    strategy: str,
    result_prefix: str,
    research_session_id: str = "",
    scope_metadata: Dict | None = None,
) -> Dict:
    content_id = _l2_content_fingerprint(video_key, basic_info, strategy=strategy)
    task_id = f"{result_prefix}_{content_id}"
    results_dir = str(media_cache_subdir("analysis_results", content_id=content_id, task_id=task_id))
    result_path = os.path.join(results_dir, f"{content_id}_analysis.json")
    task_store = get_task_store()

    if os.path.exists(result_path):
        try:
            with open(result_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
                cached.setdefault("schema_version", L2_MULTIMODAL_SCHEMA_VERSION)
                cached.setdefault("model_version", L2_MODEL_VERSION)
                cached.setdefault("content_id", content_id)
                cached["from_cache"] = True
                task_store.upsert_task(
                    task_id=task_id,
                    task_type=task_type,
                    platform=platform,
                    target=video_key,
                    content_id=content_id,
                    status="completed",
                    result_path=result_path,
                    metadata=_l2_task_metadata(
                        video_key,
                        basic_info,
                        content_id,
                        strategy=strategy,
                        cache_hit=True,
                        research_session_id=research_session_id,
                        scope_metadata=scope_metadata,
                    ),
                )
                task_store.mark_completed(task_id, result_path=result_path, metadata={"cache_hit": True})
                return cached
        except Exception:
            pass

    existing = task_store.get_task(task_id)
    if existing and existing.get("status") in {"queued", "running"}:
        return _l2_processing_response(task_id, content_id, result_path, existing)

    def _run_analysis():
        task_store.heartbeat(task_id, metadata={"phase": "analysis_start"})
        log.info(f"[bg] 千问多模态分析开始: {platform} {video_key}")
        sys.path.insert(0, PROJECT_ROOT)
        from video_processor import VideoInfo, VideoProcessor

        video = VideoInfo(
            title=basic_info.get("title", ""),
            description=basic_info.get("desc", ""),
            transcript=basic_info.get("transcript", ""),
            filtered_comments=basic_info.get("filtered_comments", []),
            duration_seconds=basic_info.get("duration", 0),
            video_url=video_url,
        )
        processor = VideoProcessor()
        r = processor.process(video, skip_deep=False)
        task_store.heartbeat(task_id, metadata={"phase": "analysis_finish"})

        result = {
            "schema_version": L2_MULTIMODAL_SCHEMA_VERSION,
            "status": "completed",
            "task_id": task_id,
            "content_id": content_id,
            "task_type": task_type,
            "strategy": strategy,
            "model_version": L2_MODEL_VERSION,
            "score": r.score,
            "decision": r.decision,
            "scoring_rationale": r.scoring_rationale,
            "deep_analysis": r.deep_analysis,
            "analysis_time_s": r.deep_analysis_time_s,
        }
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        record_media_cache_entry(
            result_path,
            kind="analysis_result",
            content_id=content_id,
            task_id=task_id,
            source_url=video_url,
            metadata={"platform": platform, "strategy": strategy, "video_key": video_key},
        )
        log.info(f"[bg] 千问多模态分析完成: {platform} {video_key}, {result_path}")
        return {
            "video_key": video_key,
            "content_id": content_id,
            "score": r.score,
            "decision": r.decision,
            "analysis_time_s": r.deep_analysis_time_s,
            "schema_version": L2_MULTIMODAL_SCHEMA_VERSION,
            "model_version": L2_MODEL_VERSION,
        }

    adapter = LocalTaskAdapter(task_store)
    task = adapter.submit(
        LocalTaskSpec(
            task_id=task_id,
            task_type=task_type,
            platform=platform,
            target=video_key,
            content_id=content_id,
            result_path=result_path,
            max_attempts=int(os.environ.get("KR_L2_VIDEO_MAX_ATTEMPTS", "2")),
            retry_interval_s=float(os.environ.get("KR_L2_VIDEO_RETRY_INTERVAL_S", "5")),
            timeout_s=float(os.environ.get("KR_L2_VIDEO_TIMEOUT_S", "900")),
            metadata=_l2_task_metadata(
                video_key,
                basic_info,
                content_id,
                strategy=strategy,
                research_session_id=research_session_id,
                scope_metadata=scope_metadata,
            ),
        ),
        _run_analysis,
    )

    return _l2_processing_response(task_id, content_id, result_path, task, task_type=task_type)


def _l2_content_fingerprint(video_key: str, basic_info: Dict, *, strategy: str = L2_ANALYSIS_STRATEGY) -> str:
    payload = {
        "video_key": video_key,
        "strategy": strategy,
        "model_version": L2_MODEL_VERSION,
        "title": basic_info.get("title", ""),
        "duration": basic_info.get("duration", 0),
        "transcript_hash": hashlib.sha256(str(basic_info.get("transcript") or "").encode("utf-8", errors="ignore")).hexdigest()[:16],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _l2_task_metadata(
    video_key: str,
    basic_info: Dict,
    content_id: str,
    *,
    strategy: str = L2_ANALYSIS_STRATEGY,
    cache_hit: bool = False,
    research_session_id: str = "",
    scope_metadata: Dict | None = None,
) -> Dict:
    metadata = {
        "video_key": video_key,
        "title": basic_info.get("title", ""),
        "content_id": content_id,
        "strategy": strategy,
        "schema_version": L2_MULTIMODAL_SCHEMA_VERSION,
        "model_version": L2_MODEL_VERSION,
        "cache_hit": cache_hit,
        "source_url": basic_info.get("url") or f"https://www.bilibili.com/video/{video_key}",
        "approach": "sampled_media_with_text",
        "blocks_final_report": True,
        "result_reread_tool": "get_content_detail",
    }
    platform = str(basic_info.get("platform") or "B站")
    metadata = merge_scope_metadata(
        _task_scope_metadata(
            source_url=str(metadata["source_url"]),
            content_id=content_id,
            platform=platform,
            research_session_id=research_session_id,
            scope_metadata=scope_metadata,
        ),
        metadata,
    )
    if research_session_id:
        metadata["research_session_id"] = research_session_id
    return metadata


def _l2_processing_response(task_id: str, content_id: str, result_path: str, task: Dict, *, task_type: str = L2_ANALYSIS_STRATEGY) -> Dict:
    return {
        "schema_version": L2_MULTIMODAL_SCHEMA_VERSION,
        "status": "processing",
        "task_id": task_id,
        "content_id": content_id,
        "task_type": task_type,
        "model_version": L2_MODEL_VERSION,
        "result_path": result_path,
        "task": compact_task_ref(task),
        "message": f"千问多模态分析已在后台处理中（task_id={task_id}），可用 get_task_status 查询状态",
        "recommended_next_action": "poll_get_task_status",
    }


def _format_search_response(
    platform: str,
    items: List[Dict],
    *,
    trace: CollectionTrace | None = None,
) -> Dict:
    return format_search_response(platform, items, trace=trace)


def _format_search_error(
    platform: str,
    error_item: Dict,
    *,
    trace: CollectionTrace | None = None,
    strategy: str = "",
) -> Dict:
    return format_search_error(platform, error_item, trace=trace, strategy=strategy)
