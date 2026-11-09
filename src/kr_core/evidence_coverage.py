"""请求、清单、原文和可查询活任务分别表达；零条不是不存在的证明。"""
from __future__ import annotations
import re


def boilerplate_only(text):
    """Conservative structural observation, never a relevance classifier."""
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    if not lines or '```' in str(text):
        return False
    signals = set()
    for line in lines:
        if re.fullmatch(r'(?:©|copyright\b).*?(?:all rights reserved)[. ]*', line, re.I):
            signals.add('copyright')
        elif re.fullmatch(r'This site is protected by reCAPTCHA.*Terms of Service apply[. ]*', line, re.I):
            signals.add('legal')
        elif re.fullmatch(r'Select topic of interest(?: \(you can select more than one\))?[. ]*', line, re.I):
            signals.add('form')
        elif re.fullmatch(r'[\w .,&-]+, (?:Inc|LLC|Ltd)\.?', line, re.I):
            continue
        elif re.fullmatch(r'\d+ [\w ,.-]+\b\d{5}(?:-\d{4})?,? (?:USA|United States)', line, re.I):
            continue
        else:
            return False
    return len(signals) >= 2


def body_observation(response):
    """只识别获取形态和明确的访问壳，不判相关性、可信度或语义充分。"""
    metadata = response.get('detail_metadata') or response.get('metadata') or {}
    text = str(response.get('content') or response.get('text') or '').strip()
    normalized = re.sub(r'\s+', ' ', text).strip(' .!…\n').casefold()
    title = str(response.get('title') or '').strip().casefold().rstrip('.…')
    declared = response.get('body_state') or metadata.get('body_state')
    kind = response.get('source_kind') or metadata.get('source_kind') or ''
    known = {'asr':'transcript','subtitle':'transcript','transcript':'transcript','abstract':'abstract',
             'index':'index','video_description':'description','ocr':'ocr_text'}
    state = declared or known.get(kind) or ('description' if text and text == str(response.get('video_description') or '').strip() else 'body' if text else 'unknown')
    if (title in {'client challenge', 'just a moment', 'verify you are human'}
            or normalized == 'client challenge'
            or normalized.startswith(('client challenge ', 'please enable javascript to continue', 'enable javascript and cookies to continue'))):
        state = 'access_challenge'
    elif normalized in {'redirecting', 'redirecting you', '正在跳转', '跳转中'}:
        state = 'redirect_shell'
    elif normalized in {'loading', 'please wait', '加载中', '正在加载', 'please enable javascript'}:
        state = 'navigation_shell'
    elif boilerplate_only(text):
        state = 'boilerplate_only'
    return {'body_state': state, 'asset_kind': response.get('asset_kind') or metadata.get('asset_kind') or state,
            'source_role': response.get('source_role') or metadata.get('source_role') or state,
            'actual_scope': response.get('actual_scope') or metadata.get('actual_scope') or state,
            'missing_reason': state if state in {'access_challenge', 'redirect_shell', 'navigation_shell', 'boilerplate_only'} else ''}


