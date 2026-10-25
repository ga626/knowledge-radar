import hashlib
import json
import threading
from http.client import HTTPConnection
import pytest
from runtime.research_ledger import open_task, read_task
from runtime.tool_trace import traced_tool
from onboarding.setup_wizard import WizardServer


def test_registered_research_schema_accepts_optional_codex_title():
    import asyncio
    import server
    tools = asyncio.run(server.mcp.list_tools())
    schema = next(tool.inputSchema for tool in tools if tool.name == 'kr_research')
    assert schema['properties']['display_title']['type'] == 'string'
    assert schema['properties']['display_title']['maxLength'] == 80
    assert 'display_title' not in schema.get('required', [])
    assert schema['properties']['project_name']['type'] == 'string'
    assert schema['properties']['project_name']['maxLength'] == 80
    assert 'project_name' not in schema.get('required', [])


def test_dialogue_route_tool_full_evidence_and_original_finalizer(tmp_path,monkeypatch):
    import server
    from kr_core.evidence_store import EvidenceStore
    from types import SimpleNamespace
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path/'state'))
    monkeypatch.setenv('KR_TOOL_TRACE_PATH',str(tmp_path/'trace.jsonl'))
    store=EvidenceStore(str(tmp_path/'events.jsonl'))
    monkeypatch.setenv('KR_EVIDENCE_ROOT',str(store.assets.root))
    monkeypatch.setattr(server,'evidence_store',store)
    monkeypatch.setattr(server,'_get_evidence_store',lambda:store,raising=False)
    # Keep route deterministic: transport and native host identity have separate receipts.
    monkeypatch.setattr(server,'_research_route_plan',lambda **kwargs:{'considered_source_ecologies':[{'source_ecology':'web'}]})
    route=server.kr_research(task='直接由Codex研究，无网页或认领',display_title='直接对话调研与原文留存',queries=[{'query':'counterexample','language':'en','intent':'counterevidence'}])
    task_id=route['research_task']['research_task_id']
    assert task_id.startswith('kr-research-')
    assert 'finalize_research_task' in route['research_task']['completion_rule']
    assert route['research_task']['display_title']=='直接对话调研与原文留存'
    assert read_task(task_id=task_id)['display_title']=='直接对话调研与原文留存'
    assert server.get_task_status(task_id=task_id,research_action='inspect')['readable_summary']['title']=='直接对话调研与原文留存'
    calls=[]
    @traced_tool('sample_search')
    def search(query,research_task_id=''):
        calls.append(query)
        return {'items':[{'title':'候选'}]}
    discovery=search(query='关键词',research_task_id=task_id)
    assert calls==['关键词']
    assert discovery['research_receipt']['association']=='explicit_task_scope'
    body='中文🙂\n'*5000+'重要末尾反例'
    monkeypatch.setattr(server,'collect_url',lambda *a,**kw:SimpleNamespace(to_mcp_dict=lambda:{'content':body,'title':'fixture','status':'ok'}))
    result=server.extract_web_page(url='https://example.org/fixture',research_task_id=task_id)
    ref=result['evidence_assets'][0]
    fetched=server.get_task_status(research_action='evidence_fetch',research_payload={'version_id':ref['version_id'],'start':len(body)-7})
    assert fetched['text']==body[-7:]
    assert fetched['asset']['raw_sha256']==hashlib.sha256(body.encode()).hexdigest()
    assert server.get_task_status(research_action='evidence_search',research_payload={'query':'末尾反例'})['items'][0]['version_id']==ref['version_id']
    assert server.get_task_status(task_id=task_id,research_action='inspect')['objective']=='直接由Codex研究，无网页或认领'
    receipt=result['research_receipt']['receipt_id']
    assert ref['version_id'] in next(r for r in read_task(task_id=task_id)['tool_receipts'] if r['receipt_id']==receipt)['evidence_versions']
    final=server.finalize_research_task(research_task_id=task_id,ecology_outcomes=[{'source_ecology':'web','outcome':'used','receipt_ids':[receipt]}],stop_rationale='确定性合同测试',key_claims=[{'id':'tail','importance':'high','supporting_evidence_ids':[ref['version_id']],'supporting_receipt_ids':[receipt]}],quality_status='pass')
    assert final['status']=='accepted_for_decision'
    assert not list(tmp_path.rglob('tasks.sqlite3'))


def test_console_reads_same_ledger_rejects_writes_and_preserves_token(tmp_path,monkeypatch):
    monkeypatch.setenv('KR_CONSOLE_ROLE','dev')
    monkeypatch.setenv('KR_CONSOLE_READ_ONLY','1')
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path/'state'))
    monkeypatch.setenv('KR_EVIDENCE_ROOT',str(tmp_path/'assets'))
    task=open_task(objective='Codex已创建的研究',budget='balanced',considered=[])
    path=tmp_path/'state'/'research_tasks'/(task['research_task_id']+'.json');before=path.read_bytes()
    server=WizardServer(('127.0.0.1',0));worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    def post(payload,authorized=True):
        conn=HTTPConnection('127.0.0.1',server.server_port,timeout=5)
        headers={'Content-Type':'application/json','Origin':f'http://127.0.0.1:{server.server_port}'}
        if authorized:headers['X-KR-Setup-Token']=server.setup_token
        conn.request('POST','/api/research',body=json.dumps(payload),headers=headers)
        response=conn.getresponse();data=json.loads(response.read());conn.close();return response.status,data
    try:
        assert post({'action':'list'},False)[0]==403
        assert post({'action':'list'})[1]['tasks'][0]['research_task_id']==task['research_task_id']
        assert post({'action':'inspect','task_id':task['research_task_id']})[1]['objective']==task['objective']
        for action in ['create','claim','heartbeat','reserve','settle','finish','cancel','request_expansion','evidence_withdraw']:
            assert post({'action':action,'task_id':task['research_task_id']})[0]==400
        assert post({'action':'evidence_search'})[1]['items']==[]
        assert path.read_bytes()==before
        assert not (tmp_path/'assets').exists()
    finally:
        server.shutdown();server.server_close();worker.join(3)
