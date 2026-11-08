"""当前产品机制验收；私密治理脚本由治理根测试，不带入用户产品。"""
from runtime.research_index import ResearchIndex, ResearchWrite
from runtime.research_queries import query_families, query_for_ecology
from runtime.research_ledger import open_task, close_task


def test_research_mechanism_smoke_verification_runs(monkeypatch,tmp_path):
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path/'state'))
    task=open_task(objective='自主研究机制',budget='balanced',considered=[])
    tid=task['research_task_id']
    index=ResearchIndex()
    question=index.annotate(ResearchWrite(action='question',task_id=tid,text='中文实践',languages=['zh']))['record_id']
    families=query_families('topic',[{'query':'实践','intent':'experience','source_ecology':'知乎','language':'zh','question_id':question}])
    assert query_for_ecology(families,'zhihu_discussion_ecology')['question_id']==question
    assert query_for_ecology(families,'github_repository_ecology') is None
    feedback=index.read(tid,view='feedback')
    assert feedback['scope_gaps'][0]['missing_languages']==['zh']
    assert feedback['semantic_saturation']=='not_computed'
    result=close_task(task_id=tid,ecology_outcomes=[],stop_rationale='尚缺中文实践',key_claims=[])
    assert result['status']=='recorded_with_gaps'
