"""真实研究缺陷的合同回归；所有输入和资产均为隔离合成数据。"""
import json

import pytest

from runtime.research_index import ResearchIndex, ResearchWrite
from runtime.research_ledger import open_task, record_tool_receipt


@pytest.fixture
def research(monkeypatch, tmp_path):
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('KR_TASK_DB_PATH', str(tmp_path / 'tasks.sqlite3'))
    monkeypatch.setenv('KR_LOG_DIR', str(tmp_path / 'logs'))
    monkeypatch.setenv('KR_EVIDENCE_ROOT', str(tmp_path / 'evidence'))
    tid = open_task(objective='开发者研究', budget='fast', considered=[], task_id='exam-custom-id')['research_task_id']
    index = ResearchIndex()
    index.annotate(ResearchWrite(action='question', task_id=tid, question_id='q1', text='开发者的真实问题'))
    return tid, index


def write(index, tid, **data):
    from runtime.research_contracts import parse_write
    return index.annotate(parse_write({'task_id': tid, **data}))


def test_custom_id_is_visible_in_all_workspace_entries(research):
    from runtime.research_workspace import ResearchRecords
    tid, _ = research
    records = ResearchRecords()
    assert records.snapshot(tid)['research_task_id'] == tid
    assert tid in [r['research_task_id'] for r in records.recent()['tasks']]
    assert records.files(tid)['research_task_id'] == tid


@pytest.mark.parametrize('bad', ['../exam', 'exam/other', '中文', 'a' * 97, '.'])
def test_task_id_validation_is_shared_and_never_aliases(bad):
    with pytest.raises(ValueError):
        open_task(objective='invalid', budget='fast', considered=[], task_id=bad)


def test_top_level_review_exposes_unreviewed_question(research):
    from runtime.composition import review_research_progress
    tid, _ = research
    result = review_research_progress(tid, 'before_delivery')
    assert result['review_summary']['semantic_review'] == 'gaps'
    assert result['stop_assessment'] != 'registered_checks_satisfied'
    assert result['review_summary']['stop_authority'] == 'agent'


def test_direct_application_call_links_action_and_cleans_context(research):
    from runtime.application import register, invoke
    from runtime.tool_trace import traced_tool
    tid, index = research
    @traced_tool('contract_probe')
    def contract_probe(research_task_id: str = '') -> dict:
        return {'status': 'ok'}
    op = register(contract_probe, group='search', read_only=False, publish=False)
    result = invoke('contract_probe', {'research_task_id': tid,
        'research_context': {'action_id': 'read-experience', 'question_id': 'q1'}}, operation=op)
    assert result['research_receipt']['research_action']['action_id'] == 'read-experience'
    second = invoke('contract_probe', {'research_task_id': tid}, operation=op)
    assert not second['research_receipt']['research_action'].get('action_id')
    assert index.read(tid, view='feedback')['unplanned_receipts_count'] == 1


def test_unassigned_cannot_be_retroactively_washed_as_exploration(research):
    tid, index = research
    receipt = record_tool_receipt(task_id=tid, trace_id='unassigned', tool='kr_web_search', status='ok')['receipt']
    write(index, tid, action='exploration', record_id='explore1', purpose='寻找未知替代思路')
    with pytest.raises(ValueError, match='post_hoc_exploration_forbidden'):
        write(index, tid, action='exploration_disposition', record_id='disposition',
              exploration_id='explore1', receipt_ids=[receipt['receipt_id']], disposition='background', rationale='作为背景')


