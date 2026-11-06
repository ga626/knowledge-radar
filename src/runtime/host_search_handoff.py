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


def submit(task_id, batch_id, items, *, host_tool, host_outcome='', index=None):
    import json
    from kr_core.source_urls import public_evidence_payload
    from generic_web.url_policy import public_addresses
    if not batch_id or host_tool not in {'web.run','codex_web_search','codex_builtin_web_search'}:
        raise ValueError('registered_host_batch_and_known_host_tool_required')
    if not isinstance(items, list) or len(items) > 50:
        raise ValueError('host_candidates_must_be_a_list_of_up_to_50')
    outcome = host_outcome or ('results' if items else '')
    if outcome not in {'results', 'no_results', 'unavailable'}:
        raise ValueError('empty_host_submission_requires_explicit_no_results_or_unavailable')
    if bool(items) != (outcome == 'results'):
        raise ValueError('host_outcome_and_candidates_disagree')
    rows = []
    for item in items:
        if not isinstance(item, dict) or not item.get('url') or not item.get('title'):
            raise ValueError('host_candidate_url_and_title_required')
        if not isinstance(item.get('host_reference'), str) or not item['host_reference'].strip():
            raise ValueError('host_candidate_real_reference_required')
        public_addresses(item['url'], resolve=False)
        rows.append(public_evidence_payload({k:str(item.get(k) or '')[:2000] for k in
                    ('url','title','snippet','host_reference','retrieved_at')}))
    digest = hashlib.sha256(encoded([host_tool,rows,outcome]).encode()).hexdigest()
    index = index or ResearchIndex()
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
    index.candidates(task_id, rows, 'generic_web_ecology', '', query=value['query'])
    with index.connection(write=True) as conn:
        value['status'] = 'projected'
        index.put(conn,task_id,'host_batch',batch_id,value,digest+':projected')
    return {'status':'recorded','batch_id':batch_id,'candidate_count':len(rows),
            'host_outcome':outcome,
            'verification':value['verification'], 'next_action':'Select URLs for public detail acquisition; use read_knowledge to read archived evidence.'}
