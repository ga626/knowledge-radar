"""BOSS 借用浏览器合同；真实站点验证另行记证据。"""
import pytest
from conftest import patch_native
from runtime import native_site
from collectors.platform import boss as boss
from runtime import chrome_manager as chrome

@pytest.fixture(autouse=True)
def isolated(monkeypatch,tmp_path):
    monkeypatch.setenv("KR_STATE_DIR",str(tmp_path/"state"))
    monkeypatch.setenv("KR_TASK_DB_PATH",str(tmp_path/"tasks.sqlite3"))


def test_city_resolved_and_unknown_stops_without_browser(monkeypatch):
    assert boss.search_url("AI产品运营","杭州").endswith("city=101210100")
    patch_native(monkeypatch,lambda **_:pytest.fail("must not navigate"))
    assert boss.search("Agent","火星",1)["failure_type"]=="city_mapping_missing"


def test_query_mismatch_is_not_success(monkeypatch):
    patch_native(monkeypatch,lambda **_: {"ok":True,"data":{"query":"old","items":[{"title":"old"}]}})
    result=boss.search("new")
    assert result["failure_type"]=="search_query_not_applied"
    assert not result["manual_action_required"]


def test_empty_page_distinct_from_empty_results(monkeypatch):
    patch_native(monkeypatch,lambda **_: {"ok":True,"data":{"query":"q","empty":True,"items":[]}})
    assert boss.search("q")["status"]=="empty"
    patch_native(monkeypatch,lambda **_: {"ok":True,"data":{"query":"next","items":[]}})
    # 使用独立词并移除频率等待，以观察解析合同。
    from runtime import recruitment_governance as gate
    monkeypatch.setattr(gate,"check_search_gate",lambda *a,**kw:{"allowed":True})
    result=boss.search("next")
    assert result["failure_type"]=="page_structure_or_not_ready"
    assert not result["manual_action_required"]


def test_unknown_probe_does_not_invent_login(monkeypatch):
    patch_native(monkeypatch,lambda **_: {"ok":True,"data":{}})
    assert boss.probe_auth()["auth_state"]=="unknown"
    assert not boss.probe_auth()["manual_action_required"]


def test_rate_gate_never_opens_login(monkeypatch):
    from runtime import recruitment_governance as gate
    monkeypatch.setattr(gate,"check_search_gate",lambda *a,**kw:{"allowed":False,"reason":"interval"})
    patch_native(monkeypatch,lambda **_:pytest.fail("rate gate must not open a tab"))
    assert boss.search("q")["failure_type"]=="rate_limited"


def test_login_request_is_single_and_explicit_completion_resumes(monkeypatch):
    calls=[]
    patch_native(monkeypatch,lambda **kw: calls.append(kw) or {"ok":True,"data":{"login":True}})
    assert chrome.request_user_login("boss",reason="login_required")["status"]=="waiting_for_user"
    assert chrome.request_browser_interaction("boss")["status"]=="waiting_for_user"
    assert len(calls)==1 and calls[0]["url"]==boss.LOGIN_URL
    assert not boss.admission()["allowed"]
    patch_native(monkeypatch,lambda **kw: {"ok":True,"data":{"authenticated_ui":True}})
    assert chrome.complete_browser_interaction("boss")["status"]=="ok"
    assert boss.admission()["allowed"]


def test_challenge_cools_down_without_navigation_or_manual_unlock(monkeypatch):
    calls=[]
    monkeypatch.setattr(boss.time,'time',lambda:1000)
    patch_native(monkeypatch,lambda **kw:calls.append(kw) or {"ok":False,"code":"SECURITY_BLOCK"})
    assert boss.search("q")["failure_type"]=="anti_bot_verification"
    assert boss.search("q2")["failure_type"]=="platform_cooldown"
    assert len(calls)==1
    assert boss.interaction_status()=="blocked"
    assert not boss.admission()['manual_action_required']
    monkeypatch.setattr(boss.time,'time',lambda:1301)
    assert boss.admission()["allowed"]
    assert len(calls)==1  # 状态读取不导航，不恢复旧请求。


def test_cancel_only_releases_owned_tab(monkeypatch):
    calls=[]
    patch_native(monkeypatch,lambda **kw:calls.append(kw) or {"ok":True,"data":{"browser_closed":False}})
    assert chrome.cancel_browser_interaction("boss")["status"]=="cancelled"
    assert calls[0]["operation"]=="release_login"
    assert not chrome._ensure_chrome_debugging("boss")
    assert chrome._managed_chrome_profile_dir("boss")==""
    assert "boss" not in chrome.managed_browser_platforms()


def test_unknown_failure_cannot_raise_login_window(monkeypatch):
    monkeypatch.setattr(boss,"request_login",lambda:pytest.fail("no login evidence"))
    assert chrome.request_user_login("boss",reason="unknown")["reason_code"]=="NO_MANUAL_ACTION_EVIDENCE"


def test_detail_identity_and_minimum_body(monkeypatch):
    url="https://www.zhipin.com/job_detail/one.html"
    patch_native(monkeypatch,lambda **_: {"ok":True,"data":{"url":url,"content":"岗位职责。"*40}})
    assert boss.detail(url)["status"]=="ok"
    assert boss.detail("http://127.0.0.1/job_detail/one")["failure_type"]=="invalid_boss_detail_url"
    patch_native(monkeypatch,lambda **_: {"ok":True,"data":{"url":url.replace("one","two"),"content":"x"*200}})
    assert boss.detail(url)["failure_type"]=="page_identity_changed"


def test_borrowed_identity_does_not_grant_process_close():
    assert boss.runtime_identity()["ownership"]=="borrowed"
    assert chrome._managed_process_close_allowed("boss",42) is False
    from runtime.browser_channel import browser_resource_contract,may_close_process
    contract=browser_resource_contract(ownership="owned",resource_id="fixture",generation="old",driver="cdp")
    assert not may_close_process(contract,"new")
    assert may_close_process(contract,"old")


def test_unknown_timeout_stops_without_inventing_captcha(monkeypatch):
    calls=[]
    patch_native(monkeypatch,lambda **kw:calls.append(kw) or {'ok':False,'code':'native_command_timeout','request_executed':True})
    result=boss.search('first')
    assert result['failure_type']=='request_outcome_unknown'
    assert boss.search('second')['failure_type']=='request_outcome_unknown'
    assert len(calls)==1 and not boss.admission()['allowed']
    boss.cancel_login()
    assert not boss.admission()['allowed']


@pytest.mark.parametrize('prior_deadline',[990,1010])
def test_explicit_redisplay_renews_grace_without_reloading_login(monkeypatch,prior_deadline):
    calls=[]
    monkeypatch.setattr(boss.time,'time',lambda:1000)
    native_site.write_state(boss.state_path(),{'expires_at':prior_deadline})
    patch_native(monkeypatch,lambda **kw:calls.append(kw) or {'ok':True,'data':{'login':True}})
    assert boss.request_login(activate=True)['expires_at']==1300
    assert calls[0]['operation']=='attention' and calls[0]['url']==''
    assert boss._site().invoke('probe')['failure_type']=='login_required'
    assert calls[-1]['operation']=='probe'
    assert boss._site().state()['expires_at']==1300
