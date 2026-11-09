"""两轮隔离的维护合同演练；合成数据，不冒充真实运营或独立 Agent 语义评测。"""
from pathlib import Path
import hashlib
import json

from runtime.evaluation_candidates import observe_task, record_candidate, observation_summary
from runtime.evaluation_maintenance import Maintenance
from runtime.evaluation_quality import compare_trials
from .core import Ledger, write_new
from .maintenance import import_asset, record_trial


def demonstrate(output):
    output=Path(output).resolve()
    output.mkdir(parents=True,exist_ok=False)
    ledger=Ledger(output/'evaluation')
    manager=Maintenance(output/'logs/evaluation-candidates.sqlite3',asset_root=ledger.path/'assets')
    source=output/'authorized-fixtures';source.mkdir()
    def asset(name,text):
        p=source/(name+'.txt');p.write_text(text,encoding='utf-8')
        checksum=hashlib.sha256(p.read_bytes()).hexdigest()
        import_asset(ledger,p,source,checksum)
        return checksum
    examples=[]
    for i in range(2):
        examples.append({'input_hash':asset(f'input-{i}',f'合成演练样本 {i}：登录壳与合法短文。非真实市场资料。'),
                         'bad_hash':asset(f'bad-{i}',f'合成错误 {i}：把登录提示当正文，或按长度误删合法短文。'),
                         'good_hash':asset(f'good-{i}',f'合成正确 {i}：明确登录阻断；保留合法短文。'),
                         'oracle':'controlled-contract-v1','provenance':'synthetic:deterministic-maintenance-demo'})
    def request(operation,cid,revision,**kw):
        return {'operation':operation,'case_id':cid,'expected_revision':revision,
                'request_id':f'{operation}-{cid}-{revision}', 'actor':'fixture-author','context_id':'fixture-context-one',
                'rule_version':'fixture-rule-v1','skill_version':'fixture-skill-v1',
                'reason':'合成样本用于检查维护合同，不代表真实市场或独立语义判断。',
                'evidence':[examples[0]['good_hash']], **kw}
    review={'reviewer':'fixture-oracle','context_id':'fixture-context-two','verdict':'accepted','evidence':[examples[0]['good_hash']]}
    # 第一轮：观察、影子提案、合法题卡与三种不同结果。
    observe_task({'research_task_id':'fixture-task','project_id':'fixture-project','revision':1,'status':'open'},manager.path)
    record_candidate({'tool_name':'get_content_detail','trace_id':'a'*16,'status':'failed','failure_tags':['empty_detail']},manager.path)
    for i,cid in enumerate(['evidence.empty_detail','content.contracts']):
        card={'case_id':cid,'title':('错误壳识别' if i==0 else '合法短文保留')+'（合成演练）',
              'prevents':'防止错误页冒充正文，以及合法短文被机械误删。','layer':'materials','case_type':'regression',
              'critical':True,'coverage':['shell' if i==0 else 'short_text'],'examples':[examples[i]]}
        manager.execute(request('upsert',cid,0,card=card,mode='shadow',request_id='proposal-'+cid))
        manager.execute(request('upsert',cid,0,card=card,review=review))
    trials=[]
    for i,(verdict,reason) in enumerate([('FAIL','漂亮但引用不支持结论'),('UNKNOWN','阻断已披露，无法判断'),('PASS','简短但与固定样本合同一致')]):
        payload={'case_id':'evidence.empty_detail','identity':{'case_revision':1,'rubric':'fixture-rubric-v1',
                 'judge':'deterministic-contract-fixture-not-agent','skill':'fixture-skill-v1','profile':'controlled_fixture',
                 'source_commit':'synthetic-source-identity','session_id':'fixture-session-one','trial_id':f'fixture-trial-{i}',
                 'inputs':[examples[0]['input_hash']]},'criteria':[{'name':'是否忠于样本','verdict':verdict,'critical':True,
                 'rationale':reason,'evidence':[examples[0]['good_hash']]}],'answer_hash':examples[0]['good_hash'],
                 'validity':{'input_verified':True,'oracle_verified':True,'contamination':'none','isolation':'controlled_fixture'}}
        trials.append(payload);record_trial(ledger,manager,payload)
    original=[r['integrity'] for r in ledger.history()]
    # 第二轮：有意错误合并、用户纠错、追加补偿，原成绩不改写。
    merged=manager.execute(request('merge','evidence.empty_detail',1,target_id='content.contracts',target_revision=1,
                                   coverage_map={'shell':'short_text'},review=review))
    feedback=manager.feedback(case_id='evidence.empty_detail',revision=2,text='合并错误，两个方向都要独立保护。',request_id='fixture-feedback')
    restored=manager.execute(request('compensate','evidence.empty_detail',2,change_id=merged['change_id'],
                                      feedback_id=feedback['feedback_id'],human=True))
    manager.execute(request('resolve_feedback','evidence.empty_detail',3,feedback_id=feedback['feedback_id'],
                            change_id=restored['change_id'],rule_version='fixture-rule-v2'))
    observe_task({'research_task_id':'fixture-task','revision':2,'status':'recorded_with_gaps',
                  'closeout':{'completion_facts':{'delivery':'pending'}}},manager.path)
    checks={'compensation_preserved_history':original==[r['integrity'] for r in ledger.history()],
            'both_cards_restored':all(c['lifecycle']=='shadow' for c in manager.view()['cases']),
            'same_context_not_independent':not compare_trials(trials[0],trials[2])['independent'],
            'feedback_processed':manager.view()['feedback'][0]['status']=='processed',
            'delivery_gap_visible':observation_summary(manager.path)['delivery_gaps']==1,
            'changed_rubric_breaks_trend':not compare_trials(trials[0],{**trials[2],'identity':{**trials[2]['identity'],'rubric':'v2'}})['comparable']}
    result={'status':'PASS' if all(checks.values()) else 'FAIL','rounds':2,'real_operations':False,
            'native_acceptance':'NOT_RUN','samples':'synthetic_not_real_market_evidence', 'checks':checks,
            'candidate_store':str(manager.path),'evaluation_store':str(ledger.path),'snapshot':manager.view()}
    write_new(output/'demo-result.json',result)
    return result
