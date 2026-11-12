"""宿主搜索的显式登记／交回；从不反向调用或伪造宿主工具回执。"""
from __future__ import annotations
import hashlib

from runtime.research_index import ResearchIndex, encoded


def register(task_id, query, *, batch_id='', index=None):
    from runtime.research_ledger import read_task
    if read_task(task_id=task_id).get('status') == 'unknown_task':
        raise ValueError('unknown_research_task')
    query = query.strip()
    if not query or len(query) > 500:
        raise ValueError('host_query_must_be_1_to_500_characters')
    batch_id = batch_id or 'host-' + hashlib.sha256(query.encode()).hexdigest()[:20]
    index = index or ResearchIndex()
    value = {'batch_id':batch_id, 'query':query, 'status':'awaiting_host',
             'route':'host_internal_web_wave', 'source':'host_declared', 'verification':'not_native_verified'}
    with index.connection(write=True) as conn:
        old = conn.execute("SELECT value FROM current WHERE task_id=? AND kind='host_batch' AND object_id=?", (task_id,batch_id)).fetchone()
        if old:
            import json
            prior = json.loads(old[0])
            if prior['query'] != query:
                raise ValueError('host_batch_identity_conflict')
            return prior
        index.put(conn, task_id, 'host_batch', batch_id, value, batch_id)
    return {**value, 'next_action':'Run the host search tool, then submit its candidate URLs and real host references with this batch_id.'}


def submit(task_id, batch_id, items, *, host_tool, host_outcome='', query='', index=None):
    import json
    from kr_core.source_urls import public_evidence_payload
    from generic_web.url_policy import public_addresses
    if host_tool not in {
        'web.run', 'codex_web_search', 'codex_builtin_web_search',
        'mcp__codex_apps__search_service_web_run',
    }:
        raise ValueError('known_host_tool_required')
    if not isinstance(items, list) or len(items) > 50:
        raise ValueError('host_candidates_must_be_a_list_of_up_to_50')
    outcome = host_outcome or ('results' if items else '')
    if outcome not in {'results', 'no_results', 'unavailable'}:
        raise ValueError('empty_host_submission_requires_explicit_no_results_or_unavailable')
    if bool(items) != (outcome == 'results'):
        raise ValueError('host_outcome_and_candidates_disagree')
    rows = []
    total_bytes = 0
    for item in items:
        if not isinstance(item, dict) or not item.get('url') or not item.get('title'):
            raise ValueError('host_candidate_url_and_title_required')
        if not isinstance(item.get('host_reference'), str) or not item['host_reference'].strip():
            raise ValueError('host_candidate_real_reference_required')
        public_addresses(item['url'], resolve=False)
        row=public_evidence_payload({k:str(item.get(k) or '')[:2000] for k in
                    ('url','title','snippet','host_reference','retrieved_at')})
        if 'visible_text' in item:
            from datetime import datetime
            text=item['visible_text'];scope=item.get('visible_scope')
            if not isinstance(text,str) or not text.strip() or len(text.encode('utf-8'))>262144:
                raise ValueError('host_snapshot_text_required_and_bounded')
            if scope not in {'excerpt','page_text'} or not isinstance(item.get('truncated',False),bool):
                raise ValueError('host_snapshot_visible_scope_required')
            try:
                captured=datetime.fromisoformat(row['retrieved_at'].replace('Z','+00:00'))
                if captured.tzinfo is None:raise ValueError()
            except ValueError:raise ValueError('host_snapshot_retrieved_at_with_offset_required') from None
            # 不截断原文，不把page_text提升为全文；固定批次重试必须保留逐字相同载荷。
            row.update(visible_text=text,visible_scope=scope,truncated=item.get('truncated',False))
            total_bytes+=len(text.encode('utf-8'))
        rows.append(row)
    if total_bytes>2*1024*1024:raise ValueError('host_snapshot_batch_too_large')
    digest = hashlib.sha256(encoded([host_tool,rows,outcome]).encode()).hexdigest()
    index = index or ResearchIndex()
    # 搜索许可属于宿主Agent；登记可在搜索完成后进行。先校验全部载荷，
    # 再按查询和实际结果身份创建批次，避免失败请求留下空壳或覆盖下一轮。
    if not batch_id:
        clean_query = query.strip()
        if not clean_query or len(clean_query) > 500:
            raise ValueError('host_query_must_be_1_to_500_characters')
        batch_id = 'host-' + hashlib.sha256(encoded([clean_query,digest]).encode()).hexdigest()[:24]
        register(task_id, clean_query, batch_id=batch_id, index=index)
    with index.connection(write=True) as conn:
        row = conn.execute("SELECT value FROM current WHERE task_id=? AND kind='host_batch' AND object_id=?", (task_id,batch_id)).fetchone()
        if not row:
            raise ValueError('register_host_batch_before_submission')
        value = json.loads(row[0])
        if value.get('submission_sha256') and value['submission_sha256'] != digest:
            raise ValueError('host_batch_submission_conflict_use_new_batch')
        value.update(status='blocked' if outcome == 'unavailable' else 'received',
                     outcome=outcome, submission_sha256=digest, candidates=rows, host_tool=host_tool,
                     verification='host_declared_not_native_verified', candidate_count=len(rows))
        index.put(conn,task_id,'host_batch',batch_id,value,digest)
    if outcome == 'unavailable':
        return {'status':'blocked', 'batch_id':batch_id, 'candidate_count':0,
                'host_outcome':outcome, 'verification':value['verification'],
                'next_action':'Host tool returned no usable payload. Repair the host connection or choose another authorized source; do not treat this as zero search matches.'}
    # Projection is idempotent. A failed projection is repairable from the fixed batch,
    # without repeating the host search or upgrading declared material to native evidence.
    from kr_core.evidence_assets import EvidenceAssets
    assets=EvidenceAssets();references=[];projected=[]
    for row in rows:
        candidate={key:value for key,value in row.items() if key!='visible_text'}
        if 'visible_text' in row:
            ref=assets.put(row['url'],row['visible_text'],kind='host_snapshot',title=row['title'],metadata={
                'extractor':'host-visible-snapshot/v1','coverage':'host_visible_'+row['visible_scope'],
                'evidence_identity':encoded([host_tool,batch_id,row['host_reference'],row['retrieved_at'],row['visible_scope'],row['truncated']]),
                'provenance':{'host':{'tool':host_tool,'reference':row['host_reference'],
                    'retrieved_at':row['retrieved_at'],'scope':row['visible_scope'],'truncated':row['truncated'],
                    'query':value['query'],'batch_id':batch_id,'verification':'host_declared_not_native_verified'}}})
            references.append(ref);candidate['visible_version_ids']=[ref['version_id']]
        projected.append(candidate)
    index.candidates(task_id, projected, 'generic_web_ecology', '', query=value['query'])
    with index.connection(write=True) as conn:
        value['status'] = 'projected'
        value['visible_version_ids']=[ref['version_id'] for ref in references]
        index.put(conn,task_id,'host_batch',batch_id,value,digest+':projected')
    return {'status':'recorded','batch_id':batch_id,'candidate_count':len(rows),
            'evidence_assets':references,
            'host_outcome':outcome,
            'verification':value['verification'], 'next_action':'Select URLs for public detail acquisition; use read_knowledge to read archived evidence.'}
