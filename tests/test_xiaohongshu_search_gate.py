"""Single-account primary route; old cooldown does not select a legacy backend."""
import pytest
import server
from runtime import xhs_opencli as oc

def test_current_search_has_no_account_rotation_or_historic_health_gate(monkeypatch):
    monkeypatch.setattr(server,"_record_search_evidence",lambda *_:None)
    monkeypatch.setattr(server,"_xhs_budget_state",lambda:{})
    monkeypatch.setattr(server,"_cached_search",lambda platform,keyword,limit,call,**kwargs:{"items":[],"total":0})
    result=server.search_xiaohongshu("q",1,"image")
    assert result["metadata"]["xhs_route_receipt"]["transport"]=="opencli"
    assert "account_pool" not in result["metadata"]["xhs_route_receipt"]

def test_error_decoration_never_opens_another_browser(monkeypatch):
    monkeypatch.setattr(server,"request_browser_interaction",lambda *_a,**_k:pytest.fail("duplicate browser"))
    response={"error":{"type":"login_required","manual_action_required":True,"retryable":False}}
    assert server._attach_xiaohongshu_error_context(response)["error"]["manual_action_required"]

def test_missing_browser_configuration_does_not_prompt_login(monkeypatch,tmp_path):
    monkeypatch.setenv("KR_STATE_DIR",str(tmp_path))
    monkeypatch.delenv("KR_XHS_OPENCLI_CONTEXT_ID",raising=False)
    monkeypatch.setattr(oc,"request_login",lambda:pytest.fail("login is not a configuration fix"))
    result=oc.search("q",1,"all")
    assert not result["request_executed"] and not result["manual_action_required"]
