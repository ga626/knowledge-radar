"""控制台只读查看Codex研究与证据；不启动研究或修改研究状态。"""
from __future__ import annotations
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
        if item.get('version_id'):
            value=records.assets.fetch(item['version_id'],start=payload.get('start',0),max_chars=8000)
            return {**value,'name':item['name'],'read_only':True}
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
    if action in {'export','export_task','export_obsidian'}:
        raise ValueError('agent_command_required_use_manage_research_artifacts')
    args = payload.get('payload') or payload
    result = research_action(records, action, str(payload.get('task_id') or ''), payload=args)
    return result if developer else user_projection(result)


def user_projection(result):
    """普通用户只收到产品信息；内部原始账本不随隐藏面板下发。"""
    keep = {'schema','status','research_task_id','objective','budget','project_name','created_at','updated_at','tasks','unreadable_records','read_only','readable_summary','metrics','rounds','files','closeout','view_model','items','asset','text','locator','locators','next_start','truncated','candidate_pool','claim_gaps','pending_submissions','gap_assessment','deliveries','next_cursor','generation','ranking','unavailable_count'}
    projected = {key:value for key,value in result.items() if key in keep}
    if 'tasks' in projected: projected['tasks'] = [user_projection(row) for row in projected['tasks']]
    if 'metrics' in projected: projected['metrics'] = {key:value for key,value in projected['metrics'].items() if key not in {'tools','event_count','planned_source_ecologies'}}
    if 'files' in projected: projected['files'] = [{key:value for key,value in row.items() if key not in {'path','raw_sha256'}} for row in projected['files'] if row.get('type') in {'report','fixed_evidence'}]
    if 'rounds' in projected: projected['rounds'] = [{key:value for key,value in row.items() if key not in {'events','tool_receipts'}} for row in projected['rounds']]
    if 'candidate_pool' in projected: projected['candidate_pool'] = [{key:row[key] for key in ('title','name','source_ecology') if key in row} for row in projected['candidate_pool']]
    if 'claim_gaps' in projected: projected['claim_gaps'] = [{key:row[key] for key in ('claim_gap_id','reason','status') if key in row} for row in projected['claim_gaps']]
    if 'pending_submissions' in projected:
        safe_keys = ('trace_id','tool','status','failure_code','source_ecology','association','receipt_kind','attempts','last_status','reason','updated_at')
        projected['pending_submissions'] = [{key:row[key] for key in safe_keys if key in row} for row in projected['pending_submissions'] if isinstance(row, dict)]
    if 'gap_assessment' in projected:
        assessment = projected['gap_assessment'] if isinstance(projected['gap_assessment'], dict) else {}
        safe_keys = ('schema','status','research_task_id','phase','stop_assessment','coverage_scope','unregistered_coverage','autonomy_boundary','read_only')
        projected['gap_assessment'] = {key:assessment[key] for key in safe_keys if key in assessment}
        projected['gap_assessment']['gaps'] = [
            {key:row[key] for key in ('code','meaning') if key in row}
            for row in assessment.get('gaps', []) if isinstance(row, dict)
        ]
        counts = assessment.get('counts') if isinstance(assessment.get('counts'), dict) else {}
        projected['gap_assessment']['counts'] = {key:counts[key] for key in ('candidate_count','upgraded_count','pending_submission_count') if key in counts}
    if 'closeout' in projected: projected['closeout'] = {key:value for key,value in projected['closeout'].items() if key in {'missing_outcomes','critical_claims_without_support','critical_claims_without_receipts','pending_background_tasks','unavailable_evidence_versions'}}
    return projected
