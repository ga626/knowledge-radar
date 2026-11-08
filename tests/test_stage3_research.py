"""阶段三定向合同：固定阅读、可逆关系、持久查询与保留候选。"""
import json
import time
from pathlib import Path
import pytest
from kr_core.evidence_assets import EvidenceAssets
from runtime.knowledge import Search, read
from runtime.research_index import ResearchIndex, ResearchWrite, identity
from runtime.research_ledger import open_task, record_candidates
from runtime.evidence_views import suggestions
from search_providers.aggregation import aggregate_results
from search_providers.models import SearchProviderResult

@pytest.fixture
def research(tmp_path,monkeypatch):
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path/'state'))
    monkeypatch.setenv('KR_EVIDENCE_ROOT',str(tmp_path/'assets'))
    monkeypatch.setenv('KR_DATA_ROOT',str(tmp_path/'data'))
    monkeypatch.setenv('KR_LOG_DIR',str(tmp_path/'logs'))
    task=open_task(objective='stage3',budget='balanced',considered=[])
    return task['research_task_id'],ResearchIndex(),EvidenceAssets(tmp_path/'assets')


def test_rrf_order_dedup_and_retained_tail():
    a=SearchProviderResult('a','https://a.test/x?utm_source=z',source_provider='a')
    b=SearchProviderResult('b','https://b.test/x',source_provider='b')
    x={'a':[a,a], 'b':[b,a]}
    first=aggregate_results(x,None)
    assert first==aggregate_results(dict(reversed(list(x.items()))),None)
    assert len(first)==2
    assert first[0].raw['provider_provenance']==['a','b']
    assert first[0].raw['score_is_truth_confidence'] is False
    assert len(aggregate_results(x,1))==1


def test_identity_cross_ecology_tracking():
    assert identity({'url':'https://a.test/x?utm_source=a'},'web')==identity({'url':'https://a.test/x'},'github')


def test_query_ecology_alias_is_normalized_and_unknown_remains_visible():
    from runtime.research_queries import query_families
    rows = query_families('topic', [{'query':'中文实践', 'intent':'experience', 'source_ecology':'知乎'},
                                  {'query':'topic', 'intent':'discovery', 'source_ecology':'future_platform'}])
    assert rows[0]['source_ecology'] == 'zhihu_discussion_ecology'
    assert rows[1]['ecology_resolution'] == 'unsupported'
    assert rows[1]['source_ecology'] == 'future_platform'


def test_first_wave_explains_unselected_and_unknown_queries(research):
    from runtime import composition
    from runtime.research_queries import query_families
    tid, index, _ = research
    families = query_families('topic', [{'query':'q', 'intent':'experience', 'source_ecology':'知乎'},
                                      {'query':'q', 'intent':'discovery', 'source_ecology':'future_platform'}])
    result = composition._research_first_wave({'selected_source_ecologies':['generic_web_ecology'], 'query_families':families}, 1, tid)
    assert {r['reason'] for r in result} == {'ecology_not_selected', 'unsupported_ecology'}
    assert len(index.read(tid, view='queries')['items']) == 2


def test_research_query_preserves_language_and_failure_outcome(research, monkeypatch):
    from runtime import composition
    tid, index, _ = research
    sent = []
    monkeypatch.setattr(composition, 'kr_web_search', lambda **kw: sent.append(kw) or {'items':[]})
    family = {'family_id':'zh', 'source_ecology':'generic_web_ecology', 'query':'中文实践', 'language':'zh'}
    composition.execute_research_query({'family':family, 'research_task_id':tid, 'limit':1})
    assert sent[0]['language'] == 'zh'
    def fail(**kw):
        raise RuntimeError('adapter failed')
    monkeypatch.setattr(composition, 'kr_web_search', fail)
    with pytest.raises(RuntimeError):
        composition.execute_research_query({'family':family, 'research_task_id':tid, 'limit':1})
    assert index.read(tid, view='queries')['items'][0]['status'] == 'failed'


