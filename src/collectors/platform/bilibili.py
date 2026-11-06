"""Bilibili collection helpers migrated out of server.py."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Dict, List, Optional

import httpx
from kr_core.collection import CollectionTrace, format_search_error, format_search_response
from runtime.asr_policy import AsrPolicy
from runtime.media_cache import media_cache_subdir, record_media_cache_entry
from runtime.task_scope import make_task_scope, merge_scope_metadata
from runtime.tasks import get_task_store
from runtime.model_roles import CLOUD_MODEL_REF

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
log = logging.getLogger("mcp-server")
L2_MULTIMODAL_SCHEMA_VERSION = "knowledgeradar-l2-video-analysis/v1"
L2_ANALYSIS_STRATEGY = "bilibili_qwen_video_analysis"
L2_MODEL_VERSION = CLOUD_MODEL_REF

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
    log.info("search_bilibili: request received; page_size=%s", page_size)
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
    policy: AsrPolicy | None = None,
    **extra: object,
) -> Dict:
    policy = policy or AsrPolicy.from_env()
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


def _write_transcript_cache(path: str, text: str, *, kind="subtitle", audio_path="", policy=None, segments=None, locators=None, engine=None, fallback_reason=None) -> None:
    import hashlib
    import json
    from pathlib import Path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    destination = Path(path)
    temporary = destination.with_suffix(destination.suffix + ".pending")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(destination)
    from runtime.asset_identity import file_identity, model_identity
    policy = policy or AsrPolicy.from_env()
    receipt = {"schema": "knowledgeradar-transcript-cache/v3", "policy": policy.compact(),
               "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "source_id": destination.stem,
               "kind": kind, "claim_accuracy": "unmeasured",
               "audio": file_identity(audio_path) if audio_path else None,
               "model_identity": model_identity(policy) if kind == "asr" else None}
    from kr_core.evidence_locators import transcript_locators
    receipt["locators"] = transcript_locators(text, segments)
    if locators:
        from kr_core.evidence_locators import validate_locators
        receipt['locators'] = {'status':'aligned', 'segments':validate_locators(text, locators), 'precision':'input_chunk_only'}
    receipt['engine'] = engine
    receipt['fallback_reason'] = fallback_reason
    destination.with_suffix(destination.suffix + ".identity.json").write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")


def _read_transcript_cache(path: str, *, policy=None) -> str:
    if not os.path.exists(path):
        return ""
    try:
        import hashlib
        import json
        from pathlib import Path
        receipt = json.loads(Path(path + ".identity.json").read_text(encoding="utf-8"))
        if receipt.get("schema") != "knowledgeradar-transcript-cache/v3":
            return ""
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        policy = policy or AsrPolicy.from_env()
        if receipt.get("policy") != policy.compact() or receipt.get("sha256") != hashlib.sha256(text.encode("utf-8")).hexdigest():
            return ""
        if receipt.get("kind") == "asr":
            from runtime.asset_identity import file_identity, model_identity
            audio = receipt.get("audio") or {}
            if not audio.get("name") or file_identity(Path(path).parent / audio["name"]) != audio or model_identity(policy) != receipt.get("model_identity"):
                return ""
        return text
    except Exception:
        return ""




def get_bilibili_transcript_metadata(bvid, language=None):
    policy = AsrPolicy.for_request(language)
    for task in get_task_store().tasks_for_source(content_id=bvid, include_terminal=True, limit=100):
        if task.get('task_type') != 'bilibili_transcribe' or task.get('status') != 'completed':
            continue
        path = task.get('result_path') or ''
        if not path or not _read_transcript_cache(path, policy=policy):
            continue
        try:
            from pathlib import Path
            receipt = json.loads(Path(path + '.identity.json').read_text(encoding='utf-8'))
            return {'source_kind': receipt.get('kind', 'unknown'), 'engine':receipt.get('engine'), 'fallback_reason':receipt.get('fallback_reason'), 'policy': receipt['policy'], 'text_sha256': receipt['sha256'], 'locators': receipt.get('locators') or {'status': 'missing', 'reason': 'legacy_cache_has_no_timed_segments'}}
        except (OSError, ValueError, KeyError):
            continue
    return {'source_kind': 'unknown', 'locators': {'status': 'missing', 'reason': 'matching_verified_cache_not_found'}}

def _probe_bilibili_subtitle(bvid: str, *, output_dir: str, task_id: str, language: str | None = None, info=None) -> Dict:
    language = AsrPolicy.for_request(language).language
    started = time.time()
    info = info if info is not None else (get_bilibili_info(bvid) or {})
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
        player_resp.raise_for_status()
        player = player_resp.json()
        if player.get("code", 0) != 0:
            return {"hit": False, "reason": "subtitle_api_error:" + str(player.get("code")),
                    "failure_type": 'verification_required' if player.get('code') in {-352, -412} else 'subtitle_api_error'}
        subtitles = (((player.get("data") or {}).get("subtitle") or {}).get("subtitles") or [])
        if not subtitles:
            return {"hit": False, "duration_s": round(time.time() - started, 3), "reason": "no_subtitles"}
        matching = [row for row in subtitles if str(row.get("lan") or "").lower().replace('_', '-').removeprefix('ai-').split("-")[0] == language] if language else subtitles
        if not matching:
            return {"hit": False, "duration_s": round(time.time() - started, 3), "reason": "no_subtitle_for_requested_language"}
        subtitle = matching[0] or {}
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
        subtitle_resp.raise_for_status()
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
            "language": subtitle.get("lan", ""),
            "text": text,
            "timestamped_segments": [{"text": str(row.get("content") or "").strip(), "start_s": row.get("from"), "end_s": row.get("to")} for row in subtitle_data.get("body") or [] if str(row.get("content") or "").strip()],
            "duration_s": round(time.time() - started, 3),
            "subtitle_url": subtitle_url,
            "line_count": len(lines),
        }
    except Exception as exc:
        from runtime.media_acquisition import classify_media_error
        return {
            "hit": False,
            "duration_s": round(time.time() - started, 3),
            "reason": str(exc)[:160],
            "failure_type": classify_media_error(exc),
        }


def collect_video_detail(request):
    """仅采集文字/评论/字幕和媒体定位；不下载、不调模型。"""
    bvid = extract_bvid(request.url)
    if not bvid:
        return {'error': 'invalid_bvid', 'failure_type': 'invalid_input'}
    info = get_bilibili_info(bvid)
    if not info:
        return {'error': 'video_metadata_unavailable', 'failure_type': 'metadata_request_failed'}
    data = {**info, 'content': info.get('desc', ''), 'content_id': bvid, 'content_type': 'video', 'url': request.url, 'platform': 'B站', 'transcript': ''}
    want_transcript = bool(request.options.get('include_transcript'))
    if want_transcript:
        subtitle = _probe_bilibili_subtitle(bvid, output_dir=str(media_cache_subdir('subtitles', content_id=bvid)),
                                           task_id='', language=request.options.get('language'), info=info)
        if subtitle.get('hit'):
            from kr_core.evidence_locators import transcript_locators
            data['transcript'] = subtitle['text']
            mapping = transcript_locators(subtitle['text'], subtitle.get('timestamped_segments'))
            data['transcript_metadata'] = {**mapping, 'source': 'bilibili_subtitle',
                'language': subtitle.get('language', ''), 'subtitle_url': subtitle.get('subtitle_url', '')}
        else:
            data['transcript_metadata'] = {'status': 'unavailable', 'reason': subtitle.get('reason'),
                'failure_type': subtitle.get('failure_type', ''), 'source': 'bilibili_subtitle'}
    if request.options.get('include_comments', request.enable_comment_filtering):
        data.update(collect_comments(bvid, options=request.options.get('comments')))
    else:
        data['comments'] = []
    data['filtered_comments'] = []  # 语义筛选属于增强阶段，采集阶段先保存全部原始评论。
    headers = {'Referer': 'https://www.bilibili.com/', 'Origin': 'https://www.bilibili.com'}
    data['media_manifest'] = [{'type': kind, 'url': f'https://www.bilibili.com/video/{bvid}',
                               'metadata': {'headers': headers}} for kind in ['audio', 'video']]
    return data


def transcribe_bilibili(bvid, output_dir='', research_session_id='', scope_metadata=None, language=None):
    """兼容旧Python消费者，执行仍只走同一持久详情用例。"""
    from runtime.video_tasks import submit_video_detail
    result = submit_video_detail('B站', f'https://www.bilibili.com/video/{bvid}',
        language=language, research_session_id=research_session_id, scope=scope_metadata, include_transcript=True)
    return result.get('transcript') or '[transcribe] 后台任务: ' + result['task_id']


def get_bilibili_comments(bvid: str, limit: int = 20) -> List[Dict]:
    """兼容列表入口；详情入口使用含分页/失败回执的collect_comments。"""
    return collect_comments(bvid,options={'root_limit':min(100,max(1,limit))})['comments']


def collect_comments(bvid, *, options=None):
    from runtime.read_options import CommentReadOptions, normalized_options
    from runtime.execution_context import check_execution, ExecutionStopped
    scope = normalized_options(CommentReadOptions, options)
    headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'}
    rows, gaps, seen, replies = [], [], set(), []
    pages, roots, cursor, exhausted = 0, 0, 0, False
    def get(path, params):
        nonlocal pages
        check_execution()
        if pages >= scope['max_pages']:
            raise ValueError('comment_page_budget')
        pages += 1
        response = httpx.get('https://api.bilibili.com'+path, params=params, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        if data.get('code') != 0:
            code = data.get('code')
            raise ValueError({-101:'login_required',-412:'anti_bot_verification',-352:'anti_bot_verification'}.get(code, 'bilibili_comment_rejected_'+str(code)))
        return data.get('data') or {}
    def add(item, root=None):
        identifier = str(item.get('rpid_str') or item['rpid'])
        if identifier in seen:
            return
        seen.add(identifier)
        content = item.get('content') or {}
        parent = item.get('parent_str') or item.get('parent')
        rows.append({'comment_id': identifier, 'content': content.get('message', ''),
            'author': (item.get('member') or {}).get('uname', ''), 'likes': item.get('like'), 'created_at': item.get('ctime'),
            'root_comment_id': root or identifier, 'parent_comment_id': str(parent) if root and parent else root,
            'is_reply': bool(root), 'relationship_confidence': 'native_parent' if root else 'root',
            'images': [picture.get('img_src') for picture in content.get('pictures') or [] if picture.get('img_src')],
            'media_observation': 'content.pictures; unobserved_audio_video_not_asserted'})
    try:
        info = get('/x/web-interface/view', {'bvid': bvid})
        aid = info['aid']
        while roots < scope['root_limit'] and len(rows) < scope['total_limit']:
            data = get('/x/v2/reply/main', {'oid': aid, 'type': 1, 'mode': 3 if scope['order']=='relevance' else 2,
                'next': cursor, 'ps': min(20, scope['root_limit']-roots, scope['total_limit']-len(rows))})
            previous = len(seen)
            for item in data.get('replies') or []:
                if roots >= scope['root_limit'] or len(rows) >= scope['total_limit']:
                    break
                identifier = str(item.get('rpid_str') or item['rpid'])
                if identifier not in seen:
                    add(item); roots += 1
                    if item.get('rcount'):
                        replies.append((identifier, int(item['rcount'])))
            state = data.get('cursor') or {}
            exhausted = state.get('is_end') is True
            next_cursor = state.get('next')
            if exhausted:
                break
            if next_cursor is None or next_cursor == cursor or previous == len(seen):
                gaps.append({'reason': 'root_pagination_unconfirmed'}); break
            cursor = next_cursor
        reply_coverage = []
        for root, total in replies:
            got, page = 0, 1
            while got < min(total, scope['reply_limit']) and len(rows) < scope['total_limit']:
                data = get('/x/v2/reply/reply', {'oid':aid,'type':1,'root':root,'pn':page,
                    'ps':min(20,scope['reply_limit']-got,scope['total_limit']-len(rows))})
                before = len(seen)
                for item in data.get('replies') or []:
                    if got + len(seen)-before >= min(total,scope['reply_limit']) or len(rows)>=scope['total_limit']:
                        break
                    add(item, root)
                got += len(seen)-before; page += 1
                if len(seen) == before:
                    break
            reply_coverage.append({'root_comment_id':root,'observed_total':total,'returned':got,
                'requested_window_complete':got>=min(total,scope['reply_limit']),'exhausted':got>=total})
        satisfied = not gaps and (roots >= scope['root_limit'] or exhausted) and all(r['requested_window_complete'] for r in reply_coverage)
        source_done = exhausted and all(r['exhausted'] for r in reply_coverage)
    except ExecutionStopped:
        raise
    except Exception as exc:
        gaps.append({'reason':str(exc) if isinstance(exc,ValueError) else type(exc).__name__, 'automatic_retry':False})
        satisfied, source_done = False, False
        reply_coverage = locals().get('reply_coverage', [])
    return {'comments':rows,'comments_complete':satisfied,'comments_exhausted':source_done,
        'comments_empty_confirmed':source_done and not rows,
        'comments_coverage':'observed_zero_comments' if source_done and not rows else 'bounded_api_window',
        'comments_metadata':{'pages':pages,'returned_roots':roots,'reply_coverage':reply_coverage,'gaps':gaps,
            'next_root_cursor':cursor,'stop_reason':gaps[0]['reason'] if gaps else 'source_exhausted' if source_done else 'requested_window'}}


def filter_bilibili_comments(comments, output_dir="", scope_options=None, bvid="", source_url=""):
    """历史Python入口；统一委托公共增强步骤，不在采集阶段调用。"""
    from runtime.video_analysis import filter_video_comments
    return filter_video_comments({"comments": comments})


def deep_analyze_bilibili(bvid, basic_info, research_session_id='', scope_options=None):
    from runtime.video_tasks import submit_video_detail
    return submit_video_detail('B站', f'https://www.bilibili.com/video/{bvid}',
        research_session_id=research_session_id, scope=(scope_options or {}).get('task_scope'), deep=True)


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
