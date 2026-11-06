import pytest
from runtime import xhs_opencli as oc

@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    monkeypatch.setenv("KR_XHS_OPENCLI_STATE_DIR", str(tmp_path/"state"))
    monkeypatch.setenv("KR_XHS_OPENCLI_CONTEXT_ID", "daily")

def test_pending_login_does_not_repeat_content_navigation(monkeypatch):
    oc._save(oc.state_dir()/"login.json",{"expires_at":oc.time.time()+300,"context":"daily"})
    monkeypatch.setattr(oc,"interaction_state",lambda:"waiting")
    monkeypatch.setattr(oc,"_run",lambda *_a,**_k:pytest.fail("must wait without search"))
    result=oc.invoke("search",keyword="q")
    assert result["error"]["error"]=="AUTH_REQUIRED" and not result["request_executed"]

def test_risk_latch_preserves_first_reason_and_time():
    oc.latch("SECURITY_BLOCK")
    first=oc._json(oc.state_dir()/"stop.json")
    oc._failure("anti_bot_verification",executed=False)
    assert oc._json(oc.state_dir()/"stop.json")==first

def test_unconfirmed_login_cannot_clear_risk(monkeypatch):
    oc.latch("SECURITY_BLOCK")
    monkeypatch.setattr(oc,"invoke",lambda *_a,**_k:{"ok":True,"data":{"authenticated_ui":False}})
    assert not oc.complete_login().get("authenticated")
    assert (oc.state_dir()/"stop.json").exists()

def test_cancelled_login_expires_without_closing_user_browser(monkeypatch):
    oc._save(oc.state_dir()/"login.json",{"expires_at":oc.time.time()+300,"context":"daily"})
    monkeypatch.setattr(oc,"invoke",lambda *_a,**_k:pytest.fail("no browser mutation"))
    assert oc.cancel_login()["browser_closed"] is False
    assert oc.interaction_state()=="expired"

def test_chrome_manager_xhs_entrypoints_delegate_without_cdp(monkeypatch):
    from runtime import chrome_manager as cm
    monkeypatch.setattr(cm,"_ensure_chrome_debugging",lambda *_a,**_k:pytest.fail("retired CDP"))
    monkeypatch.setattr(oc,"request_login",lambda:{"status":"waiting_for_user"})
    monkeypatch.setattr(oc,"complete_login",lambda:{"status":"completed"})
    monkeypatch.setattr(oc,"invoke",lambda *_a,**_k:{"ok":True})
    assert cm.request_browser_interaction("xhs","login")["status"]=="waiting_for_user"
    assert cm.complete_browser_interaction("xhs")["status"]=="completed"
    assert cm.probe_browser_auth("xhs")["ok"]
