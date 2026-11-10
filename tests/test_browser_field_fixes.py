import pytest
from runtime.browser_sessions import BrowserSessionStore
from runtime import browser_sessions


@pytest.fixture
def store(tmp_path):
    return BrowserSessionStore(tmp_path / 'sessions.json', tmp_path / 'events.jsonl')


def observe(store, session='kr:boss:work:a', **changes):
    result = changes.pop('result', {'ok': True, 'metadata': {}})
    return browser_sessions.observe_borrowed_call(result, platform='boss', context_id='daily', session=session,
                                 run_id=changes.pop('run_id', 'a'), operation=changes.pop('operation', 'search'),
                                 task_id='research-1', store=store, **changes)


def test_same_profile_different_work_pages_remain_independent(store):
    observe(store, result={'ok':True,'metadata':{'work_page_closed':True}})
    observe(store, session='kr:boss:work:b', run_id='b')
    rows = store.summary()['sessions']
    assert len(rows) == 2
    assert {r['state'] for r in rows} == {'CLOSED', 'QUIESCING'}


@pytest.mark.parametrize('result', [
    {'ok':True}, {'ok':False,'code':'opencli_caller_timeout'},
    {'ok':False,'metadata':{'cleanup_warnings':['work_page_cleanup_failed']}},
])
def test_missing_cleanup_receipt_never_means_closed(store, result):
    row = observe(store, result=result)['metadata']['browser_lifecycle']
    assert row['state'] == 'QUIESCING'
    assert row['cleanup'] == 'unconfirmed'


def test_manual_login_is_retained_and_unknown_risk_is_not_a_scan_prompt(store):
    row = observe(store, operation='login', result={'ok':True,'data':{'manual_login_pending':True},
        'metadata':{'presentation':{'visible':True,'focused':True}}})['metadata']['browser_lifecycle']
    assert row['state'] == 'USER_INTERACTING'
    risk = observe(store, session='risk', result={'ok':False,'code':'RATE_LIMITED',
        'metadata':{'retained_for_interaction':True}})['metadata']['browser_lifecycle']
    assert risk['state'] == 'QUIESCING'
    assert store.summary()['pending_human_action'] == 1


def test_receipt_preserves_actual_session_run_and_window_observations(store):
    row = observe(store, result={'ok':True,'metadata':{'work_session':'actual-work', 'run_id':'actual-run',
        'work_page_closed':True,'window_observation':{'hwnd':7,'pid':9,'window_id':3,
        'observed_visibility':'minimized','focused':False}}})['metadata']['browser_lifecycle']
    assert row['session'] == 'actual-work' and row['run_id'] == 'actual-run'
    assert row['window_observation']['hwnd'] == 7
    assert row['state'] == 'CLOSED' and row['task_id'] == 'research-1'


def test_telemetry_failure_is_visible_without_replaying_business(store, monkeypatch):
    monkeypatch.setattr(store, 'upsert', lambda **kw: (_ for _ in ()).throw(OSError('disk')))
    result = observe(store, result={'ok':True,'data':[1]})
    assert result['ok'] and result['data'] == [1]
    assert result['metadata']['diagnostic_warnings'] == ['browser_lifecycle_write_failed:OSError']


def test_window_receipt_observes_minimized_state_and_generation(monkeypatch):
    import runtime.browser_bootstrap as bootstrap
    import runtime.process_identity as process_identity
    marker = 'KR-work-'+'a'*32
    class Windows:
        def list(self): return [{'handle':7,'pid':9,'title':marker,'minimized':True}]
        def foreground(self): return 1
        def present(self, identity, foreground): pass
    monkeypatch.setattr(process_identity,'process_identity',lambda pid:'generation-1')
    identity = bootstrap.window_presentation({'action':'bind','marker':marker},windows=Windows())
    result = bootstrap.window_presentation({'action':'present','identity':identity,'title':marker,'foreground':False},windows=Windows())
    assert identity['process_identity'] == 'generation-1'
    assert result['observed_visibility'] == 'minimized'
    assert result['focused'] is False
    monkeypatch.setattr(process_identity,'process_identity',lambda pid:'generation-2')
    with pytest.raises(ValueError, match='identity_changed'):
        bootstrap.window_presentation({'action':'present','identity':identity,'title':marker,'foreground':False},windows=Windows())


