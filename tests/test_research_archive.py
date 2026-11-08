"""任务归属、全文分卷、双端交付与无副作用恢复的业务验收。"""
import hashlib
import json
import os
import re
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import pytest
from kr_core.evidence_assets import EvidenceAssets
from runtime.knowledge_delivery import DeliveryStore, Authorize, Deliver
from runtime.research_archive import ResearchArchive, BindContext, SubmitReport, calendar_layout
from runtime.research_backup import SnapshotResearch, RestoreResearch, snapshot, restore
from runtime import research_ledger as ledger


TASK_STAMP = calendar_layout({'created_at':'2026-11-02T00:00:00+00:00'},'完整测试')['directory_title']
TASK_RELATIVE = Path('2026年') / TASK_STAMP


@pytest.fixture
def archive(tmp_path,monkeypatch):
    monkeypatch.setattr(ledger,'_now',lambda:'2026-11-02T00:00:00+00:00')
    state=tmp_path/'state';state.mkdir()
    monkeypatch.setenv('KR_STATE_DIR',str(state))
    monkeypatch.setenv('KR_LOG_DIR',str(tmp_path/'logs'))
    monkeypatch.delenv('KR_TASK_DB_PATH', raising=False)
    monkeypatch.setenv('KR_EVIDENCE_ROOT',str(state/'evidence_assets'))
    monkeypatch.setenv('KR_OWNER_ROOT',str(state/'execution_owner'))
    assets=EvidenceAssets()
    store=DeliveryStore(state,assets)
    engine=ResearchArchive(store)
    local=tmp_path/'project';local.mkdir()
    vault=tmp_path/'vault';vault.mkdir()
    local_grant=store.authorize(Authorize(action='authorize_destination',approved_root=str(local),project_id='project-a'))
    vault_grant=store.authorize(Authorize(action='authorize_destination',approved_root=str(vault),project_id='project-a'))
    ledger.open_task(objective='验证归档',budget='balanced',considered=[],task_id='task-a',project_id='project-a',project_name='同名项目')
    request=BindContext(action='bind_context',task_id='task-a',project_id='project-a',project_name='同名项目',task_title='完整测试',
        local_grant_id=local_grant['grant_id'],local_report_path=str(local/'reports'/'chosen-name.md'),vault_grant_id=vault_grant['grant_id'])
    engine.bind(request)
    return engine,request,local,vault


def evidence(engine, text='证据原文。\r\n😀\n反证不删除',url='https://example.org/source'):
    ref=engine.assets.put(url,text,title='原始资料',metadata={'coverage':'complete'})
    engine.assets.reference_many([ref['version_id']],'task-a',project_id='project-a',task_id='task-a')
    return ref


def test_late_vault_binding_replays_all_fixed_local_rounds(archive):
    engine, request, local, vault = archive
    # 模拟日用任务：原文先存入KR，完成多个本地轮次后才授权Vault。
    context = engine._context('task-a')
    context['vault_grant_id'] = ''
    context['vault'] = {'status': 'not_configured'}
    engine._save(context)
    first = evidence(engine, '未引用的正文及反证')
    engine.run('task-a')
    second = evidence(engine, '第二轮有效全文', 'https://example.org/second')
    engine.run('task-a')
    submit(engine)
    engine.bind(request)
    for _ in range(4):
        engine.maintain(limit=20)
    result = engine.run('task-a', retry=True)
    assert not result['error_code']
    for record in result['rounds']:
        assert record.get('path') != str(engine.assets.root)
    restored = {}
    for record in result['rounds']:
        restored.update(reconstruct(Path(record['path'])))
    assert restored[first['version_id']] == '未引用的正文及反证'.encode()
    assert restored[second['version_id']] == '第二轮有效全文'.encode()
    assert result['vault']['status'] == 'local_committed'
    before = {str(p): p.read_bytes() for p in vault.rglob('*.md')}
    engine.run('task-a', retry=True)
    assert before == {str(p): p.read_bytes() for p in vault.rglob('*.md')}


def test_structured_comment_coverage_does_not_abort_full_archive(archive):
    engine,*_=archive
    ref=engine.assets.put('https://example.org/comments',json.dumps([{'id':'1','text':'反证评论全文'}]),kind='comments',title='评论',metadata={'coverage':{'returned':1,'has_more':False,'request_satisfied':True}})
    engine.assets.reference_many([ref['version_id']],'task-a',project_id='project-a',task_id='task-a')
    result=engine.run('task-a')
    assert not result['error_code']
    folder=Path(result['rounds'][0]['path'])
    assert '反证评论全文' in next(folder.glob('网页 - *.md')).read_text(encoding='utf-8')


def submit(engine,**kw):
    return engine.submit(SubmitReport(action='submit_report',task_id='task-a',project_id='project-a',title='最终报告',content='# 结论\n保留原文和缺口。\n',**kw))


def reconstruct(folder):
    restored={}
    seen=set()
    if (folder/'sources.jsonl').is_file():
        rows=[json.loads(line) for line in (folder/'sources.jsonl').read_text(encoding='utf-8').splitlines()]
    else:
        db=Path(os.environ['KR_STATE_DIR'])/'knowledge_delivery/delivery.sqlite3'
        with sqlite3.connect(db) as c:
            rows=[json.loads(row[0]) for row in c.execute("SELECT p.payload FROM archive_parts p JOIN archive_rounds r ON r.id=p.round_id WHERE json_extract(r.payload,'$.path')=? ORDER BY r.rowid,p.ordinal",(str(folder),))]
    for row in rows:
        key=(row['version_id'],row['raw_start'])
        if key in seen:continue
        seen.add(key)
        raw=(folder/row['file']).read_bytes()[row['export_start']:row['export_end']]
        assert hashlib.sha256(raw).hexdigest()==row['part_sha256']
        previous=restored.get(row['version_id'],b'')
        assert len(previous)==row['raw_start']
        restored[row['version_id']]=previous+raw
        if row['raw_end']==row['raw_bytes']:
            assert hashlib.sha256(restored[row['version_id']]).hexdigest()==row['raw_sha256']
    return restored


def test_full_text_and_exact_project_report_same_bytes(archive):
    engine,request,local,vault=archive
    ref=evidence(engine)
    first=engine.run('task-a')
    assert first['summary']=='summary_pending'
    folder=Path(first['rounds'][0]['path'])
    assert reconstruct(folder)[ref['version_id']]==engine.assets.fetch(ref['version_id'])['text'].encode()
    result=submit(engine,source_round=first['rounds'][0]['round_id'])
    assert result['summary']=='delivered'
    assert Path(result['local']['path'])==local/'reports/chosen-name.md'
    assert Path(result['local']['path']).read_bytes()==Path(result['vault']['path']).read_bytes()
    assert len(list(local.rglob('*.md')))==1
    assert not list(vault.rglob('*.mp4'))


def test_two_projects_same_display_name_and_cross_task_rejected(archive):
    engine,req,local,vault=archive
    with pytest.raises(ValueError,match='project_mismatch'):
        ledger.open_task(objective='换项目',budget='fast',considered=[],task_id='task-a',project_id='project-b',project_name='同名项目')
    ledger.open_task(objective='第二项目',budget='fast',considered=[],task_id='task-b',project_id='project-b',project_name='同名项目')
    assert ledger.read_task(task_id='task-b')['project_id']=='project-b'
    with pytest.raises(ValueError,match='project_mismatch'):
        engine.submit(SubmitReport(action='submit_report',task_id='task-a',project_id='project-b',title='错项目',content='wrong'))
    assert not list(local.rglob('*.md'))


