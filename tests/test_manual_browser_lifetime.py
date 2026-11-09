from runtime.state_file import write_state
from conftest import patch_native
"""人工登录不能被超时、其他客户端或调用进程退出自动结束。"""
from pathlib import Path
from types import SimpleNamespace
import time
import pytest
from runtime import chrome_manager as chrome, browser_sessions

@pytest.fixture
def recovery(monkeypatch, tmp_path, request):
    platform = getattr(request, 'param', 'cnki')
    store = browser_sessions.BrowserSessionStore(tmp_path/'sessions.json', tmp_path/'events.jsonl')
    monkeypatch.setattr(browser_sessions, '_STORE', store)
    for name in ('_MANAGED_CHROME_PROCS','_MANAGED_CHROME_PROFILE_DIRS','_MANAGED_CHROME_PIDS',
                 '_CHROME_IDLE_TIMERS','_CHROME_KEEP_ALIVE','_CHROME_ACTIVE_OPERATIONS',
                 '_CHROME_MANUAL_INTERACTION_LEASES','_CHROME_MANUAL_INTERACTION_EXPIRY_TIMERS'):
        monkeypatch.setattr(chrome, name, {})
    monkeypatch.setattr(chrome, '_MANAGED_CHROME_OWNED_PIDS', {42})
    monkeypatch.setattr(chrome, '_managed_process_close_allowed', lambda *a, **kw: True)
    profile = str(tmp_path/platform)
    closed = []
    monkeypatch.setattr(browser_sessions, 'observe_process', lambda pid: {
        'status': 'missing' if closed else 'alive',
        'identity': '' if closed else '42:scan-window',
    })
    proc = SimpleNamespace(poll=lambda: 0 if closed else None, wait=lambda **kw: None,
                           terminate=lambda: closed.append('terminate'),kill=lambda: closed.append('kill'))
    monkeypatch.setattr(chrome, '_find_chrome_with_debug_port', lambda *a, **kw: None if closed else 42)
    monkeypatch.setattr(chrome, '_chrome_user_data_dir_for_pid', lambda _: profile)
    monkeypatch.setattr(chrome, '_managed_chrome_profile_dir', lambda *a, **kw: profile)
    monkeypatch.setattr(chrome, '_profile_metadata_for_platform', lambda *a, **kw: {'profile_id':platform+'-one'})
    monkeypatch.setattr(chrome, '_close_chrome_via_cdp', lambda _: closed.append('cdp'))
    monkeypatch.setattr(chrome, '_minimize_chrome_windows', lambda _: closed.append('minimize'))
    chrome._MANAGED_CHROME_PROCS[platform] = proc
    chrome._MANAGED_CHROME_PIDS[platform] = 42
    store.upsert(platform=platform,profile_id=platform+'-one',profile_dir=profile,debug_port=chrome._chrome_debug_port(platform),pid=42,
                 state='USER_INTERACTING',desired_visibility='attention',metadata={'account_recovery_pending':True})
    return store,profile,closed

def test_request_process_exit_keeps_unverified_scan_window(recovery):
    _,_,closed = recovery
    chrome._cleanup_managed_chrome()
    assert closed == [], '进程结束不能等同于用户扫码完成'

@pytest.mark.parametrize('platform',['cnki','cnki','liepin','cnki','cnki','xhs:account-c'])
def test_every_platform_manual_deadline_only_reminds(recovery,monkeypatch,platform):
    _,profile,_ = recovery
    callbacks=[]
    class Timer:
        def __init__(self, delay, callback): callbacks.append(callback)
        def start(self): pass
        def cancel(self): pass
    monkeypatch.setattr(chrome.threading,'Timer',Timer)
    chrome._schedule_manual_interaction_expiry(platform,deadline_at=1,profile_dir=profile)
    assert callbacks == [], '等待扫码超时不能触发自动关闭'