def test_requirement_partial_scope_does_not_turn_into_full_success(research):
    from kr_core.evidence_assets import EvidenceAssets
    tid, index = research
    assets = EvidenceAssets()
    asset = assets.put('https://example.org/experience', '正文有实际经验，但评论仍未取得。', kind='content')
    assets.reference_many([asset['version_id']], 'task:' + tid, task_id=tid)
    receipt = record_tool_receipt(task_id=tid, trace_id='body-only', tool='get_content_detail', status='ok',
        result={'evidence_assets': [asset]})['receipt']
    write(index, tid, action='requirement', record_id='r1', question_id='q1', text='正文和评论',
          required_parts=['body', 'comments'])
    write(index, tid, action='coverage', record_id='body-coverage', requirement_id='r1',
          requirement_revision=1, receipt_id=receipt['receipt_id'], evidence_refs=[{'version_id':asset['version_id'], 'start':0,'end':10}],
          covered_parts=['body'], rationale='只取得正文')
    state = index.read(tid, view='requirements')['items'][0]
    assert state['acquisition'] == 'partial'
    assert state['missing_parts'] == ['comments']
    assert state['semantic_review'] == 'unreviewed'
    assert index.read(tid, view='feedback')['requirement_gaps_count'] == 1


def test_requirement_revision_reopens_coverage_and_rejects_stale_writes(research):
    tid, index = research
    write(index, tid, action='requirement', record_id='r1', question_id='q1', text='正文', required_parts=['body'])
    write(index, tid, action='requirement', record_id='r1', question_id='q1', text='正文和评论',
          required_parts=['body','comments'], expected_revision=1)
    with pytest.raises(ValueError, match='revision_conflict'):
        write(index, tid, action='requirement', record_id='r1', question_id='q1', text='静默覆盖',
              required_parts=['body'], expected_revision=1)
    assert index.read(tid, view='requirements')['items'][0]['revision'] == 2


def test_failed_attempt_remains_visible_after_requirement_is_satisfied(research):
    from runtime.research_ledger import read_task, close_task
    from kr_core.evidence_assets import EvidenceAssets
    tid,index=research
    failed=record_tool_receipt(task_id=tid, trace_id='first-failure',tool='get_content_detail',status='failed',failure_code='deadline')['receipt']
    write(index,tid,action='requirement',record_id='r1',question_id='q1',text='实际正文',required_parts=['body'])
    write(index,tid,action='attempt',record_id='a1',requirement_id='r1',requirement_revision=1,receipt_id=failed['receipt_id'],rationale='原尝试失败')
    assets=EvidenceAssets();asset=assets.put('https://example.org/second','第二次取得正文。',kind='content')
    assets.reference_many([asset['version_id']],'task:'+tid,task_id=tid)
    ok=record_tool_receipt(task_id=tid,trace_id='second-ok',tool='get_content_detail',status='ok',result={'evidence_assets':[asset]})['receipt']
    write(index,tid,action='coverage',record_id='replacement',requirement_id='r1',requirement_revision=1,
          receipt_id=ok['receipt_id'],evidence_refs=[{'version_id':asset['version_id'],'start':0,'end':6}],covered_parts=['body'],rationale='替代取得正文')
    result=close_task(task_id=tid,ecology_outcomes=[],stop_rationale='带语义缺口交付',key_claims=[])
    assert result['closeout']['requirements']['items'][0]['acquisition']=='fulfilled'
    receipts=read_task(task_id=tid)['tool_receipts']
    assert next(x for x in receipts if x['receipt_id']==failed['receipt_id'])['status']=='failed'


def test_pending_requirements_are_not_reported_as_execution_complete(research):
    from runtime.research_ledger import close_task
    tid,index=research
    write(index,tid,action='requirement',record_id='r1',question_id='q1',text='尚缺评论',required_parts=['comments'])
    result=close_task(task_id=tid,ecology_outcomes=[],stop_rationale='如实交付缺口',key_claims=[])
    assert result['closeout']['completion_facts']['required_execution']=='incomplete'


def test_action_cannot_reference_another_questions_requirement(research):
    from runtime.research_contracts import validate_context
    tid,index=research
    index.annotate(ResearchWrite(action='question',task_id=tid,question_id='q2',text='另一个问题'))
    write(index,tid,action='requirement',record_id='r1',question_id='q1',text='正文',required_parts=['body'])
    with pytest.raises(ValueError,match='requirement_question_mismatch'):
        validate_context(tid,{'action_id':'a','question_id':'q2','requirement_id':'r1'})