def test_candidate_catalog_stable_and_reversible(research):
    tid,index,assets=research
    items=[{'url':f'https://a.test/{i}','title':str(i)} for i in range(6)]
    record_candidates(task_id=tid,source_ecology='web',tool='kr_web_search',items=items)
    first=index.read(tid,limit=2)
    index.candidates(tid,[{'url':'https://new.test/x'}],'web','new')
    second=index.read(tid,limit=20,after_seq=first['next_after_seq'],before_seq=first['watermark'])
    assert len(first['items'])+len(second['items'])==6
    cid=first['items'][0]['candidate_id']
    for folded in [True,False,True,False]:
        index.annotate(ResearchWrite(action='fold',task_id=tid,candidate_id=cid,record_id='ignored-for-fold',folded=folded,rationale='review'))
        page=index.read(tid)
        assert next(x for x in page['items'] if x['candidate_id']==cid)['fold']['folded']==folded
    assert len(index.read(tid,include_folded=False)['items'])==7


def test_feedback_not_saturation_and_withdrawal(research):
    tid,index,assets=research
    ref=assets.put('https://a.test/x','support')
    q=index.annotate(ResearchWrite(action='question',task_id=tid,text='why?'))['record_id']
    index.annotate(ResearchWrite(action='link',task_id=tid,question_id=q,version_id=ref['version_id'],relation='contradicts',rationale='evidence'),assets=assets)
    f=index.read(tid,view='feedback',assets=assets)
    assert f['unresolved_counterevidence'] and not f['questions_without_valid_links']
    assets.withdraw(ref['source_id'])
    f=index.read(tid,view='feedback',assets=assets)
    assert f['questions_without_valid_links']==[q] and not f['unresolved_counterevidence']
    assert f['semantic_saturation']=='not_computed' and f['stop_decision']=='agent_owned'


def test_assessments_preserve_scope_and_do_not_fill_another_language(research):
    tid, index, assets = research
    q = index.annotate(ResearchWrite(action='question',task_id=tid,text='中文开发者实践',required=True,languages=['zh'],information_roles=['experience']))['record_id']
    ref = assets.put('https://a.test/english','English vendor claim: not production ready.')
    assets.reference_many([ref['version_id']], tid, task_id=tid)
    record = index.annotate(ResearchWrite(action='assessment',task_id=tid,question_id=q,claim_id='production',
        relation='contradicts',basis='The original explicitly says not production ready.',applicability='English vendor statement only',
        uncertainty='No Chinese developer experience collected',languages=['en'],information_roles=['vendor_statement'],
        evidence_refs=[{'version_id':ref['version_id'],'start':0,'end':len('English vendor claim: not production ready.')}]),assets=assets)
    feedback = index.read(tid,view='feedback',assets=assets)
    assert q in feedback['questions_without_reviewed_support']
    assert feedback['unresolved_counterevidence'] == [record['record_id']]
    assert feedback['scope_gaps'][0]['missing_languages'] == ['zh']
    assert feedback['semantic_saturation'] == 'not_computed'


def test_invalid_locator_and_withdrawn_assessment_are_not_support(research):
    tid, index, assets = research
    q = index.annotate(ResearchWrite(action='question',task_id=tid,text='why'))['record_id']
    ref = assets.put('https://a.test/source','bounded original')
    assets.reference_many([ref['version_id']], tid, task_id=tid)
    args = dict(action='assessment',task_id=tid,question_id=q,relation='supports',claim_id='claim',basis='read original',applicability='test')
    with pytest.raises(ValueError,match='locator'):
        index.annotate(ResearchWrite(**args,evidence_refs=[{'version_id':ref['version_id'],'start':0,'end':999}]),assets=assets)
    index.annotate(ResearchWrite(**args,evidence_refs=[{'version_id':ref['version_id'],'start':0,'end':7}]),assets=assets)
    assert not index.read(tid,view='feedback',assets=assets)['questions_without_reviewed_support']
    assets.withdraw(ref['source_id'])
    assert index.read(tid,view='feedback',assets=assets)['questions_without_reviewed_support'] == [q]


def test_decision_records_limits_without_approving_saturation(research):
    tid, index, assets = research
    saved = index.annotate(ResearchWrite(action='decision',task_id=tid,decision='defer',rationale='Chinese sources blocked',
                                       unresolved_questions=['cn-experience'],reopen_conditions='Login then retry once'))
    rows = index.read(tid,view='decisions')['items']
    assert rows[0]['record_id'] == saved['record_id'] and rows[0]['decision'] == 'defer'
    assert index.read(tid,view='feedback')['stop_decision'] == 'agent_owned'