def test_other_client_cleanup_reads_persisted_manual_state(recovery):
    _,profile,closed=recovery
    assert not chrome._CHROME_KEEP_ALIVE
    chrome._cleanup_managed_chrome_platform('cnki',expected_profile_dir=profile)
    assert closed == [], '另一客户端没有内存保活标志也必须保护扫码窗口'

def test_unverified_complete_cannot_hide_or_reclaim_window(recovery,monkeypatch):
    _,_,closed=recovery
    monkeypatch.setattr(chrome,'probe_browser_auth',lambda *a,**kw:{'status':'needs_interaction','manual_action_required':True})
    result=chrome.complete_browser_interaction('cnki')
    assert result['status']=='not_verified'
    assert closed == []

def test_background_request_during_scan_cannot_hide_window(recovery):
    _,_,closed=recovery
    assert chrome.background_chrome('cnki')['status']=='waiting_for_user'
    assert closed == []

def test_explicit_user_cancel_can_close_without_claiming_login(recovery):
    store,profile,closed=recovery
    result=chrome.cancel_browser_interaction('cnki',expected_profile_dir=profile)
    assert result['status']=='cancelled'
    assert closed == ['cdp']
    assert store.latest_for_platform('cnki').state=='CLOSED'
    assert not store.latest_for_platform('cnki').last_probe_result

def test_verified_completion_returns_window_to_normal_cleanup(recovery,monkeypatch):
    store,profile,closed=recovery
    monkeypatch.setattr(chrome,'_schedule_chrome_idle_cleanup',lambda *a,**kw:None)
    from runtime import account_observations
    monkeypatch.setattr(account_observations,'observe_account',lambda *a,**kw:{'accepted':True})
    result=chrome.complete_browser_interaction('cnki',probe_result={'status':'ok','profile_id':'cnki-one'},profile_id='cnki-one',profile_dir=profile)
    assert result['status']=='ok'
    chrome._cleanup_managed_chrome()
    assert 'cdp' in closed






def test_unknown_failure_does_not_open_manual_browser(recovery, monkeypatch):
    monkeypatch.setattr(chrome, '_ensure_chrome_debugging', lambda *a, **kw: pytest.fail('不应打开浏览器'))
    assert chrome.request_user_login('cnki', reason='unknown', source='search_recruitment')['reason_code'] == 'NO_MANUAL_ACTION_EVIDENCE'

def test_old_expiry_callback_cannot_cancel_a_waiting_user(recovery):
    _,profile,closed=recovery
    result=chrome.cancel_browser_interaction('cnki',reason='manual_interaction_expired',expected_profile_dir=profile)
    assert result['status']=='waiting_for_user'
    assert closed==[]

def test_stale_startup_cleanup_cannot_kill_scan_window(recovery,monkeypatch):
    _,_,closed=recovery
    monkeypatch.setattr(chrome,'silent_subprocess_run',lambda *a,**kw:closed.append('taskkill'))
    chrome._cleanup_stale_chrome('cnki')
    assert closed==[]

def test_other_clients_idle_timer_keeps_scan_window(recovery,monkeypatch):
    _,profile,closed=recovery
    callbacks=[]
    class Timer:
        def __init__(self,delay,callback): callbacks.append(callback)
        def start(self): pass
        def cancel(self): pass
    monkeypatch.setattr(chrome.threading,'Timer',Timer)
    chrome._schedule_chrome_idle_cleanup('cnki',profile_dir=profile)
    callbacks[0]()
    assert closed==[]

def test_state_reader_failure_is_not_permission_to_close(recovery,monkeypatch):
    _,_,closed=recovery
    monkeypatch.setattr(chrome,'browser_sessions_summary',lambda **kw:(_ for _ in ()).throw(OSError('locked')))
    chrome._cleanup_managed_chrome()
    assert closed==[]



