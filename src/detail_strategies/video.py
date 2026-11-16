"""视频用例编排：站点只负责发现，媒体执行与证据归档走公共层。"""
import time
from kr_core import DetailResponse
from media_bundle import MediaBundle, MediaRef, MediaKind, TextItem, TextKind
from media_router import MediaActionPlan, MediaOperation, MediaBudget, StoragePolicy, InformationSourceMode
from runtime.execution_context import check_execution


def _visual_requested(request, data):
    from routing import routing_recommends_l2
    return bool(request.enable_deep_analysis or (request.auto_multimodal and routing_recommends_l2(data)))


def _response(request, data):
    from understanding.evidence import build_detail_evidence
    return DetailResponse.from_legacy(request.platform, request.url, data,
        evidence=build_detail_evidence(request.url, request.platform, data), metadata={'strategy': 'staged_video_detail'})


class VideoDetailStrategy:
    def __init__(self, platform, *, collect):
        self.platform = platform
        self.collect = collect

    def extract(self, request):
        check_execution()
        started = time.monotonic()
        data = self.collect(request)
        # 视频简介是元数据层，不能被当成视频正文归档。保留兼容字段
        # ``content`` 供旧调用方读取，同时给公共证据层一个稳定的语义键。
        if isinstance(data.get('desc'), str) and data.get('desc').strip():
            data.setdefault('video_description', data['desc'])
        data.setdefault('content', data.get('desc', ''))
        from routing import attach_routing_metadata
        attach_routing_metadata(request.url, data)
        data.setdefault('platform', request.platform)
        data.setdefault('url', request.url)
        data.setdefault('timings', {})['collection_wall_s'] = round(time.monotonic() - started, 3)
        if data.get('error'):
            return _response(request, data)
        if 'desc' in data and isinstance(data['desc'], str) and not data['desc'].strip() and data.get('title'):
            data['content_observation'] = 'observed_empty_original_text'
        needs_media = ((request.options.get('include_transcript') and not data.get('transcript'))
                       or _visual_requested(request, data) or request.options.get('include_ocr') or request.enable_comment_filtering)
        # 不能把IP封禁或登录墙换个下载入口继续撞。
        blocked = (data.get('transcript_metadata') or {}).get('failure_type') in {
            'rate_limited_or_ip_blocked', 'verification_required', 'private_or_authorization_required', 'region_restricted'} or (
                (data.get('transcript_metadata') or {}).get('browser_fallback_attempted') and not data.get('transcript'))
        if blocked:
            transcript_gap = data['transcript_metadata'].get('failure_type') or 'browser_transcript_unavailable'
            data.update(enhancement_status='partial', failure_type=transcript_gap,
                        capability_status={'metadata': 'available' if data.get('title') else 'unknown',
                                           'transcript': 'blocked', 'media': 'not_attempted',
                                           'next_action': (data['transcript_metadata'].get('next_action') or
                                                           'continue_other_features_or_use_authorized_local_asr')})
            if data['transcript_metadata'].get('automatic_retry') is False:
                data.update(automatic_retry=False, resume_policy='stop_no_auto_resume')
            return _response(request, data)
        if needs_media and request.options.get('_detail_stage') == 'collection':
            data.update(status='partial', enhancement_status='queued', _enhancement_pending=True)
            return _response(request, data)
        return enhance_video_detail(request, data) if needs_media else _response(request, data)