def test_read_counterevidence_covers_language_without_supporting_opposite_claim(research):
    tid,index,assets=research
    q=index.annotate(ResearchWrite(action='question',task_id=tid,text='Multiple concurrent writers?',languages=['en']))['record_id']
    ref=assets.put('https://a.test/wal','Only one writer at a time.')
    assets.reference_many([ref['version_id']],tid,task_id=tid)
    index.annotate(ResearchWrite(action='assessment',task_id=tid,question_id=q,claim_id='multiwriter',relation='contradicts',
        basis='Explicitly one writer',applicability='WAL same database',languages=['en'],
        evidence_refs=[{'version_id':ref['version_id'],'start':0,'end':25}]),assets=assets)
    f=index.read(tid,view='feedback',assets=assets)
    assert not f['scope_gaps'], 'English was reviewed even though it refutes the claim'
    assert q in f['questions_without_reviewed_support'] and f['unresolved_counterevidence']


def test_scope_revision_and_new_source_version_reopen_review(research):
    tid, index, assets = research
    q = index.annotate(ResearchWrite(action='question', task_id=tid, question_id='q', text='Practice', languages=['en']))['record_id']
    first = assets.put('https://a.test/versioned', 'Original claim')
    assets.reference_many([first['version_id']], tid, task_id=tid)
    assessment = dict(action='assessment',task_id=tid,question_id=q,claim_id='c',relation='supports',
        basis='Original claim',applicability='Original scope',languages=['en'],
        evidence_refs=[{'version_id':first['version_id'],'start':0,'end':14}])
    index.annotate(ResearchWrite(**assessment), assets=assets)
    assert not index.read(tid,view='feedback',assets=assets)['questions_without_reviewed_support']
    index.annotate(ResearchWrite(action='scope',task_id=tid,question_id=q,text='Practice',languages=['zh'],rationale='Chinese perspective required'))
    feedback = index.read(tid,view='feedback',assets=assets)
    assert feedback['questions_without_reviewed_support'] == [q]
    assert feedback['assessments_requiring_review'][0]['reason'] == 'question_scope_changed'
    question=index.read(tid,view='questions',assets=assets)['items'][0]
    assert question['languages']==['zh'] and question['revision']==2
    assert feedback['scope_gaps'][0]['missing_languages'] == ['zh']
    index.annotate(ResearchWrite(**{**assessment,'record_id':'new-scope','languages':['zh']}),assets=assets)
    newer = assets.put('https://a.test/versioned','Changed claim')
    assets.reference_many([newer['version_id']], tid, task_id=tid)
    feedback = index.read(tid,view='feedback',assets=assets)
    assert feedback['questions_without_reviewed_support'] == [q]
    assert any(x['reason'] == 'source_version_changed' for x in feedback['assessments_requiring_review'])
    assert assets.fetch(first['version_id'])['text'] == 'Original claim'
    rows = index.read(tid,view='assessments',assets=assets)['items']
    assert all(x['review_required'] for x in rows)


def test_query_action_reaches_real_trace_receipt(research, monkeypatch):
    from runtime import composition
    from runtime.tool_trace import traced_tool
    from runtime.research_queries import CURRENT_RESEARCH_ACTION
    from runtime.research_ledger import read_task
    tid, index, _ = research
    @traced_tool('kr_web_search')
    def search(query, language='', research_task_id='', **kwargs):
        return {'items':[{'url':'https://a.test/query','title':'candidate'}]}
    monkeypatch.setattr(composition,'kr_web_search', search)
    family = {'family_id':'family','action_id':'action','question_id':'question','language':'zh',
              'query':'中文实践','source_ecology':'generic_web_ecology'}
    result = composition.execute_research_query({'family':family,'research_task_id':tid,'limit':1})
    receipt = result['research_receipt']
    assert receipt['research_action']['action_id'] == 'action'
    assert receipt['research_action']['question_id'] == 'question'
    assert receipt['research_action']['attempt_id']
    saved = next(x for x in read_task(task_id=tid)['tool_receipts'] if x['receipt_id']==receipt['receipt_id'])
    assert saved['research_action'] == receipt['research_action']
    assert not index.read(tid,view='feedback')['unplanned_receipts']
    assert CURRENT_RESEARCH_ACTION.get() is None