def test_missing_context_and_queries_do_not_create_state(tmp_path,monkeypatch):
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path/'state'))
    engine=ResearchArchive(DeliveryStore(tmp_path/'state',EvidenceAssets(tmp_path/'assets')))
    assert engine.status('unknown')['status']=='context_pending'
    assert not (tmp_path/'state').exists()


def test_closed_without_agent_summary_stays_pending(archive):
    engine,*_=archive
    evidence(engine)
    ledger.close_task(task_id='task-a',ecology_outcomes=[],stop_rationale='证据足够',key_claims=[])
    engine.maintain()
    result=engine.status('task-a')
    assert result['research']=='stopped' and result['summary']=='summary_pending'
    assert result['report'] is None


def test_late_evidence_new_round_leaves_old_immutable(archive):
    engine,*_=archive
    first=evidence(engine)
    initial=engine.run('task-a');folder=Path(initial['rounds'][0]['path']);before={x.name:x.read_bytes() for x in folder.iterdir()}
    second=evidence(engine,'迟到的转写正文','https://example.org/late')
    engine.maintain()
    rounds=engine.status('task-a')['rounds']
    assert len(rounds)==2
    assert all((folder/name).read_bytes()==raw for name,raw in before.items())
    assert {r for item in rounds for r in reconstruct(Path(item['path']))}=={first['version_id'],second['version_id']}


@pytest.mark.parametrize('kind',['report','volume','directory'])
def test_human_edits_never_overwritten(archive,kind):
    engine,*_=archive;evidence(engine);result=submit(engine)
    folder=Path(result['rounds'][0]['path'])
    path={'report':Path(result['vault']['path']),'volume':next(folder.glob('网页 - *.md')),'directory':folder/'资料导航.md'}[kind]
    path.write_text('USER HEARTWORK',encoding='utf-8')
    assert engine.run('task-a',retry=True)['error_code']=='destination_modified_preserved'
    assert path.read_text(encoding='utf-8')=='USER HEARTWORK'


def test_move_then_delete_never_recreates_old_path(archive):
    engine,*_=archive;evidence(engine);result=submit(engine)
    report=Path(result['vault']['path']);moved=report.with_name('人改名字.md');report.rename(moved)
    result=engine.run('task-a',retry=True)
    assert result['vault']['path']==str(moved) and not report.exists()
    moved.unlink()
    assert engine.run('task-a',retry=True)['error_code']=='destination_missing_preserved_no_recreation'
    assert not moved.exists() and not report.exists()


def test_round_directory_moved_and_regrant_idempotent(archive):
    engine,req,local,vault=archive;evidence(engine);initial=engine.run('task-a')
    folder=Path(initial['rounds'][0]['path']);moved=folder.with_name('移动后的轮次');folder.rename(moved)
    fresh=engine.store.authorize(Authorize(action='authorize_destination',approved_root=str(vault),project_id='project-a'))
    engine.bind(req.model_copy(update={'vault_grant_id':fresh['grant_id']}))
    result=engine.run('task-a',retry=True)
    assert result['rounds'][0]['path']==str(moved) and not folder.exists()
    assert len(result['rounds'])==1


def test_disk_full_retry_fixed_report_without_model(archive,monkeypatch):
    engine,*_=archive;evidence(engine)
    import runtime.research_archive as module
    original=module.admit_bytes
    monkeypatch.setattr(module,'admit_bytes',lambda *a,**kw:(_ for _ in ()).throw(OSError(28,'full')))
    failed=submit(engine)
    assert failed['error_code']=='storage_space_insufficient'
    version=failed['report']['version_id']
    monkeypatch.setattr(module,'admit_bytes',original)
    result=engine.run('task-a',retry=True)
    assert result['summary']=='delivered' and result['report']['version_id']==version


def test_crash_after_publish_reconciles_existing_round(archive,monkeypatch):
    engine,*_=archive;evidence(engine)
    original=engine._round_save
    def crash(key,payload):
        if payload.get('status')=='local_committed':raise RuntimeError('simulated_process_exit')
        return original(key,payload)
    monkeypatch.setattr(engine,'_round_save',crash)
    with pytest.raises(RuntimeError,match='simulated'):engine.run('task-a')
    monkeypatch.setattr(engine,'_round_save',original)
    result=engine.run('task-a',retry=True)
    assert len(result['rounds'])==1 and result['rounds'][0]['status']=='local_committed'


def test_one_endpoint_crash_then_other_resumes(archive,monkeypatch):
    engine,*_=archive;evidence(engine)
    original=engine._report_endpoint
    def crash(ctx,key,*args):
        if key=='vault':raise RuntimeError('after_local_commit')
        return original(ctx,key,*args)
    monkeypatch.setattr(engine,'_report_endpoint',crash)
    with pytest.raises(RuntimeError):submit(engine)
    before=engine.status('task-a');identity=before['local']['identity']
    monkeypatch.setattr(engine,'_report_endpoint',original)
    after=engine.run('task-a',retry=True)
    assert after['summary']=='delivered' and after['local']['identity']==identity


def test_concurrent_retries_one_physical_report(archive):
    engine,*_=archive;evidence(engine);submit(engine)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:engine.run('task-a',retry=True),range(4)))
    assert all(x['summary']=='delivered' for x in results)
    assert len({x['vault']['identity'][1] for x in results})==1


def test_no_obsidian_independent_knowledge_and_report(archive):
    engine,req,local,_=archive
    # 独立任务，不把已经接受的Vault义务静默取消。
    ledger.open_task(objective='独立',budget='fast',considered=[],task_id='solo',project_id='project-a')
    engine.bind(req.model_copy(update={'task_id':'solo','vault_grant_id':'','local_report_path':str(local/'solo.md')}))
    result=engine.submit(SubmitReport(action='submit_report',task_id='solo',project_id='project-a',title='独立',content='KR独立可用'))
    assert result['summary']=='delivered' and result['vault']['status']=='not_configured'
    assert (local/'solo.md').read_text(encoding='utf-8')=='KR独立可用'


def test_backup_restore_provenance_user_edits_and_authorization(archive,tmp_path):
    engine,*_=archive;ref=evidence(engine);result=submit(engine)
    edited=Path(result['vault']['path']);edited.write_text('USER LATER VERSION',encoding='utf-8')
    from runtime.research_archive import RebindReportOutput
    engine.rebind_report_output(RebindReportOutput(action='rebind_report_output',task_id='task-a',project_id='project-a',vault_report_path=str(edited.with_name('explicit-recovery.md'))))
    backup=tmp_path/'backups';backup.mkdir()
    grant=engine.store.authorize(Authorize(action='authorize_destination',approved_root=str(backup),project_id='project-a'))
    receipt=snapshot(engine.store,SnapshotResearch(action='snapshot_research',backup_grant_id=grant['grant_id'],project_id='project-a',backup_name='first',include_delivered_user_versions=True))
    destination=tmp_path/'restored'
    restored=restore(RestoreResearch(action='restore_research',approved_backup_root=str(backup),snapshot_path=receipt['path'],approved_restore_root=str(tmp_path),restore_path=str(destination)))
    assert restored['status']=='restored_inactive'
    assert EvidenceAssets(destination/'evidence_assets',read_only=True).fetch(ref['version_id'])['text']
    with sqlite3.connect(destination/'knowledge_delivery/delivery.sqlite3') as c:
        assert c.execute('SELECT COUNT(*) FROM grants WHERE active=1').fetchone()[0]==0
        context=json.loads(c.execute('SELECT payload FROM research_context').fetchone()[0])
        assert context['restored_requires_reauthorization']
        for table in ('archive_parts','archive_display_names'):
            with engine.store.connection() as original:
                expected=original.execute('SELECT * FROM '+table+' ORDER BY 1,2').fetchall()
            assert expected
            assert c.execute('SELECT * FROM '+table+' ORDER BY 1,2').fetchall()==[tuple(row) for row in expected]
    assert any(p.read_text(encoding='utf-8')=='USER LATER VERSION' for p in (destination/'user_versions').glob('*.md'))
    assert edited.read_text(encoding='utf-8')=='USER LATER VERSION'
    # 不允许“恢复”覆盖现有目录。
    with pytest.raises(ValueError,match='new_directory'):
        restore(RestoreResearch(action='restore_research',approved_backup_root=str(backup),snapshot_path=receipt['path'],approved_restore_root=str(tmp_path),restore_path=str(destination)))


