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
        part_tasks = []
        for task in active:
            kind = str(task.get('task_type') or '')
            if any(alias in kind for alias in aliases.get(part, (part,))):
                part_tasks.append(task['task_id'])
        acquired = bool(value.strip() if isinstance(value, str) else value)
        if isinstance(value, str) and value.strip().lower() in {'pending', 'processing', 'failed', 'error'}:
            acquired = False
        if part == 'content' and (response.get('failure_type') in {'empty_detail', 'insufficient_content'} or metadata.get('content_quality') in {'empty', 'insufficient', 'metadata_only'}):
            acquired = False
        state = 'acquired' if acquired else 'not_requested' if asked is False else 'pending' if part_tasks and asked is True else 'blocked' if asked and response.get('manual_action_required') else 'missing' if asked is True else 'unknown'
        coverage = 'manifest_only' if acquired and part in {'images', 'audio', 'video'} else 'partial' if acquired else 'unknown'
        result[part] = {'requested': asked, 'state': state, 'coverage': coverage, 'pending_task_ids': sorted(set(part_tasks)) if state == 'pending' else [], 'missing_reason': '' if acquired else 'not_requested' if state == 'not_requested' else str(response.get('failure_type') or 'not_acquired_or_not_proven')}
    gaps = [k for k, v in result.items() if v['requested'] is True and v['state'] != 'acquired']
    return {'schema': 'knowledgeradar-content-coverage/v1', 'parts': result, 'required_gaps': gaps, 'request_satisfied': not gaps, 'complete': False, 'image_understanding_proven': False}
