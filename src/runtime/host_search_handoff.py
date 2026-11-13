"""宿主搜索的显式登记／交回；从不反向调用或伪造宿主工具回执。"""
from __future__ import annotations
import hashlib

from runtime.research_index import ResearchIndex, encoded


def _host_identity(item):
    """核对宿主实际交回的身份字段；缺字段不猜测，也不反向请求宿主。"""
    from generic_web.url_policy import public_addresses
    from kr_core.source_urls import canonical_url

    def url(value):
        if not isinstance(value,str) or not value or len(value)>2000:
            raise ValueError('host_snapshot_identity_url_required_and_bounded')
        public_addresses(value,resolve=False)
        return canonical_url(value)

    source=url(item['url'])
    identity={'identity_status':'unverified'}
    requested=url(item['requested_url']) if 'requested_url' in item else source
    observed=url(item['observed_url']) if 'observed_url' in item else None
    if observed is None and requested!=source:
        raise ValueError('host_snapshot_identity_mismatch: requested_url differs without observed source')
    if 'requested_url' in item:identity['requested_url']=requested
    if observed is not None:
        identity['observed_url']=observed
        if observed!=source:
            raise ValueError('host_snapshot_identity_mismatch: observed_url differs from evidence url')
    reference=item.get('observed_reference')
    if reference is not None:
        if not isinstance(reference,str) or not reference.strip() or len(reference)>2000:
            raise ValueError('host_snapshot_identity_reference_required_and_bounded')
        if reference!=item['host_reference']:
            raise ValueError('host_snapshot_identity_mismatch: observed_reference differs from host_reference')
        identity['observed_reference']=reference
    chain=item.get('redirect_chain')
    if chain is not None:
        if not isinstance(chain,list) or not 2<=len(chain)<=8:
            raise ValueError('host_snapshot_identity_redirect_chain_required_and_bounded')
        chain=[url(value) for value in chain]
        if observed is None or chain[0]!=requested or chain[-1]!=observed:
            raise ValueError('host_snapshot_identity_mismatch: redirect endpoints disagree')
        identity['redirect_chain']=chain
    if observed is not None and requested!=observed:
        if chain is None:
            raise ValueError('host_snapshot_identity_mismatch: requested_url differs without observed redirect chain')
        identity['identity_status']='host_declared_redirect'
    elif observed is not None:
        identity['identity_status']='host_declared_aligned' if reference is not None else 'host_declared_url_only'
    return identity


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
        identity=_host_identity(item)
        if identity!={'identity_status':'unverified'}:
            row['host_identity']=identity
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
        identity=row.get('host_identity',{'identity_status':'unverified'})
        candidate['host_identity']=identity
        if 'visible_text' in row:
            # 旧批次原样重交仍指向旧版本；新增身份声明须使用新批次，不能改旧证据。
            evidence_identity=[host_tool,batch_id,row['host_reference'],row['retrieved_at'],row['visible_scope'],row['truncated']]
            if 'host_identity' in row:evidence_identity.append(identity)
            ref=assets.put(row['url'],row['visible_text'],kind='host_snapshot',title=row['title'],metadata={
                'extractor':'host-visible-snapshot/v2' if 'host_identity' in row else 'host-visible-snapshot/v1','coverage':'host_visible_'+row['visible_scope'],
                'evidence_identity':encoded(evidence_identity),
                'provenance':{'host':{'tool':host_tool,'reference':row['host_reference'],
                    'retrieved_at':row['retrieved_at'],'scope':row['visible_scope'],'truncated':row['truncated'],
                    'query':value['query'],'batch_id':batch_id,'verification':'host_declared_not_native_verified',
                    **identity}}})
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
            'verification':value['verification'],
            'identity_unverified_count':sum(row.get('host_identity',{}).get('identity_status','unverified')=='unverified' for row in rows),
            'next_action':'Select URLs for public detail acquisition; use read_knowledge to read archived evidence. Preserve observed_url and observed_reference from the host payload; absent identity remains unverified. Do not attach mismatched pages to the requested source.'}