@pytest.mark.parametrize('authenticated',[True,False])
def test_borrowed_login_verified_only_by_explicit_confirmation(monkeypatch,tmp_path,authenticated):
    from collectors.platform import maimai
    from runtime.state_file import write_state
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path))
    write_state(maimai._site().state_path(),{'expires_at':9999999999})
    calls=[]
    patch_native(monkeypatch,lambda **kw:calls.append(kw) or {'ok':True,'data':{'authenticated_api':authenticated}})
    assert not maimai.admission()['allowed'] and calls==[]
    result=chrome.complete_browser_interaction('maimai')
    assert result['status']==('ok' if authenticated else 'not_verified')
    assert calls[0]['operation']=='auth_verify' and calls[0]['url']==maimai.COMMUNITY_HOME


def test_late_auth_probe_cannot_clear_new_challenge(monkeypatch,tmp_path):
    from collectors.platform import maimai
    from runtime import native_site
    from runtime.state_file import write_state
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path))
    monkeypatch.setattr(native_site.time,'time',lambda:100)
    def invoke(**kw):
        write_state(maimai._site().state_path(),{'risk':True,'reason':'new_challenge','at':100})
        return {'ok':True,'data':{'authenticated_api':True,'authenticated_ui':True}}
    patch_native(monkeypatch,invoke)
    assert maimai.complete_login()['status']=='not_verified'
    assert not maimai.admission()['allowed']


def test_legacy_owned_sessions_not_restored_for_borrowed_platform(monkeypatch):
    monkeypatch.setattr(chrome,'browser_sessions_summary',lambda **kw:{'sessions':[{'platform':'maimai','state':'READY_SILENT'}]})
    monkeypatch.setattr(chrome,'_find_chrome_with_debug_port',lambda _:pytest.fail('must not inspect owned port'))
    assert chrome.restore_chrome_idle_cleanups()=={'restored':0,'overdue':0}


def test_borrowed_login_deadline_does_not_poll_or_resume_task(monkeypatch,tmp_path):
    from collectors.platform import boss as boss
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path))
    write_state(boss.state_path(),{'expires_at':time.time()+300})
    patch_native(monkeypatch,lambda **kw:pytest.fail('status polling must not touch the browser'))
    assert boss.interaction_status()=='waiting'
    assert not boss.admission()['allowed']
    assert boss.request_login()['status']=='waiting_for_user'


def test_borrowed_manual_window_hold_ends_only_after_verified_completion(monkeypatch,tmp_path):
    from collectors.platform import boss as boss
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path))
    write_state(boss.state_path(),{'expires_at':1})
    calls=[]
    patch_native(monkeypatch,lambda **kw:calls.append(kw) or {'ok':True,'data':{'authenticated_api':True,'authenticated_ui':True}})
    assert boss.complete_login()['status']=='ok'
    assert calls[0]['operation']=='auth_verify'
    assert calls[-1]['operation']=='release_login'
    assert boss.interaction_status()=='ready' and boss.admission()['allowed']


def test_new_related_request_checks_login_once_and_preserves_unknown_operation(monkeypatch,tmp_path):
    from collectors.platform import zhihu
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path))
    site=zhihu._site()
    write_state(site.state_path(),{'expires_at':1})
    calls=[]
    patch_native(monkeypatch,lambda **kw:calls.append(kw['operation']) or {'ok':True,'data':{'authenticated_api':True,'authenticated_ui':True}})
    assert site.invoke('search',url=site.home)['ok']
    assert calls == ['auth_verify','release_login','search']
    write_state(site.state_path(),{'expires_at':1,'unknown_outcome':True,'operation_id':'original-op'})
    assert site.complete_login()['status']=='ok'
    assert site.state()['unknown_outcome'] and site.state()['operation_id']=='original-op'


