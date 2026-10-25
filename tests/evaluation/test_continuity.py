"""U0连接合同；错误回执不能漂绿，线上候选不保存私密输入。"""
import copy
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
import json
from pathlib import Path
import threading
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from scripts.kr_eval import ci
from scripts.kr_eval.continuity import import_history, import_receipt, prepare_native_receipt, publish_summary
from scripts.kr_eval.core import SCHEMA, Ledger, compare, digest, file_hash, load_suite, now, status, write_new
from scripts.kr_eval.runner import run_suite
from runtime.evaluation_candidates import list_candidates, record_candidate, review_candidate
from runtime.evaluation_view import evaluation_store, evaluation_summary


def repository(tmp_path):
    root = tmp_path / 'source'
    (root / 'src').mkdir(parents=True)
    (root / 'src/server.py').write_text('@mcp.tool()\ndef demo():\n    """demo"""\n    return 1\n', encoding='utf-8')
    write_new(root / 'config/evaluation/catalog.json', {'schema': SCHEMA, 'capabilities': [dict(capability_id='demo', version=1, lifecycle='active', entries=['demo'], dependencies=['src/server.py'])]})
    case = dict(case_id='native.demo', version=1, oracle_version=1, capabilities=['demo'], executor='manual', layer='native_mcp', auto=False,
                dependencies=['src/server.py'], success='原生返回真实非空正文和身份', manual_reason='需要原生证据',
                receipt_contract={'tool':'demo', 'assertions':[{'field':'content', 'nonempty':True}], 'identity_fields':{'runtime':'runtime_identity'}})
    write_new(root / 'config/evaluation/cases.json', {'schema': SCHEMA, 'cases': [case]})
    return root, load_suite(root), Ledger(tmp_path / 'ledger')


def receipt(tmp_path, case):
    target = {'kind':'native_mcp', 'source_commit':'a'*40, 'artifact_sha256':'b'*64, 'runtime_identity':'stdio:123:instance'}
    response = {'content':'真实业务正文', 'runtime':target['runtime_identity']}
    write_new(tmp_path / 'response.json', response)
    host_call = {'type':'custom_tool_call','name':'exec','call_id':'host-call-1', 'input':'text(await tools.mcp__knowledgeradar__demo({}));'}
    tool_result = {'content':[{'type':'text','text':json.dumps(response)}]}
    transcript = {'session_id':'host-session-1','request':host_call, 'response':{'call_id':'host-call-1', 'output':[{'text':json.dumps(tool_result)}]}}
    write_new(tmp_path / 'transcript.json', transcript)
    host = {'session_id':'host-session-1','call_id':'host-call-1','tool':'demo','target':target,'access_path':'codex_native_tool',
            'input_sha256':digest({}), 'response_sha256':file_hash(tmp_path / 'response.json'), 'transcript':{'path':'transcript.json','sha256':file_hash(tmp_path / 'transcript.json')}}
    write_new(tmp_path / 'host.json', host)
    envelope = {'schema':'knowledgeradar-evaluation-receipt/v1','case_id':case['case_id'], 'contract_hash':digest(case['receipt_contract']),
                'oracle_version':1,'input_sha256':digest({}), 'checked_at':now(),'target':target,
                'response':{'path':'response.json','sha256':file_hash(tmp_path / 'response.json')},
                'host_record':{'path':'host.json','sha256':file_hash(tmp_path / 'host.json')}}
    write_new(tmp_path / 'receipt.json', envelope)
    return tmp_path / 'receipt.json', target


def test_history_import_preserves_bytes_baseline_and_is_idempotent(tmp_path):
    root, suite, source = repository(tmp_path)
    run = run_suite(root, suite, source)
    source.accept(run['run_id'], 'incomplete reference, not product acceptance')
    before = (source.path / 'runs' / run['run_id'] / 'run.json').read_bytes()
    destination = Ledger(tmp_path / 'canonical')
    assert len(import_history(destination, source.path)['copied']) == 1
    assert not import_history(destination, source.path)['copied']
    assert destination.baseline()['run_id'] == run['run_id']
    assert (destination.path / 'runs' / run['run_id'] / 'run.json').read_bytes() == before
    assert status(root, suite, destination)['gate'] == 'INCOMPLETE'
    evidence = source.path / 'runs' / run['run_id'] / run['results'][0]['evidence'][0]['path']
    evidence.write_text('tamper', encoding='utf-8')
    with pytest.raises(ValueError, match='corrupt'):
        import_history(Ledger(tmp_path / 'bad'), source.path)
    assert not (tmp_path / 'bad/runs').exists()


