"""Agent工具、固定检索与create-only交付的隔离业务回归。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import pytest
from kr_core.evidence_assets import EvidenceAssets
from runtime.knowledge import Search,Fetch,read,register_report,guarded_path
from runtime.knowledge_delivery import DeliveryStore,Authorize,Deliver


@pytest.fixture
def corpus(tmp_path):
    assets=EvidenceAssets(tmp_path/'assets')
    old=assets.put('https://example.org/job','BOSS 招聘 AI 工程师。薪资20K。\r\n反例😀。',title='北京招聘')
    new=assets.put('https://example.org/job','BOSS 招聘 AI 工程师。薪资25K。\r\n反例😀。',title='北京招聘')
    other=assets.put('https://example.org/other','BOSS 招聘 AI。薪资10K。',title='旧项目')
    assets.reference_many([old['version_id'],new['version_id']],'task',project_id='项目甲',task_id='任务甲')
    assets.reference_many([other['version_id']],'other',project_id='项目乙',task_id='任务乙')
    return assets,old,new,other


def test_terms_case_scope_versions_and_exact_locators(corpus):
    assets,old,new,_=corpus
    page=read(Search(action='search',query='boss 薪资 ai',project_id='项目甲'),assets)
    assert [x['asset']['version_id'] for x in page['items']]==[new['version_id']]
    hit=page['items'][0]
    full=assets.fetch(new['version_id'])['text']
    assert full[hit['start']:hit['end']]==hit['excerpt']
    assert read(Search(action='search',query='BOSS 薪资',match='phrase'),assets)['items']==[]
    assert len(read(Search(action='search',query='AI',history=True,project_id='项目甲'),assets)['items'])==2
    assert assets.fetch(old['version_id'])['text'].find('20K')>=0


def test_pagination_budget_and_stable_watermark(corpus):
    assets,*_=corpus
    req=Search(action='search',query='招聘',history=True,limit=1,max_chars=2500)
    page=read(req,assets);seen=[page['items'][0]['asset']['version_id']]
    while page['next_cursor']:
        assert len(json.dumps(page,ensure_ascii=False,separators=(',',':')))<=2500
        page=read(req.model_copy(update={'cursor':page['next_cursor']}),assets)
        seen.extend(x['asset']['version_id'] for x in page['items'])
    assert len(seen)==len(set(seen))==3
    cursor=read(req,assets)['next_cursor']
    assets.put('https://example.org/new','招聘新证据')
    assert read(req.model_copy(update={'cursor':cursor}),assets)['items']


def test_not_initialized_and_old_schema_are_not_no_hits(tmp_path):
    a=EvidenceAssets(tmp_path/'empty',read_only=True)
    assert read(Search(action='search'),a)['status']=='not_initialized'
    a.root.mkdir()
    with sqlite3.connect(a.root/'index.sqlite3') as c:c.execute('CREATE TABLE assets(v TEXT)')
    assert read(Search(action='search'),a)['status']=='upgrade_required'


def test_fixed_fetch_entire_response_budget(corpus):
    assets,old,*_=corpus
    data=read(Fetch(action='fetch',version_id=old['version_id'],max_chars=2000),assets)
    assert len(json.dumps(data,ensure_ascii=False,separators=(',',':')))<=2000
    assets.withdraw(old['source_id'])
    with pytest.raises(ValueError,match='withdrawn'):read(Fetch(action='fetch',version_id=old['version_id']),assets)


@pytest.fixture
def delivery(tmp_path,corpus):
    assets,_,new,_=corpus
    source=tmp_path/'report.md';source.write_text('# 总报告\n正文引用与反证。',encoding='utf-8')
    args=dict(assets=assets,input_path=str(source),approved_input_root=str(tmp_path),input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),project_id='项目甲',task_id='任务甲',report_id='main',title='总报告',derived_from=[new['version_id']])
    report=register_report(**args)
    vault=tmp_path/'vault';vault.mkdir()
    store=DeliveryStore(tmp_path/'state',assets)
    grant=store.authorize(Authorize(action='authorize_destination',approved_root=str(vault),project_id='项目甲'))
    request=Deliver(action='deliver',grant_id=grant['grant_id'],project_id='项目甲',task_id='任务甲',version_ids=[report['asset']['version_id'],new['version_id']])
    return store,request,source,args


def test_report_registration_frozen_and_scope_search(delivery):
    store,req,source,args=delivery
    again=register_report(**args)
    assert again['asset']['version_id'] in req.version_ids
    source.write_text('后来改动',encoding='utf-8')
    with pytest.raises(ValueError,match='input_changed'):register_report(**args)
    result=read(Search(action='search',query='反证',kind='report',project_id='项目甲'),store.assets)
    assert len(result['items'])==1
    result=store.deliver(req)
    assert result['status']=='local_committed'
    assert '后来改动' not in ''.join(p.read_text(encoding='utf-8') for p in Path(result['path']).glob('*.md'))


@pytest.mark.parametrize('name',['选取资料.md','report'])
def test_user_changes_never_overwritten(delivery,name):
    store,req,*_=delivery
    first=store.deliver(req);assert first['status']=='local_committed'
    target=Path(first['path'])
    p=target/'总报告.md' if name=='report' else target/name
    p.write_text('USER EDIT',encoding='utf-8')
    second=store.deliver(req)
    assert second['status']=='conflict' and p.read_text(encoding='utf-8')=='USER EDIT'


def test_idempotent_concurrent_delivery_and_real_hash_receipt(delivery):
    store,req,*_=delivery
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:store.deliver(req),range(2)))
    assert all(x['status']=='local_committed' for x in results)
    assert results[0]['delivery_id']==results[1]['delivery_id']
    assert results[0]['remote_synced']=='not_applicable'
    for name,sha in results[0]['file_hashes'].items():assert hashlib.sha256((Path(results[0]['path'])/name).read_bytes()).hexdigest()==sha


def test_post_publish_crash_reconciles_without_replacing(delivery,monkeypatch):
    store,req,*_=delivery
    original=store.save
    def crash(receipt):
        if receipt['status']=='local_committed':raise RuntimeError('simulated_process_exit')
        original(receipt)
    monkeypatch.setattr(store,'save',crash)
    with pytest.raises(RuntimeError):store.deliver(req)
    monkeypatch.setattr(store,'save',original)
    after=store.deliver(req)
    assert after['status']=='local_committed'
    assert store.status(after['delivery_id'])['file_hashes']==after['file_hashes']


def test_revocation_and_cross_project_rejected(delivery):
    store,req,*_=delivery
    with pytest.raises(ValueError,match='not_authorized'):store.deliver(req.model_copy(update={'project_id':'项目乙'}))
    store.revoke(req.grant_id)
    with pytest.raises(ValueError,match='not_authorized'):store.deliver(req)


def test_withdrawn_source_cannot_be_delivered_via_report(delivery):
    store,req,*_=delivery
    original=next(v for v in req.version_ids if store.assets.fetch(v)['asset']['kind']!='report')
    store.assets.withdraw(store.assets.fetch(original)['asset']['source_id'])
    with pytest.raises(ValueError,match='withdrawn'):store.deliver(req)


def test_path_escape_and_same_version_directory_preserved(tmp_path,delivery):
    with pytest.raises(ValueError,match='outside_authorized'):guarded_path(tmp_path,tmp_path/'..'/'other')
    store,req,*_=delivery
    result=store.deliver(req);target=Path(result['path'])
    (target/'human.md').write_text('human',encoding='utf-8')
    assert store.deliver(req)['status']=='local_committed'
    assert (target/'human.md').read_text()=='human'


def test_console_export_is_rejected_before_any_io():
    from onboarding.research_api import workspace_api
    for action in ['export','export_task','export_obsidian']:
        with pytest.raises(ValueError,match='agent_command_required'):workspace_api(None,{'action':action})


def test_mcp_catalog_and_typed_contracts(monkeypatch,tmp_path):
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path))
    import server
    from runtime.agent_contracts import catalog
    actual=asyncio.run(server.mcp.list_tools())
    assert {t.name for t in actual}=={x['name'] for x in catalog()['tools']}
    contract=catalog('read_knowledge')['tool']
    assert '$defs' in contract['input_schema']
    assert contract['output_schema']
    assert len(json.dumps(server.get_capabilities(),ensure_ascii=False))<12000
    assert len(json.dumps(server._health_check_agent_summary_uncached(),ensure_ascii=False))<2500
    with pytest.raises(ValueError,match='task_command_required'):server.get_task_status(task_id='cancel:fake')


def test_mcp_protocol_errors_are_actionable():
    from mcp.server.fastmcp import FastMCP
    from mcp.types import CallToolRequest,CallToolRequestParams
    from runtime.mcp_dispatch import nonblocking_mcp_tool
    from runtime.knowledge import ReadRequest,ReadResult,read
    m=FastMCP('isolated')
    @nonblocking_mcp_tool(m,read_only=True)
    def example(request:ReadRequest)->ReadResult:return read(request)
    handler=m._mcp_server.request_handlers[CallToolRequest]
    response=asyncio.run(handler(CallToolRequest(params=CallToolRequestParams(name='example',arguments={'request':{'action':'search','limit':-1}}))))
    value=response.root
    assert value.isError
    data=json.loads(value.content[0].text)
    assert data['error']['execution_state']=='not_started'
    assert any('limit' in f['field'] for f in data['error']['fields'])


def test_generated_manual_matches_runtime():
    from scripts.generate_tool_manual import generate
    assert generate(check=True)['status']=='PASS'


def test_withdrawal_also_blocks_report_search_and_fetch(delivery):
    store,req,*_=delivery
    report=next(v for v in req.version_ids if store.assets.fetch(v)['asset']['kind']=='report')
    origin=next(v for v in req.version_ids if v!=report)
    store.assets.withdraw(store.assets.fetch(origin)['asset']['source_id'])
    with pytest.raises(ValueError,match='withdrawn'):read(Fetch(action='fetch',version_id=report),store.assets)
    result=read(Search(action='search',kind='report'),store.assets)
    assert result['items']==[] and result['unavailable_count']==1


def test_two_real_processes_share_one_delivery(delivery,tmp_path):
    import subprocess,sys
    store,req,*_=delivery
    data=tmp_path/'request.json';data.write_text(req.model_dump_json(),encoding='utf-8')
    source=Path(__file__).resolve().parents[1]/'src'
    program="import sys,json;sys.path.insert(0,sys.argv[1]);from kr_core.evidence_assets import EvidenceAssets;from runtime.knowledge_delivery import DeliveryStore,Deliver;from pathlib import Path;r=DeliveryStore(Path(sys.argv[2]),EvidenceAssets(Path(sys.argv[3]))).deliver(Deliver.model_validate_json(Path(sys.argv[4]).read_text(encoding='utf-8')));print(json.dumps(r))"
    args=[sys.executable,'-c',program,str(source),str(store.root.parent),str(store.assets.root),str(data)]
    children=[subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8') for _ in range(2)]
    results=[]
    for child in children:
        out,err=child.communicate(timeout=30)
        assert child.returncode==0,err
        results.append(json.loads(out))
    assert all(x['status']=='local_committed' for x in results)
    assert results[0]['delivery_id']==results[1]['delivery_id']
    assert results[0]['file_hashes']==results[1]['file_hashes']


@pytest.mark.skipif(os.name!='nt',reason='Windows sharing contract')
def test_real_windows_read_lock_returns_recoverable_failure(delivery):
    import ctypes
    from ctypes import wintypes
    store,req,*_=delivery
    first=store.deliver(req)
    path=Path(first['path'])/'选取资料.md'
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    api.CreateFileW.restype=wintypes.HANDLE
    api.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=api.CreateFileW(str(path),0x80000000,0,None,3,0,None)
    assert handle!=wintypes.HANDLE(-1).value
    try:
        failed=store.deliver(req)
        assert failed['status']=='retryable_failure' and failed['winerror']==32
    finally:api.CloseHandle(handle)
    assert store.deliver(req)['status']=='local_committed'
    assert hashlib.sha256(path.read_bytes()).hexdigest()==first['file_hashes']['选取资料.md']


@pytest.mark.parametrize('code',[13,28])
def test_permission_and_disk_full_publish_failures_reconcile(delivery,monkeypatch,code):
    import runtime.knowledge_delivery as module
    store,req,*_=delivery
    original=module.os.link
    monkeypatch.setattr(module.os,'link',lambda *a:(_ for _ in ()).throw(OSError(code,'injected')))
    result=store.deliver(req)
    assert result['status']=='retryable_failure' and not list(Path(result['path']).glob('*.md'))
    assert not list(store.root.glob('.kr-delivery-*'))
    monkeypatch.setattr(module.os,'link',original)
    assert store.deliver(req)['status']=='local_committed'


def test_cancellation_before_publish_leaves_no_final_directory(delivery,monkeypatch):
    import runtime.knowledge_delivery as module
    store,req,*_=delivery
    original=module.pin_directory
    from contextlib import contextmanager
    @contextmanager
    def stop(path):
        raise module.ExecutionStopped('deadline_exceeded')
        yield
    monkeypatch.setattr(module,'pin_directory',stop)
    assert store.deliver(req)['status']=='interrupted'
    assert not list(store._grant(req).rglob('*.md'))
    monkeypatch.setattr(module,'pin_directory',original)
    assert store.deliver(req)['status']=='local_committed'


@pytest.mark.skipif(os.name!='nt',reason='Windows junction contract')
def test_destination_junction_cannot_escape_grant(delivery,tmp_path):
    import subprocess
    store,req,*_=delivery
    root=store._grant(req);outside=tmp_path/'outside';outside.mkdir()
    target=root/'未命名项目'
    command="New-Item -ItemType Junction -Path $env:KR_TEST_LINK -Target $env:KR_TEST_TARGET | Out-Null"
    env=dict(os.environ,KR_TEST_LINK=str(target),KR_TEST_TARGET=str(outside))
    result=subprocess.run(['pwsh','-NoProfile','-Command',command],env=env,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    try:
        receipt=store.deliver(req)
        assert receipt['status']=='conflict' and receipt['error_code']=='project_directory_exists_bind_context_with_vault_project_dir'
        assert not list(outside.iterdir()) and not (root/'未命名项目（2）').exists()
        assert list(outside.iterdir())==[]
    finally:target.rmdir()


def test_protocol_full_knowledge_delivery_loop(tmp_path,monkeypatch):
    from mcp.types import CallToolRequest,CallToolRequestParams
    import server
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path/'state'))
    monkeypatch.setenv('KR_EVIDENCE_ROOT',str(tmp_path/'assets'))
    report=tmp_path/'report.md';report.write_text('独立检索总报告，包含可逐字核验的证据。',encoding='utf-8')
    vault=tmp_path/'vault';vault.mkdir()
    handler=server.mcp._mcp_server.request_handlers[CallToolRequest]
    def call(name,request):
        result=asyncio.run(handler(CallToolRequest(params=CallToolRequestParams(name=name,arguments={'request':request})))).root
        assert not result.isError,result.content
        return json.loads(result.content[0].text)
    registered=call('manage_research_artifacts',dict(action='register_report',input_path=str(report),approved_input_root=str(tmp_path),input_sha256=hashlib.sha256(report.read_bytes()).hexdigest(),project_id='项目',task_id='任务',report_id='main',title='报告'))
    version=registered['asset']['version_id']
    hits=call('read_knowledge',dict(action='search',query='独立 检索',project_id='项目'))
    assert hits['items'][0]['asset']['version_id']==version
    fetched=call('read_knowledge',dict(action='fetch',version_id=version))
    assert fetched['text']==report.read_text('utf-8')
    grant=call('manage_research_artifacts',dict(action='authorize_destination',approved_root=str(vault),project_id='项目'))
    receipt=call('manage_research_artifacts',dict(action='deliver',grant_id=grant['grant_id'],project_id='项目',task_id='任务',version_ids=[version]))
    assert receipt['status']=='local_committed'
    status=call('manage_research_artifacts',dict(action='delivery_status',delivery_id=receipt['delivery_id']))
    assert status['file_hashes']==receipt['file_hashes']


def test_installer_leaves_global_instructions_untouched(tmp_path,monkeypatch):
    from scripts import setup_codex_product as install
    global_rules=tmp_path/'AGENTS.md';global_rules.write_text('User rules. Keep byte for byte.',encoding='utf-8')
    before=global_rules.read_bytes()
    monkeypatch.setattr(install,'_git',lambda *args:'candidate')
    result=install.install(apply=True,config_path=tmp_path/'config.toml',plugin_destination=tmp_path/'plugins'/'knowledgeradar-research',backup_dir=tmp_path/'backup')
    assert result['status']=='APPLIED'
    assert global_rules.read_bytes()==before
    assert 'agents' not in result


def test_cancel_control_remains_reachable_when_short_io_full(tmp_path,monkeypatch):
    import runtime.mcp_dispatch as dispatch
    import runtime.task_service as services
    from runtime.tasks import TaskStore
    from mcp.types import CallToolRequest,CallToolRequestParams
    import server
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path/'state'))
    store=TaskStore(str(tmp_path/'tasks.sqlite3'))
    store.upsert_task('cancel-test','fixture','isolated')
    service=services.TaskService(store=store)
    monkeypatch.setattr(services,'TaskService',lambda:service)
    acquired=0
    try:
        while dispatch._PENDING.acquire(blocking=False):acquired+=1
        handler=server.mcp._mcp_server.request_handlers[CallToolRequest]
        result=asyncio.run(handler(CallToolRequest(params=CallToolRequestParams(name='control_task',arguments={'task_id':'cancel-test','action':'cancel'})))).root
        assert not result.isError,result.content
        assert store.get_task('cancel-test')['status']=='cancelled'
    finally:
        for _ in range(acquired):dispatch._PENDING.release()


def test_nested_long_result_retains_fixed_read_reference(corpus):
    from runtime.agent_contracts import compact_evidence
    assets,*_=corpus
    full='固定证据含反例。'*1000
    ref=assets.put('https://example.org/long',full)
    result=compact_evidence({'result':{'content':full,'evidence_assets':[ref]}})['result']
    assert len(result['content'])<len(full)
    assert result['disclosure']['parts'][0]['version_id']==ref['version_id']
    assert assets.fetch(ref['version_id'])['text']==full
    assert compact_evidence({'content':full})['content']==full


def test_output_validation_never_claims_command_not_started():
    from mcp.server.fastmcp import FastMCP
    from mcp.types import CallToolRequest,CallToolRequestParams
    from runtime.mcp_dispatch import nonblocking_mcp_tool
    from pydantic import BaseModel
    class Output(BaseModel):
        count:int
    m=FastMCP('output-failure');executed=[]
    @nonblocking_mcp_tool(m)
    def command()->Output:
        executed.append(True)
        return {'count':'not an integer'}
    handler=m._mcp_server.request_handlers[CallToolRequest]
    result=asyncio.run(handler(CallToolRequest(params=CallToolRequestParams(name='command',arguments={})))).root
    assert executed==[True] and result.isError
    error=json.loads(result.content[0].text)['error']
    assert error['execution_state']=='unknown' and not error['retryable']


def test_new_capture_elsewhere_does_not_hide_task_fixed_version(corpus):
    assets,old,new,_=corpus
    assets.reference_many([old['version_id']],'old-task',project_id='历史项目',task_id='历史任务')
    result=read(Search(action='search',query='薪资',project_id='历史项目',task_id='历史任务'),assets)
    assert [hit['asset']['version_id'] for hit in result['items']]==[old['version_id']]
    before=read(Search(action='search',query='薪资',captured_before=old['captured_at']),assets)
    assert [hit['asset']['version_id'] for hit in before['items']]==[old['version_id']]