def test_post_hoc_association_keeps_original_unassigned_count(research):
    tid,index=research
    r=record_tool_receipt(task_id=tid,trace_id='late-link',tool='kr_web_search',status='ok')['receipt']
    index.annotate(ResearchWrite(action='action',task_id=tid,question_id='q1',action_id='a',receipt_id=r['receipt_id'],rationale='事后确认归属'))
    feedback=index.read(tid,view='feedback')
    assert feedback['original_unassigned_receipts_count']==1
    assert feedback['post_hoc_associations_count']==1
    assert feedback['unplanned_receipts_count']==0


def test_durable_job_persists_action_scope_before_worker_starts(research,monkeypatch):
    from runtime.task_service import TaskService
    from runtime import task_service
    from runtime.research_contracts import context_scope
    from runtime.tasks import get_task_store
    monkeypatch.setenv('KR_TASK_TEST_MODE','1')
    monkeypatch.setattr(task_service,'source_identity',lambda:'test-source')
    monkeypatch.setattr(task_service._MONITORS,'submit',lambda *a,**k:None)
    monkeypatch.setattr(TaskService,'start_maintenance',lambda self:None)
    tid,_=research
    service=TaskService(get_task_store())
    with context_scope(tid,{'action_id':'async-action','question_id':'q1'}):
        task=service.submit('fixture_echo',{},scope={'research_task_id':tid})
    job=json.loads((service.root/task['task_id']/'input.json').read_text(encoding='utf-8'))
    assert job['scope']['research_action']['action_id']=='async-action'
    assert service.store.get_task(task['task_id'])['metadata']['research_action']['question_id']=='q1'


def test_nested_trace_never_inherits_another_research_tasks_action(research):
    from runtime.research_contracts import context_scope
    from runtime.tool_trace import traced_tool
    tid,index=research
    other=open_task(objective='另一个任务',budget='fast',considered=[],task_id='other-task')['research_task_id']
    @traced_tool('nested_probe')
    def nested(research_task_id=''):return {'status':'ok'}
    with context_scope(tid,{'action_id':'only-first-task','question_id':'q1'}):
        result=nested(research_task_id=other)
    assert not result['research_receipt']['research_action'].get('action_id')
    assert index.read(other,view='feedback')['unplanned_receipts_count']==1


def test_worker_restores_task_bound_action_and_records_completion_without_replay(research,monkeypatch):
    from runtime.task_service import TaskService
    from runtime import task_service
    from runtime.research_contracts import context_scope
    from runtime.research_queries import CURRENT_RESEARCH_ACTION
    from runtime.tasks import get_task_store
    from runtime.task_process import execute_job
    from pathlib import Path
    monkeypatch.setenv('KR_TASK_TEST_MODE','1')
    monkeypatch.setattr(task_service,'source_identity',lambda:'test-source')
    monkeypatch.setattr(task_service._MONITORS,'submit',lambda *a,**k:None)
    monkeypatch.setattr(TaskService,'start_maintenance',lambda self:None)
    tid,index=research;service=TaskService(get_task_store())
    with context_scope(tid,{'action_id':'worker-action','question_id':'q1'}):
        row=service.submit('fixture_echo',{},scope={'research_task_id':tid})
    seen=[]
    monkeypatch.setattr('runtime.execution_context.check_execution',lambda:seen.append(dict(CURRENT_RESEARCH_ACTION.get() or {})))
    execute_job(service.root/row['task_id']/'input.json')
    assert seen and all(a.get('_task_id')==tid for a in seen)
    assert not CURRENT_RESEARCH_ACTION.get()
    assert service.store.get_task(row['task_id'])['status']=='completed'
    first=service.record_completion(row['task_id']);second=service.record_completion(row['task_id'])
    assert first['status']==second['status']=='completed'
    actions=[x.get('research_action') or {} for x in __import__('runtime.research_ledger',fromlist=['read_task']).read_task(task_id=tid)['tool_receipts']]
    assert any(a.get('action_id')=='worker-action' for a in actions)