@pytest.mark.parametrize('verdict', ['FAIL','ERROR','BLOCKED','SKIPPED','NOT_RUN'])
def test_any_pass_degradation_blocks(verdict):
    before = dict(run_id='old', target={'kind':'development_source'}, environment={'fingerprint':'same'}, results=[dict(case_id='x', comparison_identity='same', layer='fixture', verdict='PASS')])
    after = copy.deepcopy(before)
    after['run_id']='new'
    after['results'][0]['verdict']=verdict
    result=compare(before, after)
    assert result['gate']=='BLOCKED'
    assert result['cases'][0]['transition']=='regression'
    after['target']={'kind':'installed'}
    assert compare(before, after)['gate']=='INCOMPLETE'


def test_bound_receipt_import_is_separate_and_detects_tampering(tmp_path):
    root, suite, ledger=repository(tmp_path)
    path, target=receipt(tmp_path, suite['cases'][0])
    run=import_receipt(root,suite,ledger,path,target)
    assert run['counts']=={'PASS':1}
    assert import_receipt(root,suite,ledger,path,target)['run_id']==run['run_id']
    # 原生实测属于旧安装；不能抬高当前开发源的native状态。
    assert status(root,suite,ledger)['cases'][0]['verdict']=='NOT_RUN'
    assert status(root,suite,ledger)['gate']=='INCOMPLETE'
    (ledger.path/'runs'/run['run_id']/'transcript.json').write_text('{}',encoding='utf-8')
    with pytest.raises(ValueError,match='corrupt'):
        ledger.read(run['run_id'])


def test_paired_native_business_failure_is_recorded_not_pass(tmp_path):
    root,suite,ledger=repository(tmp_path)
    suite['cases'][0]['receipt_contract']['assertions'].append({'field':'status','equals':'ok'})
    path,target=receipt(tmp_path,suite['cases'][0])
    run=import_receipt(root,suite,ledger,path,target)
    assert run['counts']=={'FAIL':1}
    assert run['results'][0]['failed_assertions']==['status']


def test_prepare_native_uses_actual_host_pair_and_original_time(tmp_path):
    root,suite,ledger=repository(tmp_path)
    case=suite['cases'][0]
    path,target=receipt(tmp_path,case)
    d=json.loads((tmp_path/'transcript.json').read_text())
    raw=json.loads(json.loads(d['response']['output'][0]['text'])['content'][0]['text'])
    raw['runtime']={'generated_at':now(),'instance_id':target['runtime_identity']}
    case['receipt_contract']['identity_fields']={'runtime.instance_id':'runtime_identity'}
    d['response']['output'][0]['text']=json.dumps({'content':[{'type':'text','text':json.dumps(raw)}]})
    transcript=tmp_path/'session.jsonl'
    transcript.write_text('\n'.join(json.dumps(row) for row in [{'type':'session_meta','payload':{'id':d['session_id']}},{'type':'response_item','payload':d['request']},{'type':'response_item','payload':{**d['response'],'type':'custom_tool_call_output'}}]),encoding='utf-8')
    prepared=prepare_native_receipt(suite,transcript,d['request']['call_id'],case['case_id'],target,tmp_path/'prepared')
    run=import_receipt(root,suite,ledger,prepared['receipt'],target)
    assert run['finished_at']==raw['runtime']['generated_at'] and run['counts']=={'PASS':1}
    with pytest.raises(ValueError,match='missing'):
        prepare_native_receipt(suite,transcript,'absent',case['case_id'],target,tmp_path/'absent')