def enhance_video_detail(request, collected):
    if collected.get('part_details'):
        return _enhance_parts(request, collected)
    from runtime.media_execution import execute_media_plan
    check_execution()
    started = time.monotonic()
    data = dict(collected)
    data.pop('_enhancement_pending', None)
    data.pop('failure_type', None)
    data.pop('content_coverage', None)
    if request.enable_comment_filtering:
        from runtime.video_analysis import filter_video_comments
        from runtime.resource_concurrency import ResourceDeferred
        from runtime.execution_context import ExecutionStopped
        try:
            data['filtered_comments'] = filter_video_comments(data,query=(request.options.get('comments') or {}).get('query',''),limit=(request.options.get('comments') or {}).get('view_limit',12))
        except (ResourceDeferred, ExecutionStopped):
            raise
        except Exception as exc:
            data['comment_filter_status'] = {'status': 'failed', 'error_code': getattr(exc, 'error_code', type(exc).__name__)}
    media = data.get('media_manifest') or []
    refs = [MediaRef(MediaKind(row['type']), row['url'], metadata=row.get('metadata', {}),
                     duration_seconds=data.get('duration') or None) for row in media]
    texts = [TextItem(data['transcript'], TextKind.SUBTITLE, source=(data.get('transcript_metadata') or {}).get('source', ''),
        language=(data.get('transcript_metadata') or {}).get('language', ''), metadata=data.get('transcript_metadata') or {})] if data.get('transcript') else []
    bundle = MediaBundle.build(platform=request.platform, source_url=request.url, content_id=data.get('content_id', ''),
        title=data.get('title', ''), texts=texts, audio_refs=[ref for ref in refs if ref.kind == MediaKind.AUDIO],
        video_refs=[ref for ref in refs if ref.kind == MediaKind.VIDEO])
    operations = [MediaOperation.READ_BASIC_TEXT]
    if request.options.get('include_transcript'):
        operations.append(MediaOperation.READ_TRANSCRIPT if texts else MediaOperation.RUN_ASR)
    visual = _visual_requested(request, data)
    if visual or request.options.get('include_ocr'):
        operations.append(MediaOperation.SAMPLE_FRAMES)
    if request.options.get('include_ocr'):
        operations.append(MediaOperation.RUN_OCR)
    from runtime.read_options import MediaReadOptions, normalized_options
    media_options = normalized_options(MediaReadOptions, request.options.get('media'))
    plan = MediaActionPlan.from_bundle(bundle, source_mode=InformationSourceMode.DERIVED_TEXT,
        target_kinds=(MediaKind.AUDIO, MediaKind.VIDEO), operations=tuple(operations),
        provider='local', storage_policy=StoragePolicy.MEDIA_CACHE_TTL,
        budget=MediaBudget(max_frames=media_options['max_frames'], timeout_seconds=request.options.get('timeout_s',600), metadata=media_options))
    result = execute_media_plan(bundle, plan, language=request.options.get('language'))
    for key in ('transcript', 'transcript_metadata', 'ocr_text', 'ocr_text_locators'):
        if key in result:
            data[key] = result[key]
    data['media_execution'] = {k: v for k, v in result.items() if k != 'frames'}
    data['enhancement_status'] = 'ok' if result['status'] == 'acquired' else 'partial'
    if data.get('comment_filter_status', {}).get('status') == 'failed':
        data['enhancement_status'] = 'partial'
    if request.options.get('include_transcript'):
        data['transcript_status'] = {'status': 'ok' if data.get('transcript') else 'failed'}
        if data.get('part_scope'):
            scope = data['part_scope']
            complete = bool(data.get('transcript')) and result['status'] == 'acquired'
            data['part_scope'] = {**scope, 'complete': complete,
                'completed_pages': scope['selected_pages'] if complete else []}
    if visual and result.get('frames'):
        from runtime.video_analysis import analyze_video_evidence
        from runtime.resource_concurrency import ResourceDeferred
        from runtime.execution_context import ExecutionStopped
        try:
            data['deep_analysis'] = analyze_video_evidence(data, frames=result['frames'])
            if data['deep_analysis'].get('status') != 'completed':
                data['enhancement_status'] = 'partial'
        except (ResourceDeferred, ExecutionStopped):
            raise
        except Exception as exc:
            data['deep_analysis'] = {'status': 'failed', 'error_code': getattr(exc, 'error_code', type(exc).__name__)}
            data['enhancement_status'] = 'partial'
    data.setdefault('timings', {})['enhancement_wall_s'] = round(time.monotonic() - started, 3)
    data['status'] = 'completed' if data['enhancement_status'] == 'ok' else 'partial'
    return _response(request, data)


def _enhance_parts(request, collected):
    """同一持久任务内逐P复用公共媒体检查点，不另起队列/模型/期限。"""
    from dataclasses import replace
    data = dict(collected)
    completed, parts, segments, texts, outcomes = [], [], [], [], []
    offset = 0
    for row in data.pop('part_details'):
        check_execution()
        part_request = replace(request, url=row['url'])
        failure = (row.get('transcript_metadata') or {}).get('failure_type')
        if failure in {'verification_required', 'rate_limited_or_ip_blocked', 'private_or_authorization_required', 'region_restricted'}:
            outcome = {**row, 'enhancement_status': 'partial', 'failure_type': failure}
        else:
            outcome = enhance_video_detail(part_request, row).data
        outcomes.append(outcome)
        text = outcome.get('transcript', '')
        page = row.get('page', 1)
        done = bool(text) and outcome.get('enhancement_status') == 'ok'
        if done:
            completed.append(page)
        if text:
            start = offset + (1 if texts else 0)
            texts.append(text)
            meta = outcome.get('transcript_metadata') or {}
            for segment in meta.get('segments') or []:
                if isinstance(segment, dict):
                    segments.append({**segment,
                        **({key: segment[key] + start for key in ('start', 'end') if isinstance(segment.get(key), int)}),
                        'part': page, 'cid': row.get('cid'), 'source_url': row['url']})
            parts.append({'page': page, 'cid': row.get('cid'), 'source_url': row['url'],
                'start': start, 'end': start + len(text), 'unit': 'unicode_code_point',
                'complete': done, 'source': meta.get('source'), 'language': meta.get('language')})
            offset = start + len(text)
    data['transcript'] = '\n'.join(texts)
    scope = data['part_scope']
    complete = len(completed) == len(scope['selected_pages'])
    data['part_scope'] = {**scope, 'completed_pages': completed, 'complete': complete}
    data['transcript_metadata'] = {'source': 'bilibili_multipart', 'parts': parts, 'segments': segments,
        'complete': complete, 'timestamp_scope': 'part_local_seconds', 'requested_scope': scope['kind']}
    data['part_results'] = [{'page': row.get('page'), 'url': row['url'],
        'status': row.get('status'), 'enhancement_status': row.get('enhancement_status'),
        'failure_type': row.get('failure_type') or (row.get('transcript_metadata') or {}).get('failure_type'),
        'media_execution': row.get('media_execution', {})} for row in outcomes]
    data.update(status='completed' if complete else 'partial', enhancement_status='ok' if complete else 'partial')
    return _response(request, data)