def test_planned_query_also_sets_task_identity(research,monkeypatch):
    from runtime import composition
    from runtime.research_queries import CURRENT_RESEARCH_ACTION
    tid,_=research;seen=[]
    monkeypatch.setattr(composition,'kr_web_search',lambda **kw:seen.append(dict(CURRENT_RESEARCH_ACTION.get() or {})) or {'items':[]})
    composition.execute_research_query({'research_task_id':tid,'limit':1,'family':{
        'family_id':'fixture-family','query':'isolated','source_ecology':'generic_web_ecology','question_id':'q1'}})
    assert seen[0]['_task_id']==tid
    assert not CURRENT_RESEARCH_ACTION.get()


def test_requirement_pages_do_not_drop_later_items(research):
    tid,index=research
    for number in range(3):
        write(index,tid,action='requirement',record_id='r'+str(number),question_id='q1',text='产物'+str(number),required_parts=['body'])
    first=index.read(tid,view='requirements',limit=1)
    second=index.read(tid,view='requirements',limit=1,after_seq=first['next_after_seq'],before_seq=first['watermark'])
    assert first['items'][0]['record_id']!=second['items'][0]['record_id']


def test_closeout_replacement_uses_requirements_beyond_response_budget(research,monkeypatch):
    from runtime.research_ledger import close_task
    from kr_core.evidence_assets import EvidenceAssets
    from types import SimpleNamespace
    tid,index=research
    for number in range(8):
        write(index,tid,action='requirement',record_id='r'+str(number),question_id='q1',
              text='背景范围'*400,semantic_condition='边界说明'*400,required_parts=['body'],required=False)
    write(index,tid,action='requirement',record_id='r-last',question_id='q1',text='必须取得正文',required_parts=['body'])
    failed=record_tool_receipt(task_id=tid,trace_id='failed-bg',tool='get_content_detail',status='failed',
        result={'task':{'task_id':'failed-bg-job'}})['receipt']
    assert failed['background_task_ids']==['failed-bg-job']
    monkeypatch.setattr('runtime.tasks.get_task_store',lambda:SimpleNamespace(
        get_task=lambda job:{'status':'failed','metadata':{},'error_code':'deadline'}))
    write(index,tid,action='attempt',record_id='failed-attempt',requirement_id='r-last',requirement_revision=1,
        receipt_id=failed['receipt_id'],rationale='原后台尝试失败')
    assets=EvidenceAssets();ref=assets.put('https://example.org/replaced','替代成功取得完整正文。')
    assets.reference_many([ref['version_id']],tid,task_id=tid)
    ok=record_tool_receipt(task_id=tid,trace_id='replacement',tool='get_content_detail',status='ok',result={'evidence_assets':[ref]})['receipt']
    write(index,tid,action='coverage',record_id='full',requirement_id='r-last',requirement_revision=1,
        receipt_id=ok['receipt_id'],evidence_refs=[{'version_id':ref['version_id'],'start':0,'end':8}],
        covered_parts=['body'],rationale='同一需求完整替代')
    assert index.read(tid,view='requirements',limit=100,max_chars=24000)['truncated']
    result=close_task(task_id=tid,ecology_outcomes=[],stop_rationale='保留历史失败并确认替代',key_claims=[])
    assert result['closeout']['completion_facts']['required_execution']=='completed'
    assert result['closeout']['required_failed_tasks']==[]
    assert result['closeout']['unsuccessful_background_tasks'][0]['task_id']=='failed-bg-job'


def test_effectiveness_question_record_id_is_reusable(research):
    tid,index=research
    result=write(index,tid,action='question',record_id='market-users',text='匹配人群的实际需求')
    assert result['record_id']=='market-users'
    with pytest.raises(ValueError,match='question_identity_conflict'):
        write(index,tid,action='question',record_id='one',question_id='two',text='不能静默选一个')