def test_legacy_delivery_move_delete_and_regrant(archive):
    engine,req,_,vault=archive;ref=evidence(engine)
    request=Deliver(action='deliver',grant_id=req.vault_grant_id,project_id='project-a',task_id='task-a',version_ids=[ref['version_id']])
    first=engine.store.deliver(request);folder=Path(first['path']);moved=folder.with_name('moved');folder.rename(moved)
    grant=engine.store.authorize(Authorize(action='authorize_destination',approved_root=str(vault),project_id='project-a'))
    second=engine.store.deliver(request.model_copy(update={'grant_id':grant['grant_id']}))
    assert second['delivery_id']==first['delivery_id'] and second['path']==str(moved)
    # 只有本测试创建的合成目录，在临时根中删去；缺失回执不重建。
    import shutil
    shutil.rmtree(moved)
    assert engine.store.deliver(request)['status']=='conflict'
    assert engine.store.deliver(request)['status']=='conflict'
    assert not folder.exists()


def test_incremental_stage_and_final_reports_keep_both(archive):
    engine,req,local,_=archive;evidence(engine)
    first=engine.submit(SubmitReport(action='submit_report',task_id='task-a',project_id='project-a',title='第一轮',report_id='round-1',report_role='stage',content='阶段结论'))
    assert first['summary']=='summary_pending'
    before=Path(first['local']['path']).read_bytes()
    final=engine.submit(SubmitReport(action='submit_report',task_id='task-a',project_id='project-a',title='总报告',report_id='final',local_report_path=str(local/'final.md'),content='最终综合结论'))
    assert final['summary']=='delivered'
    assert Path(first['local']['path']).read_bytes()==before
    assert Path(final['local']['path']).read_bytes()==Path(final['vault']['path']).read_bytes()


def test_display_rename_keeps_identity_and_physical_layout(archive):
    engine,req,_,_=archive;evidence(engine);first=engine.run('task-a')
    renamed=engine.bind(req.model_copy(update={'project_name':'新显示名称','task_title':'任务新标题'}))
    assert renamed['project_id']==first['project_id']
    assert engine.run('task-a')['rounds'][0]['path']==first['rounds'][0]['path']


def test_storage_reservations_and_quarantine_count(tmp_path,monkeypatch):
    from runtime.research_storage import reserve_bytes
    state=tmp_path/'state';cache=state/'media_cache';cache.mkdir(parents=True)
    quarantine=tmp_path/'quarantine';quarantine.mkdir();(quarantine/'held.bin').write_bytes(b'x'*500)
    monkeypatch.setenv('KR_STATE_DIR',str(state));monkeypatch.setenv('KR_MEDIA_CACHE_DIR',str(cache))
    monkeypatch.setenv('KR_DATA_ROOT',str(tmp_path));monkeypatch.setenv('KR_MEDIA_CAPACITY_BYTES','1000')
    with reserve_bytes(cache,400,category='media'):
        with pytest.raises(ValueError,match='quarantine_counted'):
            with reserve_bytes(cache,200,category='media'):pass
    with reserve_bytes(cache,400,category='media'):pass


def test_more_than_64mib_fulltext_streams_into_volumes(archive):
    engine,*_=archive
    body='中文 mixed 😀. '*48000 # 约0.8MiB；100+来源总和超过旧64MiB门槛。
    refs=[]
    for i in range(105):refs.append(engine.assets.put('https://example.org/large/'+str(i),body,title='长文')['version_id'])
    engine.assets.reference_many(refs,'task-a',project_id='project-a',task_id='task-a')
    result=engine.run('task-a');folder=Path(result['rounds'][0]['path'])
    assert result['rounds'][0]['count']==105
    restored=reconstruct(folder)
    assert len(restored)==105
    assert sum(len(raw) for raw in restored.values())>64*1024*1024
    assert all(raw==body.encode() for raw in restored.values())
    assert all(p.stat().st_size<=2*1024*1024 for p in folder.glob('网页 - 资料汇编*.md'))


