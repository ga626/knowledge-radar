"""原有研究账本与证据的只读投影；不拥有第二套研究生命周期。"""
from __future__ import annotations
import json
import os
import re
from pathlib import Path
from collections import Counter
from runtime.paths import runtime_state_dir
from kr_core.evidence_assets import EvidenceAssets

SCHEMA = 'knowledgeradar-research-records/v1'


class ResearchRecords:
    def __init__(self, state_root=None, *, assets=None):
        self.root = Path(state_root or runtime_state_dir())
        self.assets = assets if assets is not None else EvidenceAssets(read_only=True)

    def snapshot(self, task_id):
        if not isinstance(task_id, str) or not task_id.startswith('kr-research-') or not all(c.isalnum() or c in '-_' for c in task_id) or len(task_id) > 96:
            raise ValueError('invalid_research_task_id')
        path = self.root / 'research_tasks' / (task_id + '.json')
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {'schema': SCHEMA, 'status': 'unknown_task', 'research_task_id': task_id}
        if payload.get('research_task_id') != task_id:
            raise ValueError('research_record_identity_mismatch')
        return payload

    def recent(self, limit=20):
        limit = max(1, min(int(limit), 100))
        directory = self.root / 'research_tasks'
        paths = sorted(directory.glob('kr-research-*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
        rows, unreadable = [], 0
        for path in paths[:limit]:
            try:
                row = self.snapshot(path.stem)
                rows.append({
                    key: row.get(key)
                    for key in ('research_task_id', 'objective', 'budget', 'status', 'project_name', 'created_at', 'updated_at')
                } | {'readable_summary': summarize_task(row), 'metrics': task_metrics(row)})
            except (ValueError, OSError):
                unreadable += 1
        return {
            'schema': SCHEMA,
            'tasks': rows,
            'unreadable_records': unreadable,
            'read_only': True,
            'origin': 'kr_research_ledger',
            'view_model': VIEW_MODEL,
        }

    def files(self, task_id='', limit=20):
        """Return independently viewable outputs without creating a catalog."""
        if task_id:
            payload = self.snapshot(task_id)
            if payload.get('status') == 'unknown_task':
                return payload | {'files': [], 'read_only': True, 'view_model': VIEW_MODEL}
            return {'schema': SCHEMA, 'research_task_id': task_id, 'files': task_files(payload, assets=self.assets), 'read_only': True, 'view_model': VIEW_MODEL}
        rows = []
        for item in self.recent(limit).get('tasks', []):
            current = self.snapshot(item['research_task_id'])
            rows.extend(task_files(current, assets=self.assets))
        return {'schema': SCHEMA, 'files': rows, 'read_only': True, 'view_model': VIEW_MODEL}


VIEW_MODEL = {
    'unit': 'task',
    'definition': '一次明确的用户决策问题；同一问题的工具调用和交付物归在同一任务下。',
    'round_definition': '任务内部按研究阶段分组：首轮发现、详情与身份核验、反例检查、完成检查。',
    'file_definition': '可以单独查看的报告、证据 sidecar、质量回执、固定证据版本和工具回执汇总。',
}

_ROUND_LABELS = {
    'discovery': '首轮发现',
    'verification': '详情与身份核验',
    'counterevidence': '反例检查',
    'closeout': '完成检查',
}
_EVENT_ROUNDS = {
    'route_opened': 'discovery', 'claim_gap_opened': 'discovery', 'query_family_created': 'discovery',
    'candidate_page_received': 'discovery', 'candidates_clustered': 'discovery',
    'candidate_selected': 'verification', 'detail_upgraded': 'verification', 'claim_supported': 'verification',
    'counterevidence': 'counterevidence', 'detail_degraded': 'counterevidence', 'gap_blocked': 'counterevidence',
    'gap_closed': 'closeout', 'stop_reviewed': 'closeout', 'closeout': 'closeout',
}
_TOOL_ROUNDS = {
    'kr_research': 'discovery', 'kr_web_search': 'discovery', 'search_': 'discovery',
    'get_content_detail': 'verification', 'extract_': 'verification', 'analyze_': 'verification',
    'finalize_research_task': 'closeout', 'get_task_status': 'closeout',
}
_INTERNAL_ECOLOGIES = {'research_route'}
_TOOL_ECOLOGIES = {
    'kr_web_search': 'generic_web_ecology',
    'search_': 'generic_web_ecology',
    'search_academic': 'academic_literature_ecology',
    'search_github_repositories': 'github_repository_ecology',
    'search_youtube': 'youtube_video_ecology',
    'search_wechat_articles': 'wechat_public_article_ecology',
    'search_zhihu': 'zhihu_discussion_ecology',
    'search_xiaohongshu': 'xiaohongshu_experience_ecology',
    'search_bilibili': 'bilibili_video_ecology',
}


def _round_for_tool(tool):
    name = str(tool or '')
    for prefix, round_key in _TOOL_ROUNDS.items():
        if name == prefix or name.startswith(prefix):
            return round_key
    return 'verification'


def _round_for_event(kind):
    return _EVENT_ROUNDS.get(str(kind or ''), 'verification')


def _ecology_for_tool(tool):
    name = str(tool or '')
    for prefix, ecology in sorted(_TOOL_ECOLOGIES.items(), key=lambda pair: len(pair[0]), reverse=True):
        if name == prefix or name.startswith(prefix):
            return ecology
    return ''


def _items(payload, key):
    return [item for item in payload.get(key, []) if isinstance(item, dict)]


def summarize_task(payload):
    objective = str(payload.get('objective') or '未命名调研')
    recorded_title = str(payload.get('display_title') or '').strip()
    title = recorded_title or human_task_title(objective)
    return {
        'task_id': str(payload.get('research_task_id') or ''),
        'title': title,
        'subtitle': objective,
        'title_source': 'codex' if recorded_title else 'local_fallback',
        'project_name': str(payload.get('project_name') or ''),
        'definition': VIEW_MODEL['definition'],
        'status': str(payload.get('status') or 'unknown'),
        'budget': str(payload.get('budget') or ''),
        'created_at': payload.get('created_at'),
        'updated_at': payload.get('updated_at'),
        'round_count': len(task_rounds(payload)),
        'file_count': len(task_files(payload)),
    }


def human_task_title(objective: str) -> str:
    """Turn a raw Codex objective into a stable, human-readable list label.

    The console must remain useful without making a second model call. If the
    Codex side later supplies ``display_title`` it wins; this deterministic
    fallback keeps historical records understandable and is intentionally
    conservative rather than pretending to summarize technical claims.
    """
    text = ' '.join(str(objective or '').split())
    if not text:
        return '未命名调研'
    cleaned = text
    for prefix in ('重新审视', '快速核对', '继续', '澄清', '汇总', '核对', '核验', '研究', '调研'):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].lstrip(' ：:')
            break
    # Keep the actual subject; never assign a fixed title just because a product
    # name appears. Preserve dots in API names and version numbers.
    cleaned = re.split(r'[：；;。！!?？]|:(?![\\/])|\.(?=\s|$)', cleaned, maxsplit=1)[0].strip(' ，,')
    return (cleaned[:32] + '…') if len(cleaned) > 33 else (cleaned or '未命名调研')


def task_rounds(payload):
    """Group recorded events and receipts into stable human-readable rounds."""
    grouped = {key: {'key': key, 'label': label, 'events': [], 'tool_receipts': [], 'count': 0}
               for key, label in _ROUND_LABELS.items()}
    for event in _items(payload, 'events'):
        key = _round_for_event(event.get('kind'))
        grouped[key]['events'].append({
            'at': event.get('at'), 'kind': event.get('kind'), 'tool': event.get('tool') or '',
            'source_ecology': event.get('source_ecology') or '', 'intent_label': event.get('intent_label') or '',
        })
    for receipt in _items(payload, 'tool_receipts'):
        key = _round_for_tool(receipt.get('tool'))
        grouped[key]['tool_receipts'].append({
            'at': receipt.get('at'), 'tool': receipt.get('tool'), 'status': receipt.get('status'),
            'receipt_id': receipt.get('receipt_id'), 'elapsed_s': receipt.get('elapsed_s'),
        })
    rounds = []
    for key, item in grouped.items():
        item['count'] = len(item['events']) + len(item['tool_receipts'])
        if item['count']:
            times = [row.get('at') for row in item['events'] + item['tool_receipts'] if row.get('at')]
            round_receipts = item['tool_receipts']
            round_statuses = Counter(str(row.get('status') or 'partial') for row in round_receipts)
            item['event_count'] = len(item['events'])
            item['tool_count'] = len(round_receipts)
            item['successful_calls'] = sum(round_statuses.get(status, 0) for status in ('ok', 'success', 'completed', 'pass'))
            item['failed_calls'] = sum(round_statuses.get(status, 0) for status in ('failed', 'error'))
            item['partial_calls'] = sum(round_statuses.get(status, 0) for status in ('partial', 'degraded'))
            item['elapsed_s'] = round(sum(float(row.get('elapsed_s') or 0) for row in round_receipts if isinstance(row.get('elapsed_s'), (int, float))), 3)
            item['source_ecologies'] = sorted({ecology for row in item['events'] + round_receipts for ecology in [str(row.get('source_ecology') or '') or _ecology_for_tool(row.get('tool'))] if ecology and ecology not in _INTERNAL_ECOLOGIES})
            item['started_at'] = min(times) if times else None
            item['ended_at'] = max(times) if times else None
            rounds.append(item)
    return rounds


def task_metrics(payload):
    receipts = _items(payload, 'tool_receipts')
    events = _items(payload, 'events')
    statuses = Counter(str(item.get('status') or 'partial') for item in receipts)
    tools = Counter(str(item.get('tool') or 'unknown') for item in receipts)
    ecologies = {ecology for item in receipts + events for ecology in [str(item.get('source_ecology') or '') or _ecology_for_tool(item.get('tool'))] if ecology and ecology not in _INTERNAL_ECOLOGIES}
    planned_ecologies = {str(item.get('source_ecology') or '') for item in _items(payload, 'candidate_ecologies') if str(item.get('source_ecology') or '')}
    versions = {str(version) for receipt in receipts for version in receipt.get('evidence_versions', []) if str(version)}
    gaps = [item for item in _items(payload, 'claim_gaps') if str(item.get('status') or 'open') not in {'closed', 'resolved'}]
    closeout = payload.get('closeout') if isinstance(payload.get('closeout'), dict) else {}
    unresolved = len(gaps) + sum(len(closeout.get(key) or []) for key in ('missing_outcomes', 'critical_claims_without_support', 'critical_claims_without_receipts', 'pending_background_tasks', 'unavailable_evidence_versions'))
    return {
        'tool_calls': len(receipts),
        'successful_calls': sum(statuses.get(status, 0) for status in ('ok', 'success', 'completed', 'pass')),
        'failed_calls': sum(statuses.get(status, 0) for status in ('failed', 'error')),
        'partial_calls': sum(statuses.get(status, 0) for status in ('partial', 'degraded')),
        'tools': [{'name': name, 'count': count} for name, count in sorted(tools.items())],
        'total_elapsed_s': round(sum(float(item.get('elapsed_s') or 0) for item in receipts if isinstance(item.get('elapsed_s'), (int, float))), 3),
        'source_ecologies': sorted(ecologies),
        'source_ecology_count': len(ecologies),
        'planned_source_ecologies': sorted(planned_ecologies),
        'evidence_version_count': len(versions),
        'unresolved_gap_count': unresolved,
        'event_count': len(events),
        'round_count': len(task_rounds(payload)),
    }


def _path_file(path, *, task_id, round_key, kind, label):
    if not path:
        return None
    value = str(path)
    import hashlib
    return {
        'file_id': kind + ':' + hashlib.sha256(value.encode()).hexdigest()[:24], 'name': Path(value).name or value, 'label': label, 'type': kind,
        'task_id': task_id, 'task_title': '', 'round': _ROUND_LABELS.get(round_key, _ROUND_LABELS['verification']),
        'path': value, 'status': 'available' if Path(value).is_file() else 'missing', 'readable': Path(value).is_file(),
    }


def task_files(payload, *, assets=None):
    task_id = str(payload.get('research_task_id') or '')
    task_title = str(payload.get('display_title') or human_task_title(payload.get('objective')))
    closeout = payload.get('closeout') if isinstance(payload.get('closeout'), dict) else {}
    # Older closeouts stored only the quality receipt path. Read its declared
    # artifact identities for compatibility; never write or synthesize paths.
    quality_receipt_path = str(closeout.get('quality_receipt_path') or '')
    if quality_receipt_path and Path(quality_receipt_path).is_file():
        try:
            receipt = json.loads(Path(quality_receipt_path).read_text(encoding='utf-8-sig'))
            if isinstance(receipt, dict):
                closeout = dict(closeout)
                closeout.setdefault('report_path', receipt.get('report_path') or '')
                closeout.setdefault('evidence_path', receipt.get('evidence_path') or '')
        except (OSError, json.JSONDecodeError):
            pass
    files = []
    for key, kind, label in (
        ('report_path', 'report', '调研报告'),
        ('evidence_path', 'evidence_sidecar', '证据 sidecar'),
        ('quality_receipt_path', 'quality_receipt', '质量回执'),
    ):
        item = _path_file(closeout.get(key), task_id=task_id, round_key='closeout', kind=kind, label=label)
        if item:
            item['task_title'] = task_title
            files.append(item)
    receipts = _items(payload, 'tool_receipts')
    if receipts:
        files.append({
            'file_id': f'tool_receipts:{task_id}', 'name': 'tool-receipts', 'label': '工具回执汇总', 'type': 'tool_receipts',
            'task_id': task_id, 'round': '全程记录', 'status': 'available', 'readable': True,
            'task_title': task_title, 'count': len(receipts), 'tools': task_metrics(payload)['tools'],
        })
    version_ids = sorted({str(version) for receipt in receipts for version in receipt.get('evidence_versions', []) if str(version)})
    for version_id in version_ids:
        item = {
            'file_id': f'evidence_version:{version_id}', 'name': version_id[:12], 'label': '固定证据版本', 'type': 'fixed_evidence',
            'task_id': task_id, 'round': '详情与身份核验', 'version_id': version_id, 'status': 'unknown', 'readable': False,
            'task_title': task_title,
        }
        if assets is not None:
            try:
                result = assets.fetch(version_id, start=0, max_chars=1)
                asset = result.get('asset') or {}
                item.update({'status': 'available', 'readable': True, 'title': asset.get('title') or asset.get('kind') or '固定证据', 'kind': asset.get('kind') or '', 'chars': asset.get('chars'), 'source_url': asset.get('source_url') or '', 'raw_sha256': asset.get('raw_sha256') or ''})
            except (ValueError, OSError):
                item['status'] = 'missing'
        files.append(item)
    return files


def workspace_for_console():
    root = os.environ.get('KR_RESEARCH_READ_ROOT') if os.environ.get('KR_CONSOLE_READ_ONLY') == '1' else None
    return ResearchRecords(root, assets=EvidenceAssets(Path(root) / 'evidence_assets', read_only=True)) if root else ResearchRecords()


def research_action(records, action, task_id='', *, payload=None):
    payload = payload or {}
    if not isinstance(payload, dict):
        raise ValueError('object_required')
    if action == 'list':
        return records.recent(payload.get('limit', 20))
    if action == 'inspect':
        current = records.snapshot(task_id)
        if current.get('status') == 'unknown_task':
            return current
        return current | {'readable_summary': summarize_task(current), 'rounds': task_rounds(current), 'metrics': task_metrics(current), 'files': task_files(current, assets=records.assets), 'view_model': VIEW_MODEL, 'read_only': True}
    if action == 'files':
        return records.files(task_id, payload.get('limit', 20))
    if action == 'evidence_search':
        return {'items': records.assets.search(payload.get('query', ''), limit=max(1, min(int(payload.get('limit', 30)), 100))), 'read_only': True}
    if action == 'evidence_fetch':
        return records.assets.fetch(payload.get('version_id'), start=payload.get('start', 0), max_chars=payload.get('max_chars', 8000))
    raise ValueError('read_only_research_action_required')
