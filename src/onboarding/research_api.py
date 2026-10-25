"""控制台只读查看Codex研究与证据；不启动研究或修改研究状态。"""
from __future__ import annotations
import base64
from pathlib import Path
from runtime.research_workspace import research_action


def workspace_api(records, payload, *, developer=True):
    if not isinstance(payload, dict):
        raise ValueError('object_required')
    action = payload.get('action', 'list')
    if not developer and action == 'optional_plan':
        raise ValueError('maintainer_view_required')
    if action == 'file_fetch':
        # Resolve only files already registered to this task, never a caller-
        # supplied filesystem path. The preview remains read-only.
        task_id = str(payload.get('task_id') or '')
        if not task_id:
            raise ValueError('research_task_required')
        files = records.files(task_id).get('files', [])
        item = next((row for row in files if row['file_id'] == payload.get('file_id')), None)
        if not item or item.get('type') not in ({'report', 'evidence_sidecar', 'quality_receipt'} if developer else {'report'}):
            raise ValueError('registered_research_file_required')
        path = Path(item['path'])
        if path.suffix.lower() not in {'.md', '.txt', '.json'}:
            raise ValueError('research_text_file_required')
        start = payload.get('start', 0)
        if isinstance(start, bool) or not isinstance(start, int) or start < 0:
            raise ValueError('invalid_locator')
        with path.open('rb') as handle:
            raw = handle.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError('research_file_too_large')
        body = raw.decode('utf-8-sig')
        if start > len(body):
            raise ValueError('invalid_locator')
        end = min(len(body), start + 8000)
        return {'name': item['name'], 'text': body[start:end], 'next_start': end if end < len(body) else None, 'read_only': True}
    if action == 'optional_plan':
        from runtime.optional_media import COMPONENTS, component_plan
        return {'components': [component_plan(name) for name in COMPONENTS]}
    if action in {'export', 'export_task', 'export_obsidian'}:
        from runtime.evidence_export import build_bundle, sha
        reports = []
        versions = payload.get('version_ids', [])
        if action != 'export':
            task_id = str(payload.get('task_id') or '')
            if not task_id: raise ValueError('research_task_required')
            files = records.files(task_id).get('files', [])
            allowed = {row['version_id'] for row in files if row.get('type') == 'fixed_evidence'}
            if not isinstance(versions, list) or not set(versions).issubset(allowed):
                raise ValueError('selected_versions_must_belong_to_task')
            if payload.get('include_report') is True:
                for row in files:
                    if row.get('type') != 'report': continue
                    if Path(row['path']).suffix.lower() not in {'.md','.txt'}: raise ValueError('research_text_file_required')
                    with Path(row['path']).open('rb') as handle: data = handle.read(2 * 1024 * 1024 + 1)
                    if len(data) > 2 * 1024 * 1024: raise ValueError('research_file_too_large')
                    reports.append({'text':data.decode('utf-8-sig'), 'title':row['name']})
        raw, manifest = build_bundle(records.assets, versions, export_acknowledged=payload.get('export_acknowledged'), reports=reports)
        if action == 'export_obsidian':
            from runtime.evidence_export import materialize_bundle
            from urllib.parse import urlencode
            # 目标由本次点击显式提供；不发现、遍历或缓存私人 Vault。
            root = Path(str(payload.get('vault_path') or ''))
            if not root.is_absolute() or not root.is_dir(): raise ValueError('existing_absolute_vault_required')
            target = root / 'KnowledgeRadar Inbox' / ('research-' + sha(raw)[:16])
            result = materialize_bundle(raw, target, approved_root=root)
            entry = target / (manifest.get('reports', [{}])[0].get('file', 'manifest.json') if manifest.get('reports') else 'manifest.json')
            uri = 'obsidian://open?' + urlencode({'path':str(entry.resolve())})
            receipt = {**result, 'sha256':sha(raw), 'open_uri':uri, 'source_store_modified':False}
            (target / 'delivery-receipt.json').write_text(__import__('json').dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
            return receipt
        return {'filename': 'KnowledgeRadar-evidence-' + sha(raw)[:12] + '.zip', 'sha256': sha(raw), 'bytes': len(raw), 'data_base64': base64.b64encode(raw).decode(), 'manifest': manifest}
    args = payload.get('payload') or payload
    result = research_action(records, action, str(payload.get('task_id') or ''), payload=args)
    return result if developer else user_projection(result)


def user_projection(result):
    """普通用户只收到产品信息；内部原始账本不随隐藏面板下发。"""
    keep = {'schema','status','research_task_id','objective','budget','project_name','created_at','updated_at','tasks','unreadable_records','read_only','readable_summary','metrics','rounds','files','closeout','view_model','items','asset','text','locator','locators','next_start','truncated','candidate_pool','claim_gaps'}
    projected = {key:value for key,value in result.items() if key in keep}
    if 'tasks' in projected: projected['tasks'] = [user_projection(row) for row in projected['tasks']]
    if 'metrics' in projected: projected['metrics'] = {key:value for key,value in projected['metrics'].items() if key not in {'tools','event_count','planned_source_ecologies'}}
    if 'files' in projected: projected['files'] = [{key:value for key,value in row.items() if key not in {'path','raw_sha256'}} for row in projected['files'] if row.get('type') in {'report','fixed_evidence'}]
    if 'rounds' in projected: projected['rounds'] = [{key:value for key,value in row.items() if key not in {'events','tool_receipts'}} for row in projected['rounds']]
    if 'candidate_pool' in projected: projected['candidate_pool'] = [{key:row[key] for key in ('title','name','source_ecology') if key in row} for row in projected['candidate_pool']]
    if 'claim_gaps' in projected: projected['claim_gaps'] = [{key:row[key] for key in ('claim_gap_id','reason','status') if key in row} for row in projected['claim_gaps']]
    if 'closeout' in projected: projected['closeout'] = {key:value for key,value in projected['closeout'].items() if key in {'missing_outcomes','critical_claims_without_support','critical_claims_without_receipts','pending_background_tasks','unavailable_evidence_versions'}}
    return projected