def test_query_failed_is_not_zero(research):
    tid,index,_=research
    for key,state in [('a','ok'),('b','failed')]:
        index.query(tid,{'family_id':key},status=state,result={'items':[],'error':'timeout' if state=='failed' else None})
    f=index.read(tid,view='feedback')
    assert f['zero_result_queries']==1 and f['query_outcomes']['failed']==1


def test_read_is_no_write(tmp_path):
    index=ResearchIndex(tmp_path/'absent')
    assert index.read('tid')['status']=='not_initialized'
    assert not index.root.exists()


def test_near_duplicate_numeric_negation_short_protection():
    body='This is a detailed research source about reliable evidence and retrieval. '*5
    items=[{'candidate_id':'a','snippet':body+'salary 20K'}, {'candidate_id':'b','snippet':body+'salary 25K'}, {'candidate_id':'c','snippet':body+'not salary 20K'}, {'candidate_id':'d','snippet':'不支持'}]
    result=suggestions(items)
    assert result and all(x['protected_difference'] for x in result)
    assert all(x['automatic_action']=='none' for x in result)
    assert not any(x['candidate_id']=='d' for x in result)


def test_or_short_chinese_and_multi_passage(tmp_path):
    assets=EvidenceAssets(tmp_path/'assets')
    a=assets.put('https://a.test','招聘 '+('背景 '*350)+' ASR transcription',title='test')
    req=Search(action='search',query='招聘 ASR',match='any')
    result=read(req,assets)
    hit=result['items'][0]
    assert len(hit['passages'])==2
    text=assets.fetch(a['version_id'])['text']
    assert all(text[x['start']:x['end']]==x['text'] for x in hit['passages'])
    assert read(Search(action='search',query='招聘 missing',match='terms'),assets)['items']==[]
    assert read(Search(action='search',query='招聘 missing',match='any'),assets)['items']


def test_stable_search_does_not_skip_after_withdrawal(tmp_path):
    assets=EvidenceAssets(tmp_path/'assets')
    refs=[assets.put(f'https://a.test/{i}','needle '+str(i)) for i in range(5)]
    req=Search(action='search',query='needle',limit=1)
    page=read(req,assets);seen=[page['items'][0]['asset']['version_id']]
    assets.withdraw(page['items'][0]['asset']['source_id'])
    new=assets.put('https://a.test/new','needle new')
    while page['next_cursor']:
        page=read(req.model_copy(update={'cursor':page['next_cursor']}),assets)
        seen.extend(x['asset']['version_id'] for x in page['items'])
    assert set(seen)=={r['version_id'] for r in refs}
    assert new['version_id'] not in seen


def test_snapshot_latest_version_scope_is_fixed(tmp_path):
    assets=EvidenceAssets(tmp_path/'assets')
    refs=[assets.put(f'https://a.test/{i}','needle '+str(i)) for i in range(3)]
    assets.reference_many([x['version_id'] for x in refs],'r',task_id='r')
    req=Search(action='search',query='needle',task_id='r',limit=1)
    page=read(req,assets);seen=[page['items'][0]['asset']['version_id']]
    newer=assets.put('https://a.test/0','needle changed')
    assets.reference_many([newer['version_id']],'r',task_id='r')
    while page['next_cursor']:
        page=read(req.model_copy(update={'cursor':page['next_cursor']}),assets)
        seen.extend(x['asset']['version_id'] for x in page['items'])
    assert set(seen)=={x['version_id'] for x in refs}


def test_firstwave_all_selected_queries_and_async(research,monkeypatch,tmp_path):
    import threading
    from runtime import composition
    from runtime.task_service import TaskService
    from runtime.tasks import TaskStore
    tid,index,_=research
    service=TaskService(TaskStore(str(tmp_path/'tasks.sqlite3')))
    monkeypatch.setattr('runtime.task_service.TaskService',lambda:service)
    release_slow=threading.Event()
    def search(query,**kwargs):
        from runtime.execution_context import check_execution
        if query=='slow':
            # Hold the slow query until cancellation. A wall-clock comparison
            # measures runner load rather than asynchronous task independence.
            deadline=time.monotonic()+10
            while not release_slow.wait(.01):
                check_execution()
                if time.monotonic()>=deadline:
                    raise AssertionError('first wave must return before the slow query finishes')
        return {'items':[{'url':'https://a.test/'+query}],'total':1}
    monkeypatch.setattr(composition,'kr_web_search',search)
    families=[{'family_id':q,'query':q,'source_ecology':'generic_web_ecology','intent':'discovery'} for q in ['slow','fast']]
    try:
        waves=composition._research_first_wave({'query':'q','selected_source_ecologies':['generic_web_ecology'],'query_families':families},3,tid)
        assert len(waves)==2
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            if service.result(waves[1]['task']['task_id'])['status']=='completed':break
            time.sleep(.02)
        assert service.result(waves[1]['task']['task_id'])['status']=='completed'
        assert service.result(waves[0]['task']['task_id'])['status'] in {'running','queued'}
        service.cancel(waves[0]['task']['task_id'])
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            if index.read(tid,view='feedback')['query_outcomes']['failed']:break
            time.sleep(.02)
        assert index.read(tid,view='feedback')['query_outcomes']['failed']==1
    finally:
        release_slow.set()
        service.stop_maintenance()