def test_login_verification_is_durable_before_release_and_failed_cleanup_can_resume(monkeypatch,tmp_path):
    from collectors.platform import zhihu
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path))
    site=zhihu._site()
    write_state(site.state_path(),{'expires_at':1,'interaction_id':'scan-one'})
    releases=[]
    def invoke(**kw):
        if kw['operation']=='release_login':
            state=site.state()
            assert state.get('verified_at') and state.get('auth_generation')
            assert state['cleanup_intent']['status']=='pending'
            assert state['expires_at']==1, '失败清理不能提前移除人工等待记录'
            releases.append(state['cleanup_intent']['id'])
            return {'ok':len(releases)>1,'failure_type':'temporary_release_failure'}
        return {'ok':True,'data':{'authenticated_api':True,'authenticated_ui':True}}
    patch_native(monkeypatch,invoke)
    assert site.complete_login()['status']=='not_verified'
    assert site.state()['cleanup_intent']['status']=='pending'
    assert not site.admission()['allowed']
    assert site.complete_login()['status']=='ok'
    assert len(releases)==2
    assert site.state()['cleanup_intent']['status']=='completed'
    assert site.admission()['allowed']


def test_login_cleanup_late_success_cannot_overwrite_cancellation(monkeypatch,tmp_path):
    from collectors.platform import zhihu
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path))
    site=zhihu._site()
    write_state(site.state_path(),{'expires_at':1,'interaction_id':'scan-one'})
    def invoke(**kw):
        if kw['operation']=='release_login':
            write_state(site.state_path(),{**site.state(),'cancelled':True,'interaction_id':'newer'})
        return {'ok':True,'data':{'authenticated_api':True,'authenticated_ui':True}}
    patch_native(monkeypatch,invoke)
    assert site.complete_login()['status']=='not_verified'
    assert site.state()['cancelled'] and site.state()['interaction_id']=='newer'


def test_unknown_operation_resolution_is_explicit_and_does_not_replay(monkeypatch,tmp_path):
    from collectors.platform import zhihu
    from types import SimpleNamespace
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path))
    site=zhihu._site()
    write_state(site.state_path(),{'unknown_outcome':True,'operation_id':'original-op','operation':'search'})
    reads=[]
    monkeypatch.setattr(type(site),'bridge',lambda _:SimpleNamespace(receipt=lambda op:reads.append(op) or
        {'status':'recorded','result':{'ok':True,'data':{'items':[]},'request_id':op}}))
    assert site.reconcile_outcome()['status']=='recorded'
    assert site.state()['unknown_outcome'], '读取回执不能自动解锁或重放'
    assert site.reconcile_outcome(operation_id='wrong',resolution='accept_result')['status']=='conflict'
    assert site.reconcile_outcome(operation_id='original-op',resolution='accept_result')['status']=='resolved'
    assert site.admission()['allowed'] and reads==['original-op','original-op']
    assert site.state()['operation_resolutions'][-1]['resolution']=='accept_result'


def test_pending_cannot_be_abandoned_and_newer_latch_cannot_be_cleared(monkeypatch,tmp_path):
    from collectors.platform import zhihu
    from types import SimpleNamespace
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path))
    site=zhihu._site()
    original={'unknown_outcome':True,'operation_id':'original-op'}
    write_state(site.state_path(),original)
    def receipt(op):
        write_state(site.state_path(),{**original,'operation_id':'new-op'})
        return {'status':'recorded','result':{'ok':True}}
    monkeypatch.setattr(type(site),'bridge',lambda _:SimpleNamespace(receipt=receipt))
    assert site.reconcile_outcome(operation_id='original-op',resolution='accept_result')['status']=='conflict'
    assert site.state()['operation_id']=='new-op'
    monkeypatch.setattr(type(site),'bridge',lambda _:SimpleNamespace(receipt=lambda _: {'status':'pending'}))
    assert site.reconcile_outcome(operation_id='new-op',resolution='abandon')['status']=='pending'
    assert site.state()['unknown_outcome']
    monkeypatch.setattr(type(site),'bridge',lambda _:SimpleNamespace(receipt=lambda _: {'status':'unknown'}))
    assert site.reconcile_outcome(operation_id='new-op',resolution='abandon')['status']=='resolved'
    assert site.admission()['allowed']