@pytest.mark.parametrize('fault', ['target','empty','runtime','host_pair','fake_dispatch','response','no_contract','time'])
def test_bad_real_evidence_is_rejected_before_run(tmp_path,fault):
    root,suite,ledger=repository(tmp_path)
    path,target=receipt(tmp_path,suite['cases'][0])
    envelope=json.loads(path.read_text(encoding='utf-8'))
    if fault=='target': target={**target,'artifact_sha256':'wrong'}
    elif fault in {'empty','runtime'}:
        p=tmp_path/'response.json';d=json.loads(p.read_text());d['content' if fault=='empty' else 'runtime']='';p.write_text(json.dumps(d))
        envelope['response']['sha256']=file_hash(p)
    elif fault in {'host_pair','fake_dispatch'}:
        p=tmp_path/'transcript.json';d=json.loads(p.read_text());
        if fault=='host_pair':d['response']['call_id']='wrong'
        else:d['request']['input']='text({status:"ok"});'
        p.write_text(json.dumps(d));hp=tmp_path/'host.json';h=json.loads(hp.read_text());h['transcript']['sha256']=file_hash(p);hp.write_text(json.dumps(h));envelope['host_record']['sha256']=file_hash(hp)
    elif fault=='response':envelope['response']['sha256']='wrong'
    elif fault=='no_contract':suite['cases'][0].pop('receipt_contract')
    elif fault=='time':envelope['checked_at']=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
    path.write_text(json.dumps(envelope))
    with pytest.raises(ValueError):import_receipt(root,suite,ledger,path,target)
    assert not (ledger.path/'runs').exists()


def test_candidate_collection_is_bounded_private_and_reviewed(tmp_path):
    path=tmp_path/'candidates.sqlite3'
    event={'event_type':'tool_execution','tool_name':'search_xiaohongshu','trace_id':'a'*16,'status':'failed','failure_tags':['login_required'],
           'failure_code':'token=private','metadata':{'query':'private','url':'private','profile_id':'secret'}}
    record_candidate(event,path);record_candidate(event,path)
    assert list_candidates(path)[0]['occurrences']==1
    record_candidate({**event,'trace_id':'b'*16},path)
    rows=list_candidates(path)
    assert len(rows)==1 and rows[0]['occurrences']==2
    assert 'private' not in str(rows) and 'secret' not in str(rows)
    with pytest.raises(ValueError):review_candidate(path,rows[0]['candidate_id'],case='existing',reviewer='agent',reason='test')
    review=review_candidate(path,rows[0]['candidate_id'],case='existing',reviewer='agent',reason='existing regression',input_sha256='a'*64,oracle_version=1)
    assert review['gold_promoted'] is False
    with pytest.raises(ValueError):review_candidate(path,rows[0]['candidate_id'],reviewer='agent',reason='again')
    import runtime.evaluation_candidates as candidates
    old=candidates.LIMIT
    try:
        candidates.LIMIT=3
        for i in range(8):record_candidate({**event,'tool_name':f'tool_{i}'},path)
        assert len(list_candidates(path))==3
    finally:candidates.LIMIT=old


def test_store_resolution_and_readonly_summary(tmp_path,monkeypatch):
    monkeypatch.delenv('KR_EVALUATION_STORE',raising=False)
    assert evaluation_store(tmp_path/'.codex/public-repos/product')==tmp_path/'local/evaluation'
    root,suite,ledger=repository(tmp_path)
    monkeypatch.setenv('KR_EVALUATION_STORE',str(ledger.path))
    assert evaluation_summary(root)['availability']=='missing'
    run_suite(root,suite,ledger);publish_summary(root,suite,ledger)
    before={p:p.stat().st_mtime_ns for p in ledger.path.rglob('*') if p.is_file()}
    result=evaluation_summary(root)
    assert result['gate']=='INCOMPLETE' and result['availability']=='snapshot'
    assert 'dependency_identity' not in str(result)
    assert before=={p:p.stat().st_mtime_ns for p in ledger.path.rglob('*') if p.is_file()}
    (root/'src/new.py').write_text('changed=True',encoding='utf-8')
    assert evaluation_summary(root)['availability']=='stale_snapshot'
    p=ledger.path/'summary.json';d=json.loads(p.read_text(encoding='utf-8'));d['gate']='PASS';p.write_text(json.dumps(d))
    assert evaluation_summary(root)['availability']=='invalid'


def test_existing_speech_assets_are_preserved_without_gold_promotion(tmp_path):
    from scripts.kr_eval.continuity import import_speech_dataset
    source=tmp_path/'speech';source.mkdir();audio=source/'sample.wav';audio.write_bytes(b'fixture audio identity only')
    write_new(source/'manifest.json',{'dataset_revision':'fixed','license':'fixture','samples':[{'id':'sample','path':str(audio),'audio_sha256':file_hash(audio),'language':'en','duration_s':1}]})
    write_new(source/'human-annotations.json',{'status':'PENDING_HUMAN_REVIEW','samples':[{'id':'sample','audio_sha256':file_hash(audio),'human_review_status':'PENDING','human_reference':None}]})
    ledger=Ledger(tmp_path/'ledger')
    result=import_speech_dataset(ledger,source/'manifest.json')
    assert result['human_gold_available']==0 and result['quality_verdict']=='NOT_RUN'
    assert import_speech_dataset(ledger,source/'manifest.json')['snapshot']==result['snapshot']
    audio.write_bytes(b'tamper')
    with pytest.raises(ValueError):import_speech_dataset(ledger,source/'manifest.json')


