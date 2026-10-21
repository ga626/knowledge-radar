"""人工登录不能被超时、其他客户端或调用进程退出自动结束。"""
from types import SimpleNamespace
import pytest
from runtime import chrome_manager as chrome, browser_sessions

@pytest.fixture
def recovery(monkeypatch, tmp_path):
    store = browser_sessions.BrowserSessionStore(tmp_path/'sessions.json', tmp_path/'events.jsonl')
    monkeypatch.setattr(browser_sessions, '_STORE', store)
    for name in ('_MANAGED_CHROME_PROCS','_MANAGED_CHROME_PROFILE_DIRS','_MANAGED_CHROME_PIDS',
                 '_CHROME_IDLE_TIMERS','_CHROME_KEEP_ALIVE','_CHROME_ACTIVE_OPERATIONS',
                 '_CHROME_MANUAL_INTERACTION_LEASES','_CHROME_MANUAL_INTERACTION_EXPIRY_TIMERS'):
        monkeypatch.setattr(chrome, name, {})
    monkeypatch.setattr(chrome, '_MANAGED_CHROME_OWNED_PIDS', {42})
    profile = str(tmp_path/'boss')
    closed = []
    proc = SimpleNamespace(poll=lambda: 0 if closed else None, wait=lambda **kw: None,
                           terminate=lambda: closed.append('terminate'),kill=lambda: closed.append('kill'))
    monkeypatch.setattr(chrome, '_find_chrome_with_debug_port', lambda _: None if closed else 42)
    monkeypatch.setattr(chrome, '_chrome_user_data_dir_for_pid', lambda _: profile)
    monkeypatch.setattr(chrome, '_managed_chrome_profile_dir', lambda *a, **kw: profile)
    monkeypatch.setattr(chrome, '_profile_metadata_for_platform', lambda *a, **kw: {'profile_id':'boss-one'})
    monkeypatch.setattr(chrome, '_close_chrome_via_cdp', lambda _: closed.append('cdp'))
    monkeypatch.setattr(chrome, '_minimize_chrome_windows', lambda _: closed.append('minimize'))
    chrome._MANAGED_CHROME_PROCS['boss'] = proc
    chrome._MANAGED_CHROME_PIDS['boss'] = 42
    store.upsert(platform='boss',profile_id='boss-one',profile_dir=profile,debug_port='12737',pid=42,
                 state='USER_INTERACTING',desired_visibility='attention',metadata={'account_recovery_pending':True})
    return store,profile,closed

def test_request_process_exit_keeps_unverified_scan_window(recovery):
    _,_,closed = recovery
    chrome._cleanup_managed_chrome()
    assert closed == [], '进程结束不能等同于用户扫码完成'

@pytest.mark.parametrize('platform',['boss','zhihu','liepin','maimai','cnki','xhs:account-c'])
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
    chrome._cleanup_managed_chrome_platform('boss',expected_profile_dir=profile)
    assert closed == [], '另一客户端没有内存保活标志也必须保护扫码窗口'

def test_unverified_complete_cannot_hide_or_reclaim_window(recovery,monkeypatch):
    _,_,closed=recovery
    monkeypatch.setattr(chrome,'probe_browser_auth',lambda *a,**kw:{'status':'needs_interaction','manual_action_required':True})
    result=chrome.complete_browser_interaction('boss')
    assert result['status']=='not_verified'
    assert closed == []

def test_background_request_during_scan_cannot_hide_window(recovery):
    _,_,closed=recovery
    assert chrome.background_chrome('boss')['status']=='waiting_for_user'
    assert closed == []

def test_explicit_user_cancel_can_close_without_claiming_login(recovery):
    store,profile,closed=recovery
    result=chrome.cancel_browser_interaction('boss',expected_profile_dir=profile)
    assert result['status']=='cancelled'
    assert closed == ['cdp']
    assert store.latest_for_platform('boss').state=='CLOSED'
    assert not store.latest_for_platform('boss').last_probe_result

def test_verified_completion_returns_window_to_normal_cleanup(recovery,monkeypatch):
    store,profile,closed=recovery
    monkeypatch.setattr(chrome,'_schedule_chrome_idle_cleanup',lambda *a,**kw:None)
    from runtime import account_observations
    monkeypatch.setattr(account_observations,'observe_account',lambda *a,**kw:{'accepted':True})
    result=chrome.complete_browser_interaction('boss',probe_result={'status':'ok','profile_id':'boss-one'},profile_id='boss-one',profile_dir=profile)
    assert result['status']=='ok'
    chrome._cleanup_managed_chrome()
    assert 'cdp' in closed

def test_old_expiry_callback_cannot_cancel_a_waiting_user(recovery):
    _,profile,closed=recovery
    result=chrome.cancel_browser_interaction('boss',reason='manual_interaction_expired',expected_profile_dir=profile)
    assert result['status']=='waiting_for_user'
    assert closed==[]

def test_stale_startup_cleanup_cannot_kill_scan_window(recovery,monkeypatch):
    _,_,closed=recovery
    monkeypatch.setattr(chrome,'silent_subprocess_run',lambda *a,**kw:closed.append('taskkill'))
    chrome._cleanup_stale_chrome('boss')
    assert closed==[]

def test_other_clients_idle_timer_keeps_scan_window(recovery,monkeypatch):
    _,profile,closed=recovery
    callbacks=[]
    class Timer:
        def __init__(self,delay,callback): callbacks.append(callback)
        def start(self): pass
        def cancel(self): pass
    monkeypatch.setattr(chrome.threading,'Timer',Timer)
    chrome._schedule_chrome_idle_cleanup('boss',profile_dir=profile)
    callbacks[0]()
    assert closed==[]

def test_state_reader_failure_is_not_permission_to_close(recovery,monkeypatch):
    _,_,closed=recovery
    monkeypatch.setattr(chrome,'browser_sessions_summary',lambda **kw:(_ for _ in ()).throw(OSError('locked')))
    chrome._cleanup_managed_chrome()
    assert closed==[]