def test_provider_empty_failure_and_partial_are_distinct():
    from runtime.research_index import query_outcome
    assert query_outcome({'items':[],'error':{'type':'all_providers_failed','details':[{'type':'empty_results'}]}})=='ok'
    assert query_outcome({'items':[],'error':{'type':'all_providers_failed','details':[{'type':'empty_results'},{'type':'timeout'}]}})=='failed'
    assert query_outcome({'items':[{'url':'https://a.test'}],'metadata':{'errors':[{'type':'timeout'}]}})=='partial'


def test_compact_catalog_only_after_persistent_association(research):
    from runtime.research_ledger import submit_tool_result
    from runtime.agent_contracts import compact_evidence
    tid,index,_=research
    all_items=[{'url':f'https://a.test/{i}','title':str(i)} for i in range(130)]
    result={'items':all_items[:2],'acquired_items':all_items}
    assert 'acquired_items' in compact_evidence(result)
    receipt=submit_tool_result(task_id=tid,trace_id='retained',tool='kr_web_search',status='ok',result=result)
    result['research_receipt']={'research_task_id':tid,'candidate_association':receipt['candidate_association']}
    view=compact_evidence(result)
    assert 'acquired_items' not in view and view['candidate_catalog']['acquired_count']==130
    seen=[];page=index.read(tid,limit=100,max_chars=24000)
    while True:
        seen.extend(x['candidate_id'] for x in page['items'])
        if not page['truncated']:break
        page=index.read(tid,limit=100,max_chars=24000,after_seq=page['next_after_seq'],before_seq=page['watermark'])
    assert len(set(seen))==130


def test_projection_failure_repair_does_not_recollect(research,monkeypatch):
    from runtime import research_ledger
    tid,index,_=research
    original=ResearchIndex.ledger_projection
    def fail(*args):raise OSError('disk busy')
    monkeypatch.setattr(ResearchIndex,'ledger_projection',fail)
    research_ledger.record_event(task_id=tid,kind='claim_gap_opened',tool='test')
    marker=index.root/'research_tasks'/(tid+'.projection-pending')
    assert marker.exists()
    monkeypatch.setattr(ResearchIndex,'ledger_projection',original)
    assert research_ledger.repair_pending_projections()==1
    assert not marker.exists()


def test_repeated_plan_does_not_reset_finished_query(research):
    tid,index,_=research
    family={'family_id':'same','query':'same'}
    index.query(tid,family,status='ok',result={'items':[]})
    index.query(tid,family,status='planned')
    assert index.read(tid,view='queries')['items'][0]['status']=='ok'


def test_legacy_candidate_id_stays_addressable(research):
    from runtime import research_ledger
    from generic_web.url_policy import canonical_url
    import hashlib
    tid,index,_=research
    row={'url':'https://a.test/old','title':'old'}
    payload=research_ledger.read_task(task_id=tid)
    payload['candidate_pool']=[{'candidate_id':'candidate-legacy','source_key':hashlib.sha256(canonical_url(row['url']).encode()).hexdigest(),'discovery_receipt_ids':[],'cluster_id':'cluster-old','stage':'selected'}]
    research_ledger._write(payload)
    index.candidates(tid,[row],'web','early-provider')
    record_candidates(task_id=tid,source_ecology='web',tool='kr_web_search',items=[row])
    catalog=index.read(tid)['items']
    assert len(catalog)==1 and catalog[0]['candidate_id']=='candidate-legacy'
    assert catalog[0]['progress']['stage']=='selected'