def test_ci_maps_one_execution_and_does_not_green_uncollected(tmp_path,monkeypatch):
    root,suite,ledger=repository(tmp_path)
    (root/'tests').mkdir();(root/'tests/test_sample.py').write_text('def test_ok(): pass',encoding='utf-8')
    case={**suite['cases'][0],'case_id':'fixture.sample','executor':'pytest','layer':'fixture','auto':True,'nodes':['tests/test_sample.py::test_ok']}
    suite['cases'].append(case)
    calls=[]
    def fake_pytest(root,case,folder):
        calls.append(case['nodes']);(folder/'junit.xml').write_text('<testsuites><testsuite><testcase classname="tests.test_sample" name="test_ok" /></testsuite></testsuites>')
        return {'verdict':'PASS','returncode':0}
    monkeypatch.setattr(ci,'_pytest',fake_pytest)
    run,passed=ci.run_ci(root,suite,ledger,['tests/test_sample.py'])
    assert passed and calls==[['tests/test_sample.py']]
    assert run['counts']=={'NOT_RUN':1,'PASS':1}
    assert ci._matches(__import__('xml.etree.ElementTree',fromlist=['Element']).Element('testcase',classname='tests.test_sample',name='test_other'),'tests/test_sample.py::test_ok') is False


def test_ci_real_junit_single_batch(tmp_path):
    import shutil
    root,suite,ledger=repository(tmp_path)
    shutil.copytree(ROOT/'scripts/kr_eval',root/'scripts/kr_eval',ignore=shutil.ignore_patterns('__pycache__'))
    (root/'tests').mkdir()
    (root/'tests/test_business.py').write_text('def test_empty():\n    assert not "".strip()\n',encoding='utf-8')
    suite['cases'].append({**suite['cases'][0],'case_id':'fixture.empty','executor':'pytest','auto':True,'layer':'fixture','nodes':['tests/test_business.py::test_empty']})
    run,passed=ci.run_ci(root,suite,ledger,['tests/'])
    assert passed and run['counts']=={'NOT_RUN':1,'PASS':1}
    assert len(list((ledger.path/'runs'/run['run_id']).glob('**/junit.xml')))==1
    summary=status(root,suite,ledger)
    native=next(c for c in summary['cases'] if c['case_id']=='native.demo')
    assert native['checked_at'] is None and native['applicability']=='unknown'


def test_evaluation_endpoint_requires_page_token(tmp_path,monkeypatch):
    from onboarding.setup_wizard import WizardServer
    monkeypatch.setenv('KR_CONSOLE_ROLE','dev')
    monkeypatch.setenv('KR_EVALUATION_STORE',str(tmp_path/'absent'))
    server=WizardServer(('127.0.0.1',0));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        conn=HTTPConnection('127.0.0.1',server.server_port,timeout=5)
        conn.request('GET','/api/evaluation');response=conn.getresponse();assert response.status==403;response.read()
        conn.request('GET','/api/evaluation',headers={'X-KR-Setup-Token':server.setup_token})
        response=conn.getresponse();payload=json.loads(response.read())
        assert response.status==200 and payload['gate']=='INCOMPLETE'
    finally:server.shutdown();server.server_close();thread.join(timeout=5)


@pytest.mark.parametrize('xml,verdict', [('<failure/>','FAIL'),('<error/>','ERROR'),('<skipped/>','SKIPPED')])
def test_ci_failures_skips_and_missing_nodes_never_pass(tmp_path,monkeypatch,xml,verdict):
    root,suite,ledger=repository(tmp_path)
    (root/'tests').mkdir();(root/'tests/test_one.py').write_text('def test_one(): pass',encoding='utf-8')
    suite['cases']=[{**suite['cases'][0],'executor':'pytest','auto':True,'layer':'fixture','nodes':['tests/test_one.py::test_one']}]
    def execute(root,case,folder):
        (folder/'junit.xml').write_text('<testsuites><testcase classname="tests.test_one" name="test_one">'+xml+'</testcase></testsuites>')
        return {'verdict':verdict,'returncode':1 if verdict in {'ERROR','FAIL'} else 0}
    monkeypatch.setattr(ci,'_pytest',execute)
    run,passed=ci.run_ci(root,suite,ledger)
    assert not passed and run['counts']=={verdict:1}


