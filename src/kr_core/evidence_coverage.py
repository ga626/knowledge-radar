"""请求、清单、原文和可查询活任务分别表达；零条不是不存在的证明。"""
from __future__ import annotations


def content_coverage(response, requested=None, *, task_lookup=None):
    requested = dict(requested or {})
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