def test_xhs_cleanup_failure_preserves_login_handoff(monkeypatch,tmp_path):
    import json
    import runtime.xhs_opencli as xhs
    from types import SimpleNamespace
    (tmp_path/'login.json').write_text(json.dumps({'context':'daily','expires_at':9999999999}),encoding='utf-8')
    monkeypatch.setattr(xhs,'state_dir',lambda:tmp_path)
    monkeypatch.setattr(xhs,'browser',lambda:SimpleNamespace(context_id='daily'))
    monkeypatch.setattr(xhs,'invoke',lambda *a,**k:{'ok':True,'data':{'authenticated_ui':True},
        'metadata':{'work_page_closed':False,'cleanup_warnings':['work_page_cleanup_failed']}})
    monkeypatch.setattr(xhs,'_observe_account',lambda *a,**k:None)
    result=xhs.complete_login()
    assert result['status']!='completed'
    assert (tmp_path/'login.json').is_file()
    assert result['failure_type']=='manual_window_cleanup_unconfirmed'


@pytest.mark.parametrize('ok',[True,False])
def test_xhs_runner_result_reaches_lifecycle_ledger(monkeypatch,tmp_path,ok):
    import hashlib,json
    from types import SimpleNamespace
    from runtime import xhs_opencli as xhs
    root=tmp_path/'opencli';adapter=root/'clis/xiaohongshu';adapter.mkdir(parents=True)
    (root/'package.json').write_text('{"version":"2.0.2"}',encoding='utf-8')
    digest=hashlib.sha256()
    for name in ('search','note','comments','download','risk-control','note-helpers'):
        relative='clis/xiaohongshu/'+name+'.js';(root/relative).write_bytes(b'fixture')
        digest.update(relative.encode());digest.update(b'fixture')
    settings={'KR_XHS_OPENCLI_ROOT':str(root),'KR_XHS_OPENCLI_CONTEXT_ID':'daily',
              'KR_XHS_OPENCLI_ADAPTER_SHA256':digest.hexdigest()}
    monkeypatch.setattr(xhs,'setting',lambda key:settings.get(key,''))
    monkeypatch.setattr(xhs,'_ensure_daemon',lambda *a:'')
    monkeypatch.setenv('KR_NODE_EXE','fixture-node')
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path/'state'))
    result={'ok':ok,'code':'fixture_failure','metadata':{'work_session':'actual-work','work_page_closed':True}}
    monkeypatch.setattr(xhs,'silent_subprocess_run',lambda *a,**k:SimpleNamespace(stdout=json.dumps(result)))
    observed=xhs._run('inspect')
    receipt=observed['metadata']['browser_lifecycle']
    assert receipt['state']=='CLOSED' and receipt['session']=='actual-work'
    rows=BrowserSessionStore().summary()['sessions']
    assert len(rows)==1 and rows[0]['metadata']['run_id']==receipt['run_id']


def test_common_runner_records_lifecycle_before_releasing_site_lease(monkeypatch,tmp_path):
    import json
    from contextlib import contextmanager
    from types import SimpleNamespace
    from runtime import opencli_bridge,process,browser_channel
    root=tmp_path/'opencli';root.mkdir();(root/'package.json').write_text('{"version":"2.0.2"}',encoding='utf-8')
    monkeypatch.setattr(opencli_bridge,'connection_config',lambda:{'root':str(root),'context_id':'daily','state_root':tmp_path})
    monkeypatch.setattr(opencli_bridge,'ensure_daemon',lambda *a,**k:'')
    monkeypatch.setenv('KR_NODE_EXE','fixture-node')
    held=[];receipts=[]
    @contextmanager
    def lease(*args):
        held.append(True)
        try:yield
        finally:held.pop()
    monkeypatch.setattr(browser_channel.BorrowedBrowser,'lease',lease)
    monkeypatch.setattr(process,'silent_subprocess_run',lambda *a,**k:SimpleNamespace(stdout=json.dumps({'ok':True,'metadata':{'work_page_closed':True}})))
    def observe(result,**kwargs):
        assert held,'A later operation must not overtake this receipt'
        receipts.append(kwargs);return result
    monkeypatch.setattr(browser_sessions,'observe_borrowed_call',observe)
    assert opencli_bridge.invoke_page(session='kr:boss',operation='probe')['ok']
    assert len(receipts)==1 and not held