def test_docs_only_trigger_is_fail_closed(tmp_path,monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(ci.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=0,stdout='docs/reference/report.md\nREADME.md\n'))
    assert ci.docs_only_change(tmp_path,'base')
    monkeypatch.setattr(ci.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=0,stdout='src/algorithm.md\n'))
    assert not ci.docs_only_change(tmp_path,'base')
    monkeypatch.setattr(ci.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=1,stdout=''))
    assert not ci.docs_only_change(tmp_path,'base')


def test_ci_workflow_preserves_checks_and_single_invocation():
    text=(ROOT/'.github/workflows/ci.yml').read_text(encoding='utf-8-sig')
    assert text.count('scripts/kr_evaluate.py --store local/evaluation ci ')==1
    assert 'run: python -m pytest' not in text
    assert '--extra-node tests/evaluation' in text and '--extra-node tests/test_product_install.py' in text
    assert 'if: always()' in text and 'path: local/evaluation' in text
    # 新增构建步骤不应让合同依赖步骤总数；所有产品步骤都必须保留文档跳过门禁。
    product_steps = (
        'Install package and test tools', 'Compile public source',
        'Set up console build runtime', 'Check console source and reconnect behavior',
        'Verify public package plan', 'Prepare hash-locked offline runtime',
        'Build immutable release candidate', 'Verify the generated candidate',
        'Upload verified candidate',
    )
    for name in product_steps:
        step = next(block for block in text.split('      - name: ') if block.startswith(name + '\n'))
        assert "if: steps.scope.outputs.docs_only != 'true'" in step, name


def test_extra_governance_failure_blocks_summary_compare_and_survives_import(tmp_path, monkeypatch):
    root, suite, ledger = repository(tmp_path)
    (root/'tests').mkdir()
    (root/'tests/test_extra.py').write_text('def test_extra(): assert False', encoding='utf-8')
    def fake_pytest(root, case, folder):
        (folder/'junit.xml').write_text('<testsuites><testcase classname="tests.test_extra" name="test_extra"><failure/></testcase></testsuites>')
        return {'verdict':'FAIL','returncode':1}
    monkeypatch.setattr(ci, '_pytest', fake_pytest)
    run, passed = ci.run_ci(root, suite, ledger, ['tests/test_extra.py'])
    assert not passed and run['counts'] == {'NOT_RUN':1}
    summary = status(root, suite, ledger)
    assert summary['gate']=='BLOCKED' and summary['ci_gate']['verdict']=='FAIL'
    assert compare(run, run)['gate']=='BLOCKED'
    destination = Ledger(tmp_path/'imported')
    import_history(destination, ledger.path)
    copied = destination.read(run['run_id'])
    assert copied['integrity']==run['integrity']
    (destination.path/'runs'/run['run_id']/'pytest/junit.xml').write_text('corrupt')
    with pytest.raises(ValueError, match='evidence missing/corrupt'):
        destination.read(run['run_id'])


def test_ci_separates_owned_host_process_without_repeating_offline_tests(tmp_path, monkeypatch):
    root, suite, ledger = repository(tmp_path)
    (root/'tests').mkdir()
    (root/'tests/test_product_console_launcher.py').write_text('pass', encoding='utf-8')
    calls=[]
    def fake_pytest(root, case, folder):
        calls.append(case)
        (folder/'junit.xml').write_text('<testsuites><testcase classname="tests.test_product_console_launcher" name="test_real_dev_supervisor_starts_restarts_and_stops_without_touching_stable"/></testsuites>')
        return {'verdict':'PASS','returncode':0}
    monkeypatch.setattr(ci, '_pytest', fake_pytest)
    run, passed=ci.run_ci(root,suite,ledger,['tests/test_product_console_launcher.py'])
    assert passed and len(calls)==2
    assert calls[0]['deselect']==[ci.HOST_NODE]
    assert calls[1]['nodes']==[ci.HOST_NODE] and calls[1]['owned_host_process']
    assert len(run['ci_pytest']['batches'])==2
