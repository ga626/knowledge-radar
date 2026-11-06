"""双来源、真实已观察卡片/响应关系及认证边界；不联网或读取个人浏览器。"""
import threading
from copy import deepcopy

import pytest

from collectors.platform import weread
from kr_core.search_cache import SearchCache
from use_cases.wechat_search import execute

URL = "https://mp.weixin.qq.com/s/research"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("KR_RUNTIME_ENV_PATH", str(tmp_path / "missing.env"))
    monkeypatch.setattr(weread, "browser_scope", lambda: "test-binding")


def invoke_with(data):
    def invoke(**kw):
        if kw["operation"] == "auth_verify":
            return {"ok": True, "data": {"authenticated_ui": True, "account_marker": "avatar://test"}}
        return {"ok": True, "data": deepcopy(data)}
    return invoke


def test_exact_card_identity_resolves_original_and_rejects_encyclopedia():
    data = {"items": [
        {"candidate_ref": "123&2", "title": "相同标题", "url": ""},
        {"candidate_ref": "456&2", "title": "相同标题", "url": ""},
        {"candidate_ref": "789&2", "title": "未解析原文", "url": ""}],
        "broker_articles": [{"candidate_ref": "123", "url": URL},
                            {"candidate_ref": "456", "url": "https://baike.sogou.com/example"}]}
    r = weread.search("研究", invoke=invoke_with(data))
    assert r["acquired_items"][0]["url"] == URL
    assert r["acquired_items"][1]["resolution_status"] == "article_url_unresolved"
    assert r["metadata"]["excluded_non_article_cards"] == 1
    assert r["metadata"]["unresolved_article_urls"] == 1
    assert "avatar://test" not in str(r)


def test_long_original_url_keeps_publisher_parameters_for_fetch():
    raw = "http://mp.weixin.qq.com/s?__biz=abc==&mid=1&idx=2&sn=3&chksm=original#rd"
    data = {"items":[{"candidate_ref":"123&2","url":""}],
            "broker_articles":[{"candidate_ref":"123","url":raw}]}
    row = weread.search("研究",invoke=invoke_with(data))["items"][0]
    assert row["url"] == raw.replace("http:","https:",1)
    assert "chksm" not in row["article_identity"]


@pytest.mark.parametrize("snapshot,code", [
    ({"items": [], "empty": True}, None),
    ({"items": [], "loading": True}, "search_loading_timeout"),
    ({"items": []}, "search_structure_unrecognized"),
    ({"items": [], "broker_error": {"type": "authentication_required", "code": -2010}}, "authentication_required"),
    ({"items": [], "risk": True}, "anti_bot_verification"),
    ({"items": [], "broker_error": {"type": "rate_limited", "status_code": 429}}, "rate_limited"),
])
def test_empty_loading_auth_and_risk_are_different(snapshot, code):
    result = weread.search("研究", invoke=invoke_with(snapshot))
    if code:
        assert result["error"]["type"] == code
    else:
        assert result["status"] == "empty" and not result.get("error")


def test_logged_out_does_not_search_or_begin_login():
    calls = []
    def invoke(**kw):
        calls.append(kw)
        return {"ok": True, "data": {"login": True}}
    assert weread.search("研究", invoke=invoke)["error"]["type"] == "authentication_required"
    assert [r["operation"] for r in calls] == ["auth_verify"]


def test_auto_sources_start_independently_and_keep_original_when_other_fails():
    both_started = threading.Barrier(2)
    def public(*a, **kw):
        both_started.wait(timeout=3)
        return {"items": [{"url": URL, "title": "研究"}], "provider": "public"}
    def browser(*a, **kw):
        both_started.wait(timeout=3)
        return weread.failure("authentication_required")
    r = execute("研究", limit=1, cache=SearchCache(), record_evidence=lambda *a:None,
                web_execute=public, weread_execute=browser)
    assert r["status"] == "partial" and r["items"][0]["url"] == URL
    assert [s["source"] for s in r["metadata"]["source_outcomes"]] == ["web", "weread"]


def test_browser_results_not_cached_across_account_changes():
    calls = []
    def browser(*a, **kw):
        calls.append(1)
        return {"items": [{"url": URL + str(len(calls)), "title": "研究"}], "status": "ok"}
    cache = SearchCache()
    args = dict(source="weread", cache=cache, record_evidence=lambda *a: None, weread_execute=browser)
    one, two = execute("研究", **args), execute("研究", **args)
    assert len(calls) == 2 and one["items"][0]["url"] != two["items"][0]["url"]


def test_unresolved_candidate_preserved_without_fabricated_article_url():
    r = execute("研究", source="weread", cache=SearchCache(),record_evidence=lambda *a:None,
        weread_execute=lambda *a,**kw:{"status":"partial","items":[{"candidate_ref":"123", "url":"", "title":"待解析"}]})
    assert r["acquired_items"][0]["url"] == ""
    assert r["acquired_items"][0]["detail_supported"] is False


def test_mcp_and_cli_share_weread_login_handoff(monkeypatch):
    from runtime import composition, application  # noqa: F401 -- 注册应用合同，未启动原生MCP
    calls = []
    monkeypatch.setattr(weread, "begin_login", lambda: calls.append(1) or {"ok": True})
    r = application.invoke("health_check", {"mode":"request_browser_interaction:weread:login"})
    assert r["status"] == "waiting_for_user" and calls == [1]


def test_scroll_is_bounded_and_does_not_invent_pagination():
    calls=[]
    base=invoke_with({"items":[{"candidate_ref":"x","url":URL,"title":"研究"}]})
    def call(**kw):
        calls.append(kw)
        return base(**kw)
    r=weread.search("研究",limit=20,invoke=call)
    steps=calls[-1]["steps"]
    assert len(steps)==3 and "cards.length>=20" in steps[1]["expression"]
    assert r["metadata"]["expansion"]["max_scrolls"]==2
    assert r["metadata"]["continuation"]["mode"]=="refine_query"


def test_dual_source_threads_preserve_trace(monkeypatch):
    from runtime.tool_trace import current_tool_trace,set_current_tool_trace
    trace={"call_id":"wechat-isolated-test"}
    previous=current_tool_trace()
    set_current_tool_trace(trace)
    def source(*a,**kw):
        assert current_tool_trace() == trace
        return {"items":[],"status":"empty"}
    try:
        execute("研究",cache=SearchCache(),record_evidence=lambda *a:None,
                web_execute=source,weread_execute=source)
    finally:
        set_current_tool_trace(previous)