def test_effectiveness_empty_plan_is_unknown_not_reviewed(research):
    from runtime.composition import review_research_progress
    tid=open_task(objective='研究',budget='fast',considered=[],task_id='no-scope')['research_task_id']
    result=review_research_progress(tid,'before_delivery')
    assert result['review_summary']['semantic_review']=='unknown'
    assert result['evidence_feedback']['scope_registration']=='missing'


def test_effectiveness_project_projection_distinguishes_missing_identity(research):
    from runtime.research_workspace import ResearchRecords
    tid,_=research
    record=ResearchRecords().snapshot(tid)
    assert record['project_context']['status']=='unclassified'
    open_task(objective='另一项目',budget='fast',considered=[],task_id='other-project',project_id='menslink',project_name='MensLink')
    rows={r['research_task_id']:r for r in ResearchRecords().recent()['tasks']}
    assert rows['other-project']['project_id']=='menslink'
    assert rows['other-project']['project_context']['identity_source']=='explicit'
    assert rows['other-project']['project_context']['archive_ready'] is None


def test_effectiveness_assessment_is_a_valid_evidence_link(research):
    from kr_core.evidence_assets import EvidenceAssets
    tid,index=research
    assets=EvidenceAssets();ref=assets.put('https://example.org/body','这是支持问题的真实定位文本。',kind='content')
    assets.reference_many([ref['version_id']],'task:'+tid,task_id=tid)
    write(index,tid,action='assessment',record_id='a',question_id='q1',claim_id='claim',
          relation='supports_with_limits',evidence_refs=[{'version_id':ref['version_id'],'start':0,'end':8}],
          basis='该段讨论问题',applicability='仅此样本')
    feedback=index.read(tid,view='feedback')
    assert feedback['questions_without_valid_links']==[]



def test_effectiveness_entry_can_seed_declared_scope_before_queries(research):
    from runtime.composition import kr_research
    from runtime.application import invoke
    result=invoke('kr_research',{'task':'比较两地真实用户','research_task_id':'seeded','project_id':'menslink','project_name':'MensLink',
        'questions':[{'question_id':'users','text':'同岗位中外使用者需求','languages':['zh','en'],'populations':['developers']}],
        'queries':[{'query':'知识管理 开发者 痛点','intent':'experience','language':'zh','question_id':'users'}]})
    assert result.get('status')=='ok', result
    row=ResearchIndex().read('seeded',view='questions')['items'][0]
    assert row['record_id']=='users' and row['languages']==['zh','en']
    assert result['research_task']['scope_registration']=='recorded'


def test_effectiveness_capabilities_exposes_packaged_workflow_identity(research):
    from runtime.application import catalog
    import hashlib
    from pathlib import Path
    skill=Path(__file__).resolve().parents[1]/'config/codex-product/plugin/knowledgeradar-research/skills/research/SKILL.md'
    result=catalog()['research_workflow']
    assert result['skill_sha256']==hashlib.sha256(skill.read_bytes()).hexdigest()
    assert result['host_loaded_skill']=='unverified'


def test_effectiveness_entry_reuse_does_not_reset_scope(research):
    from runtime.application import invoke
    args = {'task':'研究', 'research_task_id':'reuse-scope', 'questions':[{'question_id':'users','text':'用户需求','languages':['zh']} ]}
    assert invoke('kr_research', args)['status']=='ok'
    assert invoke('kr_research', args)['status']=='ok'
    row=ResearchIndex().read('reuse-scope',view='questions')['items'][0]
    assert row['revision']==1
    result=invoke('kr_research', {'task':'研究','research_task_id':'reuse-scope'})
    assert result['research_task']['scope_registration']=='recorded'
    assert ResearchIndex().read('reuse-scope',view='questions')['items'][0]['languages']==['zh']