def test_ten_thousand_versions_frozen_membership(archive):
    engine,*_=archive
    seed=evidence(engine,'用于隔离规模验收的短资料。😀')
    with engine.assets.connection() as c:
        original=dict(c.execute('SELECT * FROM assets WHERE version_id=?',(seed['version_id'],)).fetchone())
        # 复用正文对象，构造合法不同来源/版本；这是规模fixture，不冒充真实采集。
        metadata=json.loads(original['metadata']);loc_sha=hashlib.sha256(json.dumps(metadata['locators'],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        for i in range(10000):
            url='https://example.org/scale/'+str(i);sid=hashlib.sha256(url.encode()).hexdigest()[:24]
            version=hashlib.sha256(json.dumps([sid,original['kind'],original['raw_sha256'],metadata.get('extractor','unknown'),loc_sha],ensure_ascii=False).encode()).hexdigest()
            c.execute('INSERT INTO sources VALUES(?,0)',(sid,))
            values=dict(original,version_id=version,source_id=sid,source_url=url)
            c.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?,?)',tuple(values.values()))
            c.execute('INSERT INTO asset_scopes VALUES(?,?,?)',(version,'project-a','task-a'))
            c.execute('INSERT INTO asset_refs VALUES(?,?)',(version,'task-a'))
    result=engine.run('task-a');folder=Path(result['rounds'][0]['path'])
    assert result['rounds'][0]['count']==10001
    assert len(reconstruct(folder))==10001


@pytest.mark.parametrize('point',['round_publish','local_report'])
def test_real_child_exit_releases_locks_and_resumes_same_intent(archive,point):
    import subprocess,sys
    engine,*_=archive;evidence(engine)
    child='''
import os,sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'src'))
from runtime.knowledge_delivery import DeliveryStore
from runtime.research_archive import ResearchArchive,SubmitReport
engine=ResearchArchive(DeliveryStore())
point=POINT
if point=='round_publish':
    original=engine._round_save
    def save(key,payload):
        if payload.get('status')=='local_committed':os._exit(77)
        return original(key,payload)
    engine._round_save=save
    engine.run('task-a')
else:
    original=engine._report_endpoint
    def endpoint(context,key,*args):
        if key=='vault':os._exit(77)
        return original(context,key,*args)
    engine._report_endpoint=endpoint
    engine.submit(SubmitReport(action='submit_report',task_id='task-a',project_id='project-a',title='最终报告',content='# 结论\\n保留原文和缺口。\\n'))
'''.replace('POINT',repr(point))
    result=subprocess.run([sys.executable,'-X','utf8','-B','-c',child],capture_output=True,text=True,encoding='utf-8',timeout=40)
    assert result.returncode==77,(result.stdout,result.stderr)
    recovered=engine.run('task-a',retry=True)
    assert recovered['rounds'][0]['status']=='local_committed'
    if point=='local_report':assert recovered['summary']=='delivered'


@pytest.mark.skipif(os.name!='nt',reason='Windows真实共享锁合同')
def test_actual_windows_file_lock_retry_preserves_fixed_version(archive):
    import ctypes
    from ctypes import wintypes
    engine,req,local,_=archive;evidence(engine)
    path=Path(req.local_report_path);path.parent.mkdir(parents=True);path.write_bytes('# 结论\n保留原文和缺口。\n'.encode('utf-8'))
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    kernel.CreateFileW.restype=wintypes.HANDLE;kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.CreateFileW(str(path),0x80000000,0,None,3,0,None)
    assert handle!=wintypes.HANDLE(-1).value
    try:
        failed=submit(engine)
        assert failed['error_code']=='PermissionError'
        version=failed['report']['version_id']
    finally:kernel.CloseHandle(handle)
    result=engine.run('task-a',retry=True)
    assert result['summary']=='delivered' and result['report']['version_id']==version


def test_conflict_explicit_new_target_uses_same_fixed_report(archive):
    from runtime.research_archive import RebindReportOutput
    engine,req,local,vault=archive;evidence(engine);initial=submit(engine)
    old=Path(initial['vault']['path']);old.write_text('USER EDIT',encoding='utf-8')
    conflict=engine.run('task-a',retry=True);assert conflict['error_code']=='destination_modified_preserved'
    new=old.with_name('explicit-new-report.md')
    result=engine.rebind_report_output(RebindReportOutput(action='rebind_report_output',task_id='task-a',project_id='project-a',vault_report_path=str(new)))
    assert result['summary']=='delivered' and result['report']['version_id']==initial['report']['version_id']
    assert old.read_text(encoding='utf-8')=='USER EDIT'
    assert new.read_bytes()==Path(result['local']['path']).read_bytes()


def test_mcp_schema_dispatch_new_archive_actions(archive):
    import asyncio
    from mcp.types import CallToolRequest,CallToolRequestParams
    import server
    engine,req,*_=archive;evidence(engine)
    handler=server.mcp._mcp_server.request_handlers[CallToolRequest]
    def call(request):
        result=asyncio.run(handler(CallToolRequest(params=CallToolRequestParams(name='manage_research_artifacts',arguments={'request':request})))).root
        assert not result.isError,result.content
        return json.loads(result.content[0].text)
    fixed=call(dict(action='submit_report',task_id='task-a',project_id='project-a',title='协议验收',content='从真实SDK分发器进入同一归档内核'))
    assert fixed['summary']=='delivered'
    status=call(dict(action='archive_status',task_id='task-a'))
    assert status['report']['version_id']==fixed['report']['version_id']


def test_restore_rejects_broken_reference_even_if_manifest_rehashed(archive,tmp_path):
    engine,*_=archive;evidence(engine);submit(engine)
    backups=tmp_path/'backup';backups.mkdir()
    grant=engine.store.authorize(Authorize(action='authorize_destination',approved_root=str(backups),project_id='project-a'))
    receipt=snapshot(engine.store,SnapshotResearch(action='snapshot_research',backup_grant_id=grant['grant_id'],project_id='project-a',backup_name='before'))
    root=Path(receipt['path']);db=root/'evidence_assets/index.sqlite3'
    with sqlite3.connect(db) as c:c.execute("INSERT INTO asset_refs VALUES('missing-version','task-a')")
    manifest=json.loads((root/'snapshot.json').read_text(encoding='utf-8'))
    manifest['files']['evidence_assets/index.sqlite3']=hashlib.sha256(db.read_bytes()).hexdigest()
    (root/'snapshot.json').write_text(json.dumps(manifest),encoding='utf-8')
    with pytest.raises(ValueError,match='reference_missing'):
        restore(RestoreResearch(action='restore_research',approved_backup_root=str(backups),snapshot_path=str(root),approved_restore_root=str(tmp_path),restore_path=str(tmp_path/'new-state')))
    assert not (tmp_path/'new-state').exists()


def test_maintenance_archive_io_does_not_block_task_control(archive,monkeypatch):
    import threading,time
    from runtime.task_service import TaskService,_ARCHIVE_FUTURES
    from runtime.tasks import TaskStore
    engine,*_=archive;evidence(engine)
    entered=threading.Event();release=threading.Event()
    def slow(self,**kwargs):
        entered.set();release.wait(10);return 0
    monkeypatch.setattr(ResearchArchive,'maintain',slow)
    store=TaskStore(str(engine.root.parent/'knowledgeradar-tasks.sqlite3'))
    store.upsert_task('controllable','fixture','isolated')
    service=TaskService(store)
    try:
        start=time.monotonic();service.maintain_once()
        assert time.monotonic()-start<2
        assert entered.wait(2)
        assert service.cancel('controllable')['status']=='cancelled'
    finally:
        release.set()
        future=_ARCHIVE_FUTURES.get(str(engine.root.parent.resolve()))
        if future:future.result(timeout=10)


def test_report_scope_is_not_late_evidence_or_empty_round(archive):
    engine,*_=archive;evidence(engine);first=submit(engine)
    assert first['new_evidence_after_report'] is False
    assert len(first['rounds'])==1
    again=engine.run('task-a',retry=True)
    assert len(again['rounds'])==1 and again['rounds'][0]['count']==1
    evidence(engine,'真正迟到的正文','https://example.org/after-report')
    latest=engine.run('task-a',retry=True)
    assert latest['new_evidence_after_report'] is True and len(latest['rounds'])==2


def assert_readable_tree(root):
    import re
    for path in root.rglob('*'):
        assert not re.search(r'(?i)[0-9a-f]{10,}|\d{10,}|round-|final-report|index|manifest|sources\.json|texts-',path.name),path
        if path.is_file():assert path.suffix=='.md',path


def bind_another(engine, local, vault, project, task, project_name='同名项目',task_title='完整测试'):
    local_grant=engine.store.authorize(Authorize(action='authorize_destination',approved_root=str(local),project_id=project))
    vault_grant=engine.store.authorize(Authorize(action='authorize_destination',approved_root=str(vault),project_id=project))
    ledger.open_task(objective='验证自动命名',budget='fast',considered=[],task_id=task,project_id=project,project_name=project_name)
    request=BindContext(action='bind_context',task_id=task,project_id=project,project_name=project_name,task_title=task_title,
        local_grant_id=local_grant['grant_id'],local_report_path=str(local/(task+'.md')),vault_grant_id=vault_grant['grant_id'])
    return request,engine.bind(request)


def test_future_automatic_archive_tree_is_readable_and_machine_mapping_internal(archive):
    from urllib.parse import unquote
    import re
    engine,request,local,vault=archive
    evidence(engine)
    # 后台维护驱动，不手改任何输出名称。
    engine.maintain()
    result=submit(engine)
    parent=vault/'同名项目'/TASK_RELATIVE
    assert Path(result['vault']['path'])==parent/'总报告.md'
    assert {str(p.relative_to(vault)) for p in vault.rglob('*.md')}=={
        str(Path('同名项目')/TASK_RELATIVE/'总报告.md'),
        str(Path('同名项目')/TASK_RELATIVE/'资料导航.md'),
        str(Path('同名项目')/TASK_RELATIVE/'网页 - 原始资料.md')}
    assert_readable_tree(vault)
    folder=Path(result['rounds'][0]['path']);directory=folder/'资料导航.md'
    for link in re.findall(r'\]\(([^)]+)\)',directory.read_text(encoding='utf-8')):
        assert (directory.parent/unquote(link)).is_file()
    assert len(reconstruct(folder))==1
    with engine.store.connection() as c:
        assert c.execute('SELECT COUNT(*) FROM archive_parts').fetchone()[0]==1
        assert c.execute('SELECT COUNT(*) FROM archive_display_names').fetchone()[0]>=4


def test_same_project_reuses_directory_and_new_project_collision_requires_explicit_binding(archive):
    engine,_,local,vault=archive
    # 两个项目同名；同一项目两个任务也同名。
    with pytest.raises(ValueError,match='project_directory_exists_bind_context'):
        bind_another(engine,local,vault,'project-b','task-b')
    bind_another(engine,local,vault,'project-a','task-c')
    assert (vault/'同名项目'/TASK_RELATIVE).is_dir()
    assert (vault/'同名项目'/'2026年'/(TASK_STAMP+'（2）')).is_dir()
    assert not (vault/'同名项目（2）').exists()
    assert_readable_tree(vault)


def test_existing_user_folder_is_not_adopted_or_overwritten(archive):
    engine,_,local,vault=archive
    user=vault/'私人项目';user.mkdir();(user/'自己的笔记.md').write_text('用户原件',encoding='utf-8')
    with pytest.raises(ValueError,match='project_directory_exists_bind_context'):
        bind_another(engine,local,vault,'project-b','task-b',project_name='私人项目')
    assert not (vault/'私人项目（2）').exists()
    assert (user/'自己的笔记.md').read_text(encoding='utf-8')=='用户原件'
    assert list(user.iterdir())==[user/'自己的笔记.md']


def test_renamed_project_and_task_accept_late_content_without_recreating_old_paths(archive):
    engine,_,_,vault=archive;evidence(engine);first=submit(engine)
    original=vault/'同名项目';original.rename(vault/'我的研究项目')
    task=vault/'我的研究项目'/TASK_RELATIVE;task.rename(task.with_name('已经改名的调研'))
    evidence(engine,'后续资料','https://example.org/late-after-folder-rename')
    later=engine.run('task-a',retry=True)
    assert later['rounds'][0]['status']=='local_committed',later
    assert Path(later['rounds'][0]['path'])==vault/'我的研究项目'/'2026年'/'已经改名的调研'
    assert not original.exists() and not task.exists()
    assert_readable_tree(vault)


def test_deleted_task_directory_is_not_recreated_for_late_material(archive):
    import shutil
    engine,_,_,vault=archive;evidence(engine);submit(engine)
    shutil.rmtree(vault/'同名项目'/TASK_RELATIVE)
    evidence(engine,'后续资料','https://example.org/late-after-delete')
    result=engine.run('task-a',retry=True)
    assert result['error_code']=='destination_missing_preserved_no_recreation'
    assert not (vault/'同名项目'/TASK_RELATIVE).exists()


def test_multiple_reports_and_supplements_keep_short_human_names(archive):
    engine,_,local,vault=archive;evidence(engine);submit(engine)
    evidence(engine,'补充一','https://example.org/second');engine.run('task-a')
    evidence(engine,'补充二','https://example.org/third');engine.run('task-a')
    result=engine.submit(SubmitReport(action='submit_report',task_id='task-a',project_id='project-a',title='最终更新',
        report_id='e'*64,local_report_path=str(local/'更新报告.md'),content='补齐反证后的结论。'))
    assert Path(result['vault']['path']).name=='总报告（2）.md'
    task=vault/'同名项目'/TASK_RELATIVE
    assert {p.name for p in task.iterdir()}=={'总报告.md','总报告（2）.md','资料导航.md','资料导航（2）.md','资料导航（3）.md','网页 - 原始资料.md','网页 - 原始资料（2）.md','网页 - 原始资料（3）.md'}
    assert_readable_tree(vault)


def test_opaque_ids_and_reserved_titles_do_not_leak_into_display(archive):
    engine,_,local,vault=archive
    _,bound=bind_another(engine,local,vault,'f'*64,'a'*64,project_name='b'*64,task_title='CON')
    assert (vault/'未命名调研'/'2026年'/calendar_layout({'created_at':'2026-11-02T00:00:00+00:00'},'CON')['directory_title']).is_dir()
    ref=engine.assets.put('https://example.org/opaque','保留原文',title='index')
    engine.assets.reference_many([ref['version_id']],'a'*64,project_id='f'*64,task_id='a'*64)
    result=engine.run('a'*64)
    assert result['rounds'][0]['status']=='local_committed'
    assert_readable_tree(vault)


def test_restart_and_reauthorization_preserve_allocated_names(archive):
    engine,request,_,vault=archive;evidence(engine);first=submit(engine)
    before={p.relative_to(vault) for p in vault.rglob('*')}
    grant=engine.store.authorize(Authorize(action='authorize_destination',approved_root=str(vault),project_id='project-a'))
    fresh=ResearchArchive(DeliveryStore())
    fresh.bind(request.model_copy(update={'vault_grant_id':grant['grant_id']}))
    retried=fresh.run('task-a',retry=True)
    assert retried['vault']['path']==first['vault']['path']
    assert {p.relative_to(vault) for p in vault.rglob('*')}==before


def test_registered_report_uses_same_readable_archive_mechanism(archive):
    from runtime.knowledge_delivery import execute,RegisterReport
    engine,request,local,vault=archive;evidence(engine)
    source=local/'写好的总结.md';source.write_text('登记现有总结',encoding='utf-8')
    execute(RegisterReport(action='register_report',input_path=str(source),approved_input_root=str(local),input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        project_id='project-a',task_id='task-a',report_id='final',title='收尾'),engine.store)
    assert Path(engine.status('task-a')['vault']['path']).name=='总报告.md'
    assert_readable_tree(vault)


@pytest.mark.parametrize('title',['index.md','final-report.md','round-3-补充资料','调研 index 汇总'])
def test_selected_source_titles_cannot_reintroduce_machine_names(archive,title):
    engine,request,_,vault=archive
    ref=engine.assets.put('https://example.org/machine-title','不改原文',title=title)
    engine.assets.reference_many([ref['version_id']],'task-a',project_id='project-a',task_id='task-a')
    result=engine.store.deliver(Deliver(action='deliver',grant_id=request.vault_grant_id,project_id='project-a',task_id='task-a',version_ids=[ref['version_id']]))
    assert result['status']=='local_committed'
    assert (Path(result['path'])/result['entries'][0]['file']).read_text(encoding='utf-8').endswith('不改原文\n\n')
    assert engine.assets.fetch(ref['version_id'])['asset']['title']==title
    assert_readable_tree(vault)


@pytest.mark.parametrize('url,metadata,label',[
    ('https://www.bilibili.com/video/BVtest',{},'B站'),
    ('https://www.xiaohongshu.com/explore/note',{},'小红书'),
    ('https://arxiv.org/abs/123',{},'学术'),
    ('https://publisher.example/paper',{'provenance':{'provider_id':'openalex'}},'学术'),
    ('https://cdn.example/item',{'source_platform':'maimai'},'脉脉'),
    ('https://www.zhipin.com/job_detail/abc',{},'BOSS直聘'),
    ('https://bilibili.com.evil.example/item',{},'网页'),
])
def test_source_naming_is_independent_of_media_kind_and_collection_tool(archive,url,metadata,label):
    engine,*_=archive
    ref=engine.assets.put(url,'真实格式的固定转写文本',kind='transcript',title='原始标题',metadata=metadata|{'display_title_zh':'语音识别实测'})
    engine.assets.reference_many([ref['version_id']],'task-a',project_id='project-a',task_id='task-a')
    result=engine.run('task-a');folder=Path(result['rounds'][0]['path'])
    note=folder/(label+' - 语音识别实测.md')
    assert note.is_file() and '资料内容：转写或字幕' in note.read_text(encoding='utf-8')
    assert not [p for p in folder.iterdir() if p.is_dir()]
    assert reconstruct(folder)[ref['version_id']].decode()=='真实格式的固定转写文本'


def test_internal_assets_filtered_and_comments_tables_rendered_without_research_llm(archive):
    engine,*_=archive
    rows=[('transcript_map',json.dumps({'segments':[{'start':1}]})),('media_manifest','[{"url":"https://example.org/video.mp4"}]'),
        ('comments',json.dumps([{'id':'c2','parent_id':'c1','author':'读者甲','content':'保留反面意见'}],ensure_ascii=False)),
        ('tables',json.dumps({'columns':['模型','速度'],'rows':[['A','2倍']]},ensure_ascii=False))]
    refs=[engine.assets.put('https://www.bilibili.com/video/BVtest',text,kind=kind,title='识别引擎对比') for kind,text in rows]
    engine.assets.reference_many([r['version_id'] for r in refs],'task-a',project_id='project-a',task_id='task-a')
    result=engine.run('task-a');folder=Path(result['rounds'][0]['path'])
    all_text='\n'.join(p.read_text(encoding='utf-8') for p in folder.glob('*.md'))
    assert '保留反面意见' in all_text and '回复对象：c1' in all_text and '| A | 2倍 |' in all_text
    assert 'video.mp4' not in all_text and '"segments"' not in all_text
    assert '仅供机器定位或媒体引用：2 项' in all_text
    with engine.store.connection() as c:
        parts=[json.loads(r[0]) for r in c.execute('SELECT payload FROM archive_parts')]
    assert len(parts)==2
    for part in parts:
        ref=engine.assets.fetch(part['text_version_id'])
        assert ref['asset']['provenance']['parent_version_id']==part['version_id']
    for ref,(_,text) in zip(refs,rows):assert engine.assets.fetch(ref['version_id'])['text']==text


def test_selection_reuses_existing_files_and_automatic_archive_reuses_selection(archive):
    engine,request,*_=archive;first=evidence(engine);engine.run('task-a')
    second=evidence(engine,'后续正文','https://example.org/second-selection')
    selected=engine.store.deliver(Deliver(action='deliver',grant_id=request.vault_grant_id,project_id='project-a',task_id='task-a',version_ids=[first['version_id'],second['version_id']]))
    assert selected['status']=='local_committed',selected
    folder=Path(selected['path']);before={p.name:p.read_bytes() for p in folder.glob('网页 - *.md')}
    assert len(before)==2
    assert engine.run('task-a')['error_code']==''
    assert before=={p.name:p.read_bytes() for p in folder.glob('网页 - *.md')}
    assert not [p for p in folder.iterdir() if p.is_dir()]
    assert (folder/'选取资料.md').is_file()


def test_display_metadata_preserves_original_capture_time_and_fixed_task_date(archive,monkeypatch):
    engine,request,*_=archive
    ref=engine.assets.put('https://example.org/old','old body',title='English original',metadata={'read_at':'2020-01-02T03:04:05Z','language':'en'})
    engine.assets.reference_many([ref['version_id']],'task-a',project_id='project-a',task_id='task-a')
    result=engine.run('task-a');folder=Path(result['rounds'][0]['path'])
    assert folder.name==TASK_STAMP
    text=next(folder.glob('网页 - *.md')).read_text(encoding='utf-8')
    assert '采集时间：2020-01-02T03:04:05Z' in text and '语言：en' in text and 'English original' in next(folder.glob('网页 - *.md')).name
    monkeypatch.setattr(ledger,'_now',lambda:'2026-11-03T00:00:00+00:00')
    engine.bind(request);evidence(engine,'新一天资料','https://example.org/new-day')
    assert Path(engine.run('task-a')['rounds'][0]['path'])==folder


def test_flat_file_delete_after_partial_publish_never_recreated(archive,monkeypatch):
    engine,*_=archive;evidence(engine)
    original=engine._round_save
    def crash(key,record):
        original(key,record)
        if record.get('status')=='publishing' and record.get('file_records'):
            raise RuntimeError('partial publication exit')
    monkeypatch.setattr(engine,'_round_save',crash)
    with pytest.raises(RuntimeError,match='partial publication'):engine.run('task-a')
    record=engine._round_get(engine.status('task-a')['rounds'][0]['round_id'])
    path=Path(next(iter(record['file_records'].values()))['path']);path.unlink()
    monkeypatch.setattr(engine,'_round_save',original)
    result=engine.run('task-a',retry=True)
    assert result['error_code']=='destination_missing_preserved_no_recreation' and not path.exists()


def test_selected_final_report_reuses_fixed_dual_delivery(archive):
    engine,request,*_=archive;evidence(engine);finished=submit(engine)
    report=finished['report']['version_id'];folder=Path(finished['vault']['path']).parent
    delivered=engine.store.deliver(Deliver(action='deliver',grant_id=request.vault_grant_id,project_id='project-a',task_id='task-a',version_ids=[report]))
    assert delivered['status']=='local_committed',delivered
    assert len(list(folder.glob('总报告*.md')))==1
    assert delivered['entries'][0]['file']=='总报告.md'


def test_renamed_source_is_linked_by_latest_navigation_and_not_recreated(archive):
    from urllib.parse import unquote
    engine,request,*_=archive;ref=evidence(engine);first=engine.run('task-a');folder=Path(first['rounds'][0]['path'])
    old=next(folder.glob('网页 - *.md'));new=folder/'用户起的资料名.md';old.rename(new)
    evidence(engine,'追加的实际文字','https://example.org/after-source-rename');later=engine.run('task-a',retry=True)
    assert not later['error_code'],later
    assert new.is_file() and not old.exists()
    nav=folder/later['rounds'][0]['navigation']
    for link in __import__('re').findall(r'\]\(([^)]+)\)',nav.read_text(encoding='utf-8')):
        assert (folder/unquote(link)).is_file(),link


def test_academic_document_asset_archived_as_academic_without_body_translation(archive):
    engine,*_=archive
    ref=engine.assets.put('https://publisher.example/oa/paper','English paper body',kind='document_text',title='Original paper title',metadata={'source_platform':'academic','title_zh':'长音频识别论文'})
    engine.assets.reference_many([ref['version_id']],'task-a',project_id='project-a',task_id='task-a')
    result=engine.run('task-a');folder=Path(result['rounds'][0]['path'])
    text=(folder/'学术 - 长音频识别论文.md').read_text(encoding='utf-8')
    assert 'English paper body' in text and engine.assets.fetch(ref['version_id'])['asset']['title']=='Original paper title'


@pytest.mark.parametrize('url,metadata,label',[
    ('https://www.youtube.com/watch?v=123',{},'YouTube'),
    ('https://www.zhipin.com/job_detail/123',{},'BOSS直聘'),
    ('https://www.liepin.com/job/123',{},'猎聘'),
    ('https://maimai.cn/article/123',{},'脉脉'),
    ('https://v2ex.com/t/123',{},'V2EX'),
    ('https://x.com/user/status/123',{},'推特'),
    ('https://new.example/item',{'source_presentation':{'group':'platform','label':'未来平台','site':'未来站点'}},'未来平台'),
    ('https://provider.example/item',{'source_presentation':{'group':'academic','label':'不应拆分的学术站点'}},'学术'),
    ('https://blog.example/item',{'source_presentation':{'group':'web','label':'不应拆分的网页工具'}},'网页'),
])
def test_extensible_platform_categories_keep_web_and_academic_coarse(archive,url,metadata,label):
    from runtime.research_archive import source_label
    assert source_label({'source_url':url,'kind':'content','metadata':json.dumps(metadata)})==label


def test_two_field_name_and_compact_summary_without_duplicate_display_title(archive):
    from runtime.research_archive import display_subject,asset_header
    row={'source_url':'https://docs.python.org/zh-cn/3/library/pathlib.html','kind':'content',
        'title':'pathlib --- 面向对象的文件系统路径 — Python 3.15.2 文档',
        'captured_at':1,'metadata':json.dumps({'extractor':'trafilatura','original_captured_at':'2026-01-01'})}
    assert display_subject(row)=='面向对象的文件系统路径'
    text=asset_header(row)
    assert text.startswith('> [!info] 资料摘要') and '来源网站：docs.python.org' in text
    assert '处理方式：正文抽取（trafilatura）' in text
    assert not text.startswith('# ') and 'Python 3.15.2' not in text
    assert asset_header(row,include_title=True).startswith('## 面向对象的文件系统路径')
    row.update(source_url='https://trafilatura.readthedocs.io/en/latest/corefunctions.html',title='Core functions — Trafilatura 2.3.1 documentation')
    assert display_subject(row)=='Core functions'
    row.update(source_url='https://example.org/research',title='能源成本 — 长期趋势')
    assert display_subject(row)=='能源成本：长期趋势'


def test_calendar_layout_uses_same_windows_safe_title_for_obsidian_and_physical_path():
    from runtime.research_archive import calendar_layout
    layout = calendar_layout({'created_at': '2026-11-07T01:44:00-04:00'}, '连续探索 / 首轮:回归')
    assert layout['directory_year'] == '2026年'
    assert re.match(r'^11-07 \d{2}-\d{2} ', layout['directory_title'])
    assert '/' not in layout['directory_title'] and ':' not in layout['directory_title']


@pytest.mark.parametrize('platform,source,description',[
    ('YouTube','youtube_transcript_api','读取平台字幕'),
    ('B站','bilibili_subtitle','读取平台字幕'),
    ('YouTube','sherpa-onnx-int8','音频语音转写'),
    ('B站','','具体方式未记录'),
])
def test_real_ingress_preserves_subtitle_vs_asr_method(archive,platform,source,description):
    from kr_core.evidence_store import EvidenceStore
    engine,*_=archive
    repo=EvidenceStore(str(engine.assets.root.parent/'records.jsonl'));repo.assets=engine.assets
    data={'title':'音频样本','transcript':'保留的内容','transcript_metadata':{'source':source,'language':'en'}}
    repo.append_detail(platform=platform,url='https://youtube.com/watch?v=123',response=data)
    version=next(x['version_id'] for x in data['evidence_assets'] if x['kind']=='transcript')
    engine.assets.reference_many([version],'task-a',project_id='project-a',task_id='task-a')
    result=engine.run('task-a');folder=Path(result['rounds'][0]['path'])
    note=next(folder.glob('YouTube - *.md')).read_text(encoding='utf-8')
    assert description in note and '语言：en' in note


def test_fresh_state_must_bind_existing_project_then_reuses_it_for_later_tasks(archive,tmp_path):
    engine,_,_,vault=archive
    fresh=ResearchArchive(DeliveryStore(tmp_path/'fresh-state',engine.assets))
    grant=fresh.store.authorize(Authorize(action='authorize_destination',approved_root=str(vault),project_id='project-a'))
    context={'task_id':'later-a','project_id':'project-a','project_name':'同名项目','task_title':'新研究',
        'directory_title':'2026-11-02 新研究','display_layout':'source-flat-v2','vault_grant_id':grant['grant_id']}
    with pytest.raises(ValueError,match='project_directory_exists_bind_context'):
        fresh._task_directory(context)
    context['vault_project_dir']='同名项目'
    assert fresh._task_directory(context)==vault/'同名项目'/'2026-11-02 新研究'
    context.update(task_id='later-b',directory_title='2026-11-02 又一项研究',vault_project_dir='')
    assert fresh._task_directory(context)==vault/'同名项目'/'2026-11-02 又一项研究'
    assert [x.name for x in vault.iterdir()]==['同名项目']
    context.update(vault_project_dir='另一个目录')
    with pytest.raises(ValueError,match='archive_project_target_changed'):
        fresh._task_directory(context)
    assert not (vault/'另一个目录').exists()


def test_explicit_project_binding_survives_human_rename_without_recreation(archive):
    engine,req,local,vault=archive
    bind_another(engine,local,vault,'project-a','second-task',task_title='显式目录')
    # 原有隐式映射也能由Codex明确确认同一目录。
    explicit=req.model_copy(update={'vault_project_dir':'同名项目'})
    engine.bind(explicit)
    (vault/'同名项目').rename(vault/'人改后的项目名称')
    evidence(engine);result=engine.run('task-a')
    assert not result['error_code']
    engine.bind(explicit)
    bind_another(engine,local,vault,'project-a','third-task',task_title='追加任务')
    assert not (vault/'同名项目').exists()
    assert (vault/'人改后的项目名称'/'2026年'/calendar_layout({'created_at':'2026-11-02T00:00:00+00:00'},'追加任务')['directory_title']).is_dir()


def test_raw_html_archived_only_after_extraction_and_bad_body_is_explicit_gap(archive):
    engine,*_=archive
    body='包含实际研究结论的文章正文。'*30
    valid=f'<html><body><nav>页面菜单</nav><article><h1>研究结果</h1><p>{body}</p><pre><code>if ready:\n    preserve()</code></pre></article><footer>网站页脚</footer></body></html>'
    refs=[engine.assets.put('https://example.org/article',valid,title='研究结果',metadata={'content_format':'html'}),
          engine.assets.put('https://example.org/empty','<html><script src="app.js"></script></html>',title='空壳',metadata={'content_format':'html'})]
    engine.assets.reference_many([r['version_id'] for r in refs],'task-a',project_id='project-a',task_id='task-a')
    result=engine.run('task-a');folder=Path(result['rounds'][0]['path'])
    text=(folder/'网页 - 研究结果.md').read_text(encoding='utf-8')
    assert body in text and 'if ready:\n    preserve()' in text
    assert '页面菜单' not in text and '网站页脚' not in text and '<html>' not in text
    assert result['rounds'][0]['skipped_assets']=={'正文转换失败：html_body_unavailable':1}
    assert not list(folder.glob('*空壳*'))
    assert engine.assets.fetch(refs[0]['version_id'])['text']==valid


def test_exit_after_commit_reclaims_only_unchanged_owned_staging(archive,monkeypatch):
    engine,*_=archive;evidence(engine);original=engine._round_save
    def crash(key,record):
        original(key,record)
        if record.get('status')=='local_committed':raise RuntimeError('exit before cleanup')
    monkeypatch.setattr(engine,'_round_save',crash)
    with pytest.raises(RuntimeError):engine.run('task-a')
    monkeypatch.setattr(engine,'_round_save',original)
    key=engine.status('task-a')['rounds'][0]['round_id'];record=engine._round_get(key)
    stage=Path(record['staging_path'])
    # 手动写入的未知内容必须保留；只删除其测试自建文件后才能正常回收。
    stage.mkdir(exist_ok=True);user=stage/'用户临时笔记.md';user.write_text('用户心血',encoding='utf-8')
    assert engine.run('task-a',retry=True)['error_code']=='staging_modified_preserved'
    assert user.read_text(encoding='utf-8')=='用户心血'
    user.unlink()
    assert engine.run('task-a',retry=True)['error_code']==''
    assert not stage.exists()

# 安装布局回归：state、logs与任务库覆盖项可以位于不同目录。
def test_maintenance_uses_delivery_state_not_task_database_parent(archive, tmp_path, monkeypatch):
    from runtime.task_service import TaskService, _ARCHIVE_FUTURES
    from runtime.tasks import TaskStore
    engine, *_ = archive
    ref = evidence(engine)
    logs = tmp_path / 'separate-logs'
    monkeypatch.setenv('KR_LOG_DIR', str(logs))
    monkeypatch.delenv('KR_TASK_DB_PATH', raising=False)
    service = TaskService(TaskStore(str(logs / 'knowledgeradar-tasks.sqlite3')))
    service.maintain_once()
    future = _ARCHIVE_FUTURES.get(str(engine.root.parent.resolve()))
    assert future is not None, 'maintenance looked for deliveries beside the task database'
    future.result(timeout=10)
    result = engine.status('task-a')
    assert result['rounds']
    assert ref['version_id'] in reconstruct(Path(result['rounds'][0]['path']))
    assert not (logs / 'knowledge_delivery').exists()


@pytest.mark.parametrize('layout', ['split', 'explicit', 'legacy'])
def test_snapshot_keeps_split_root_tasks_and_rebinds_restored_results(archive, tmp_path, monkeypatch, layout):
    from runtime.tasks import TaskStore
    engine, *_ = archive
    evidence(engine)
    submit(engine)
    logs = tmp_path / 'logs'
    monkeypatch.setenv('KR_LOG_DIR', str(logs))
    task_db = {'split': logs/'knowledgeradar-tasks.sqlite3', 'explicit': tmp_path/'custom-jobs/jobs.sqlite3',
               'legacy': engine.root.parent/'knowledgeradar-tasks.sqlite3'}[layout]
    if layout != 'split':
        monkeypatch.setenv('KR_TASK_DB_PATH', str(task_db))
    else:
        monkeypatch.delenv('KR_TASK_DB_PATH', raising=False)
    jobs = TaskStore(str(task_db))
    jobs.upsert_task('durable-preserved', 'fixture', 'local')
    result = task_db.parent / 'task_artifacts' / 'durable-preserved' / 'result-one.json'
    result.parent.mkdir(parents=True)
    result.write_text('{"status":"completed","text":"saved"}', encoding='utf-8')
    jobs.mark_completed('durable-preserved', result_path=str(result))
    backups = tmp_path / 'backups'; backups.mkdir()
    grant = engine.store.authorize(Authorize(action='authorize_destination', approved_root=str(backups), project_id='project-a'))
    receipt = snapshot(engine.store, SnapshotResearch(action='snapshot_research', backup_grant_id=grant['grant_id'], project_id='project-a', backup_name='split'))
    saved = Path(receipt['path'])
    assert (saved / 'knowledgeradar-tasks.sqlite3').is_file(), 'configured task DB silently omitted'
    relative = Path('task_artifacts/durable-preserved/result-one.json')
    assert (saved / relative).read_bytes() == result.read_bytes()
    manifest = json.loads((saved / 'snapshot.json').read_text(encoding='utf-8'))
    assert Path(manifest['source_layout']['task_database']) == task_db.resolve()
    if layout == 'legacy':
        manifest.pop('source_layout')
        (saved/'snapshot.json').write_text(json.dumps(manifest), encoding='utf-8')
    restored = tmp_path / 'restored'
    restore(RestoreResearch(action='restore_research', approved_backup_root=str(backups), snapshot_path=str(saved), approved_restore_root=str(tmp_path), restore_path=str(restored)))
    row = TaskStore(str(restored / 'knowledgeradar-tasks.sqlite3')).get_task('durable-preserved')
    assert Path(row['result_path']) == restored / relative
    assert Path(row['result_path']).read_bytes() == result.read_bytes()
    restoring = json.loads((restored / 'RESTORE-RECEIPT.json').read_text(encoding='utf-8'))
    assert restoring['runtime_paths']['KR_TASK_DB_PATH'] == str(restored / 'knowledgeradar-tasks.sqlite3')
    assert restoring['external_authorization'] == 'disabled'


def test_snapshot_rejects_missing_task_result_instead_of_success(archive, tmp_path, monkeypatch):
    from runtime.tasks import TaskStore
    engine, *_ = archive
    task_db = engine.root.parent / 'knowledgeradar-tasks.sqlite3'
    monkeypatch.setenv('KR_TASK_DB_PATH', str(task_db))
    jobs = TaskStore(str(task_db))
    jobs.upsert_task('durable-missing', 'fixture', 'local')
    jobs.mark_completed('durable-missing', result_path=str(task_db.parent / 'task_artifacts/durable-missing/result-gone.json'))
    backups = tmp_path / 'backups'; backups.mkdir()
    grant = engine.store.authorize(Authorize(action='authorize_destination', approved_root=str(backups), project_id='project-a'))
    with pytest.raises(ValueError, match='snapshot_task_result_missing'):
        snapshot(engine.store, SnapshotResearch(action='snapshot_research', backup_grant_id=grant['grant_id'], project_id='project-a', backup_name='incomplete'))
    assert not (backups / 'incomplete').exists()


@pytest.mark.parametrize('role,expected', [('stage','summary_pending'),('final','delivered')])
def test_closeout_reports_actual_fixed_summary_without_reverse_lock(archive, monkeypatch, role, expected):
    engine, *_ = archive
    evidence(engine)
    delivered = submit(engine, report_role=role)
    assert delivered['summary'] == expected
    # close_task不能持有ledger锁后再读取/锁住归档，避免与run反序。
    original = engine.__class__._context
    original_guard = ledger._ledger_guard
    from contextlib import contextmanager
    held = []
    @contextmanager
    def guarded(task):
        with original_guard(task):
            held.append(task)
            try: yield
            finally: held.pop()
    def context(self, task):
        assert not held
        return original(self, task)
    monkeypatch.setattr(ledger, '_ledger_guard', guarded)
    monkeypatch.setattr(engine.__class__, '_context', context)
    result = ledger.close_task(task_id='task-a', ecology_outcomes=[], stop_rationale='test', key_claims=[])
    facts = result['closeout']['completion_facts']
    assert facts['summary'] == expected
    assert facts['delivery'] == 'delivered'
    assert facts['summary_version_id'] == delivered['report']['version_id']


def test_closeout_does_not_call_historical_delivery_current_after_conflict(archive):
    engine, *_ = archive
    evidence(engine)
    delivered = submit(engine)
    Path(delivered['local']['path']).write_text('USER CHANGED', encoding='utf-8')
    conflict = engine.run('task-a')
    assert conflict['error_code']
    result = ledger.close_task(task_id='task-a', ecology_outcomes=[], stop_rationale='conflict observed', key_claims=[])
    facts = result['closeout']['completion_facts']
    assert facts['summary'] == 'registered'
    assert facts['delivery'] == 'conflict'
    assert facts['delivery_error_code'] == conflict['error_code']
    assert facts['summary_version_id'] == delivered['report']['version_id']