def content_coverage(response, requested=None, *, task_lookup=None):
    requested = dict(requested or {})
    body = body_observation(response)
    metadata = response.get('detail_metadata') or response.get('metadata') or {}
    rows = []
    for container in (response, metadata):
        for key in ('tasks', 'pending_tasks', 'blocking_tasks'):
            value = container.get(key) or []
            rows.extend(value if isinstance(value, list) else value.get('tasks', []) if isinstance(value, dict) else [])
        if isinstance(container.get('task'), dict):
            rows.append(container['task'])
    active = []
    if rows and task_lookup is None:
        from runtime.tasks import get_task_store
        task_lookup = get_task_store().get_task
    for hint in rows:
        if not isinstance(hint, dict) or not hint.get('task_id'):
            continue
        task = task_lookup(hint['task_id'])
        if task and task.get('status') in {'queued', 'running'}:
            active.append(task)
    values = {'content': response.get('content') or response.get('text'), 'transcript': response.get('transcript'), 'images': response.get('images'), 'comments': response.get('comments'), 'audio': response.get('audio'), 'video': response.get('video'), 'ocr': response.get('ocr_text') or response.get('ocr')}
    aliases = {'transcript': ('transcript', 'asr', 'subtitle'), 'ocr': ('ocr',), 'comments': ('comment',)}
    result = {}
    for part, value in values.items():
        asked = True if part == 'content' else requested.get('include_' + part, requested.get(part))
        if part == 'ocr' and isinstance(value, dict):
            # A diagnostic object is not recognized text. Empty/failed OCR
            # must remain a gap even when the failure object is non-empty.
            value = '' if value.get('status') in {'failed', 'error', 'missing', 'pending'} else value.get('text') or value.get('content') or ''
        part_tasks = []
        for task in active:
            kind = str(task.get('task_type') or '')
            if any(alias in kind for alias in aliases.get(part, (part,))):
                part_tasks.append(task['task_id'])
        if part in {'ocr', 'transcript'} and asked is True and response.get('enhancement_status') == 'queued':
            part_tasks.extend(task['task_id'] for task in active if task.get('task_type') == 'durable_content_detail')
        acquired = bool(value.strip() if isinstance(value, str) else value)
        observed_zero_comments = part == 'comments' and value == [] and (
            response.get('comments_coverage') == 'observed_zero_comments' or response.get('comments_empty_confirmed') is True)
        observed_empty_text = (part == 'content' and not value and not response.get('error')
            and not response.get('failure_type') and response.get('content_observation') == 'observed_empty_original_text')
        acquired = acquired or observed_zero_comments or observed_empty_text
        if isinstance(value, str) and value.strip().lower() in {'pending', 'processing', 'failed', 'error'}:
            acquired = False
        if part == 'content' and (response.get('failure_type') in {'empty_detail', 'insufficient_content'} or metadata.get('content_quality') in {'empty', 'insufficient', 'metadata_only'}):
            acquired = False
        if part == 'content' and body['missing_reason']:
            acquired = False
        state = 'acquired' if acquired else 'not_requested' if asked is False else 'pending' if part_tasks and asked is True else 'blocked' if asked and response.get('manual_action_required') else 'missing' if asked is True else 'unknown'
        if not acquired and asked is True and part in {'ocr', 'transcript'} and response.get('enhancement_status') == 'queued':
            state = 'pending'
        enhancement = response.get('transcript_result' if part == 'transcript' else 'ocr') if part in {'transcript', 'ocr'} else None
        if isinstance(enhancement, dict) and enhancement.get('status') == 'not_applicable' and 'media' in response.get('completed_stages', []):
            state = 'not_applicable'
        coverage = 'manifest_only' if acquired and part in {'images', 'audio', 'video'} else 'partial' if acquired else 'unknown'
        if observed_zero_comments:
            coverage = 'observed_zero_comments'
        if observed_empty_text:
            coverage = 'observed_empty_original_text'
        result[part] = {'requested': asked, 'state': state, 'coverage': coverage, 'pending_task_ids': sorted(set(part_tasks)) if state == 'pending' else [], 'missing_reason': '' if acquired else 'not_requested' if state == 'not_requested' else str(response.get('failure_type') or 'not_acquired_or_not_proven')}
        if part == 'content':
            result[part].update({k: v for k, v in body.items() if k != 'missing_reason'})
            result[part]['requested_scope'] = requested.get('content_scope') or response.get('requested_scope') or 'source_text'
            if body['missing_reason']:
                result[part]['missing_reason'] = body['missing_reason']
            elif result[part]['requested_scope'] in {'body', 'fulltext', 'article_body'} and body['body_state'] in {'description', 'abstract', 'index'}:
                result[part].update(state='missing', missing_reason='requested_body_not_acquired')
        if state == 'not_applicable':
            result[part].update(coverage='not_applicable', missing_reason=enhancement.get('reason', 'not_applicable'))
    gaps = [k for k, v in result.items() if v['requested'] is True and v['state'] not in {'acquired', 'not_applicable'}]
    # 有部分文字不等于请求集合已完成。平台/公共执行器显式提供范围结果时优先核对。
    for part, key in (('ocr', 'ocr'), ('transcript', 'transcript_result')):
        enhancement = response.get(key)
        if result[part]['requested'] is True and isinstance(enhancement, dict):
            if enhancement.get('request_complete') is False or enhancement.get('status') in {'partial', 'failed', 'missing', 'skipped', 'error'}:
                result[part].update(coverage='partial', missing_reason='requested_media_window_incomplete')
                if part not in gaps: gaps.append(part)
            elif enhancement.get('request_complete') is True:
                # 成功但无文字的图片也可以完成识别；不伪造OCR文本。
                result[part].update(state='not_applicable' if enhancement.get('status')=='not_applicable' else 'acquired',
                    coverage='not_applicable' if enhancement.get('status')=='not_applicable' else 'requested_window', missing_reason='')
                if part in gaps: gaps.remove(part)
    if result['comments']['requested'] is True and response.get('comments_complete') is False:
        result['comments'].update(coverage='partial', missing_reason='requested_comment_window_incomplete')
        if 'comments' not in gaps:
            gaps.append('comments')
    comment_media_gaps = (response.get('comments_metadata') or {}).get('media_gaps') or []
    if result['comments']['requested'] is True and comment_media_gaps:
        result['comments'].update(coverage='partial', missing_reason='requested_comment_media_incomplete',
                                  media_gaps=comment_media_gaps)
        if 'comments' not in gaps:
            gaps.append('comments')
    if response.get('body_complete') is False and result['content']['requested'] is True:
        result['content'].update(coverage='partial', missing_reason=str(response.get('body_gap') or 'body_incomplete'))
        if 'content' not in gaps: gaps.append('content')
    return {'schema': 'knowledgeradar-content-coverage/v1', 'parts': result, 'required_gaps': gaps, 'request_satisfied': not gaps, 'complete': False, 'image_understanding_proven': False}
