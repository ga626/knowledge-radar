from __future__ import annotations
from conftest import patch_native

import json
import pytest
import time
from pathlib import Path

from collectors.platform import liepin
from collectors.platform import xiaohongshu as xhs
from kr_core.collection import CollectionTrace, format_search_error
from runtime.browser_sessions import BrowserSessionStore
from runtime.profile_registry import raw_registry_for_platform, select_main_chain_profile
from runtime import recruitment_governance


def test_profile_registry_scopes_raw_rows_by_platform(monkeypatch, tmp_path: Path) -> None:
    profile_dir = tmp_path / "xhs-profile"
    profile_dir.mkdir()
    registry = tmp_path / "profile_registry.json"
    registry.write_text(
        """
{
  "schema": "knowledgeradar-profile-registry/v1",
  "accounts": [
    {"platform": "liepin", "account_slot": "liepin-a"},
    {"platform": "xiaohongshu", "account_slot": "xhs-a"}
  ],
  "profiles": [
    {"platform": "liepin", "profile_id": "liepin-profile", "account_slot": "liepin-a", "main_chain_allowed": true, "profile_dir": "missing"},
    {"platform": "xiaohongshu", "profile_id": "xhs-profile", "account_slot": "xhs-a", "main_chain_allowed": true, "profile_dir": "__PROFILE_DIR__"}
  ],
  "bindings": [
    {"platform": "liepin", "profile_id": "liepin-profile", "account_slot": "liepin-a"},
    {"platform": "xiaohongshu", "profile_id": "xhs-profile", "account_slot": "xhs-a"}
  ],
  "policy": {}
}
""".replace("__PROFILE_DIR__", str(profile_dir).replace("\\", "\\\\")),
        encoding="utf-8",
    )
    monkeypatch.setenv("KR_PROFILE_REGISTRY_PATH", str(registry))
    monkeypatch.setenv("KR_PROFILE_STATE_PATH", str(tmp_path / "state.json"))

    xhs_raw = raw_registry_for_platform("xhs")
    assert [row["account_slot"] for row in xhs_raw["accounts"]] == ["xhs-a"]
    assert [row["profile_id"] for row in xhs_raw["profiles"]] == ["xhs-profile"]
    assert select_main_chain_profile("xhs")["profile_id"] == "xhs-profile"


def test_browser_session_upsert_uses_profile_scope(tmp_path: Path) -> None:
    store = BrowserSessionStore(tmp_path / "sessions.json", tmp_path / "events.jsonl")
    first = store.upsert(platform="xhs", debug_port="12733", profile_dir=str(tmp_path / "a"), profile_id="xhs-a")
    second = store.upsert(platform="xhs", debug_port="12734", profile_dir=str(tmp_path / "b"), profile_id="xhs-b")
    reused = store.upsert(platform="xhs", debug_port="12733", profile_dir=str(tmp_path / "a"), profile_id="xhs-a")

    assert first["session_id"] != second["session_id"]
    assert first["session_id"] == reused["session_id"]


def test_manual_registry_platform_enters_chrome_runtime(monkeypatch, tmp_path: Path) -> None:
    profile_dir = tmp_path / "vip-profile"
    profile_dir.mkdir()
    registry = tmp_path / "profile_registry.json"
    registry.write_text(
        """
{
  "schema": "knowledgeradar-profile-registry/v1",
  "accounts": [],
  "profiles": [
    {"platform": "vip_oa", "profile_id": "vip-profile", "account_slot": "vip-a", "role": "manual_candidate", "channel_id": "authorized_browser_profile", "browser_base": "chrome_manual", "main_chain_allowed": false, "profile_dir": "__PROFILE_DIR__"}
  ],
  "bindings": [],
  "policy": {}
}
""".replace("__PROFILE_DIR__", str(profile_dir).replace("\\", "\\\\")),
        encoding="utf-8",
    )
    monkeypatch.setenv("KR_PROFILE_REGISTRY_PATH", str(registry))
    monkeypatch.setenv("KR_PROFILE_STATE_PATH", str(tmp_path / "state.json"))

    from runtime import chrome_manager

    assert "vip_oa" in chrome_manager.managed_browser_platforms()
    assert "xiaohongshu" not in chrome_manager.managed_browser_platforms()
    summary = chrome_manager.chrome_runtime_summary()
    assert "vip_oa" in summary["platforms"]
    assert summary["platforms"]["vip_oa"]["port"] == "12741"
    assert summary["platforms"]["vip_oa"]["manual_probe"]["startup_url"] == "https://www.cqvip.com/search?k=ai"


def test_stealth_chrome_flags_are_opt_in_for_managed_browsers() -> None:
    from runtime import chrome_manager

    assert chrome_manager._chrome_stealth_flags_for_platform("vip_oa") == []
    assert chrome_manager._chrome_stealth_flags_for_platform("socolar") == []
    assert chrome_manager._chrome_stealth_flags_for_platform("xhs") == []
    assert chrome_manager._chrome_stealth_flags_for_platform("boss") == []


def test_manual_browser_constants_enter_runtime_without_registry(monkeypatch, tmp_path: Path) -> None:
    registry = tmp_path / "profile_registry.json"
    registry.write_text(
        """
{
  "schema": "knowledgeradar-profile-registry/v1",
  "accounts": [],
  "profiles": [],
  "bindings": [],
  "policy": {}
}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("KR_PROFILE_REGISTRY_PATH", str(registry))
    monkeypatch.setenv("KR_PROFILE_STATE_PATH", str(tmp_path / "state.json"))

    from runtime import chrome_manager

    assert "socolar" not in chrome_manager.managed_browser_platforms()
    calls=[]
    monkeypatch.setattr(chrome_manager,"_ensure_chrome_debugging",lambda *a,**k:calls.append(a))
    result=chrome_manager.probe_browser_auth("socolar")
    assert result["status"]=="error" and result["auth_state"]=="unsupported_platform" and not calls


def test_manual_browser_interaction_launches_visible_managed_chrome(monkeypatch, tmp_path: Path) -> None:
    from runtime import chrome_manager
    from runtime.leases import RuntimeLeaseCoordinator

    profile_dir = tmp_path / "vip-profile"
    profile_dir.mkdir()
    monkeypatch.setattr(chrome_manager, "get_runtime_lease_coordinator", lambda: RuntimeLeaseCoordinator(tmp_path / "leases.sqlite3"))
    monkeypatch.setattr(chrome_manager, "_managed_chrome_profile_dir", lambda platform: str(profile_dir))
    monkeypatch.setattr(chrome_manager, "_profile_metadata_for_platform", lambda platform: {"profile_id": "vip-profile", "account_slot": "vip-a", "channel_id": "authorized_browser_profile"})
    calls = []

    def fake_ensure(platform: str, *, visible: bool = False, detach: bool = False) -> bool:
        calls.append((platform, visible, detach))
        chrome_manager._MANAGED_CHROME_PIDS[platform] = 12345
        return True

    monkeypatch.setattr(chrome_manager, "_ensure_chrome_debugging", fake_ensure)
    monkeypatch.setattr(chrome_manager, "bring_chrome_to_front", lambda platform: {"status": "ok", "platform": platform, "pid": 12345})
    monkeypatch.setattr(chrome_manager, "_find_chrome_with_debug_port", lambda platform: 12345)

    result = chrome_manager.request_browser_interaction("vip_oa", reason="manual_login")

    assert calls == [("vip_oa", False, False)]
    assert chrome_manager._CHROME_KEEP_ALIVE["vip_oa"] is True
    assert result["status"] == "waiting_for_user"
    assert result["lease"]["acquired"] is True
    assert result["next_step"] == "登录完成后，调用 health_check(mode='complete_browser_interaction:vip_oa') 恢复普通浏览器生命周期"


def test_manual_browser_interaction_busy_does_not_open_second_window(monkeypatch, tmp_path: Path) -> None:
    from runtime import chrome_manager
    from runtime.leases import RuntimeLeaseCoordinator

    coordinator = RuntimeLeaseCoordinator(tmp_path / "leases.sqlite3")
    monkeypatch.setattr(chrome_manager, "get_runtime_lease_coordinator", lambda: coordinator)
    monkeypatch.setattr(chrome_manager, "_managed_chrome_profile_dir", lambda platform: str(tmp_path / "vip-profile"))
    monkeypatch.setattr(chrome_manager, "_profile_metadata_for_platform", lambda platform: {"profile_id": "vip-profile", "account_slot": "vip-a"})

    first = coordinator.acquire_exclusive("manual_interaction", "vip_oa:vip-profile:vip-a:12741", owner={"client_id": "other"}, ttl_s=60)
    assert first.acquired is True
    calls = []
    monkeypatch.setattr(chrome_manager, "_ensure_chrome_debugging", lambda *args, **kwargs: calls.append((args, kwargs)) or True)

    result = chrome_manager.request_browser_interaction("vip_oa", reason="manual_login")

    assert result["status"] == "busy"
    assert result["validation_status"] == "EXPECTED_DEGRADED"
    assert calls == []


def test_complete_manual_browser_interaction_returns_to_operation_cleanup(monkeypatch, tmp_path: Path) -> None:
    from runtime import chrome_manager

    profile_dir = tmp_path / "vip-profile"
    profile_dir.mkdir()
    chrome_manager._CHROME_KEEP_ALIVE["vip_oa"] = True
    chrome_manager._MANAGED_CHROME_PIDS["vip_oa"] = 12345
    monkeypatch.setattr(chrome_manager, "_find_chrome_with_debug_port", lambda platform: 12345)
    monkeypatch.setattr(chrome_manager, "_minimize_chrome_windows", lambda pid: None)
    cleanup_calls = []
    monkeypatch.setattr(chrome_manager, "finish_chrome_automation", lambda platform, reason="": cleanup_calls.append((platform, reason)))

    result = chrome_manager.complete_browser_interaction("vip_oa", {"status": "ok"})

    assert result["status"] == "ok"
    assert chrome_manager._CHROME_KEEP_ALIVE["vip_oa"] is False
    assert cleanup_calls == [("vip_oa", "user_login_complete")]


def test_complete_manual_browser_interaction_records_auto_recovered_probe(monkeypatch, tmp_path: Path) -> None:
    from runtime import chrome_manager

    chrome_manager._CHROME_KEEP_ALIVE["vip_oa"] = True
    chrome_manager._MANAGED_CHROME_PIDS["vip_oa"] = 12345
    monkeypatch.setattr(chrome_manager, "_find_chrome_with_debug_port", lambda platform: 12345)
    monkeypatch.setattr(chrome_manager, "_minimize_chrome_windows", lambda pid: None)
    monkeypatch.setattr(chrome_manager, "finish_chrome_automation", lambda platform, reason="": None)
    transitions = []
    original_transition = chrome_manager.transition_browser_session

    def tracking_transition(*args, **kwargs):
        transitions.append((args, kwargs))
        return original_transition(*args, **kwargs)

    monkeypatch.setattr(chrome_manager, "transition_browser_session", tracking_transition)

    result = chrome_manager.complete_browser_interaction(
        "vip_oa",
        {"status": "ok", "manual_action_required": False, "platform_state": "search_ok"},
    )

    assert result["status"] == "ok"
    assert result["manual_state_auto_recovered"] is True
    assert any(call[1].get("event") == "manual_state_auto_recovered" for call in transitions)






def test_browser_auth_probe_does_not_request_foreground_when_authenticated(monkeypatch, tmp_path: Path) -> None:
    from runtime import chrome_manager

    monkeypatch.setattr(chrome_manager, "managed_browser_platforms", lambda: ("vip_oa",))
    ensure_calls = []
    monkeypatch.setattr(chrome_manager, "_ensure_chrome_debugging", lambda platform, visible=False, detach=False: ensure_calls.append((platform, visible, detach)) or True)
    monkeypatch.setattr(chrome_manager, "_read_browser_cookie_names", lambda platform: ["journalOA-token", "_qdda"])
    cleanup_calls = []
    monkeypatch.setattr(chrome_manager, "finish_chrome_automation", lambda platform, reason="": cleanup_calls.append((platform, reason)))

    result = chrome_manager.probe_browser_auth("vip_oa")

    assert result["status"] == "ok"
    assert result["manual_action_required"] is False
    assert ensure_calls == [("vip_oa", False, False)]
    assert cleanup_calls == [("vip_oa", "browser_auth_probe")]


def test_browser_auth_probe_reports_needs_interaction_without_opening_browser(monkeypatch, tmp_path: Path) -> None:
    from runtime import chrome_manager

    monkeypatch.setattr(chrome_manager, "managed_browser_platforms", lambda: ("vip_oa",))
    monkeypatch.setattr(chrome_manager, "_ensure_chrome_debugging", lambda platform, visible=False, detach=False: True)
    monkeypatch.setattr(chrome_manager, "_read_browser_cookie_names", lambda platform: ["_qdda"])
    monkeypatch.setattr(chrome_manager, "finish_chrome_automation", lambda platform, reason="": None)

    result = chrome_manager.probe_browser_auth("vip_oa")

    assert result["status"] == "needs_interaction"
    assert result["manual_action_required"] is True
    assert result["recommended_action"] == "health_check(mode='request_browser_interaction:vip_oa:manual_login')"
























def test_search_recruitment_promotes_manual_interaction(monkeypatch) -> None:
    import server

    interactions = []
    class Lease:
        acquired = True
        lease_id = "test-boss-lease"

        def to_dict(self) -> dict:
            return {"acquired": True, "lease_id": self.lease_id}

    monkeypatch.setattr(
        recruitment_governance,
        "check_platform_admission",
        lambda platform, **kwargs: {"allowed": True, "admission": "open", "reason_code": "ok"},
    )
    monkeypatch.setattr(recruitment_governance, "acquire_platform_lease", lambda platform, **kwargs: Lease())
    monkeypatch.setattr(recruitment_governance, "release_platform_lease", lambda lease_id: None)
    monkeypatch.setattr(
        server,
        "_RECRUITMENT_ADAPTERS",
        {
            "boss": lambda request: {
                "items": [],
                "total": 0,
                "platform": "BOSS直聘",
                "error": {
                    "type": "login_required",
                    "failure_type": "login_required",
                    "platform_state": "login_required",
                    "manual_action_required": True,
                },
            }
        },
    )
    monkeypatch.setattr(server, "managed_browser_platforms", lambda: ("boss",))
    monkeypatch.setattr(
        server,
        "request_browser_interaction",
        lambda platform, reason, **kwargs: interactions.append((platform, reason, kwargs))
        or {"status": "waiting_for_user", "platform": platform, "reason": reason, "session_id": "boss-test"},
    )

    result = server.search_recruitment("boss", "Python", city="北京", limit=1)

    assert result["error"]["status_class"] == "NEEDS_INTERACTION"
    assert result["error"]["expected_degraded"] is False
    assert result["metadata"]["status_class"] == "NEEDS_INTERACTION"
    assert result["error"]["manual_interaction_envelope"]["platform"] == "boss"
    assert result["error"]["manual_interaction_envelope"]["original_tool"] == "search_recruitment"
    assert result["error"]["manual_interaction"]["status"] == "waiting_for_user"
    assert result["error"]["manual_interaction"]["manual_open_mode"] == "health_check(mode='request_browser_interaction:boss:login_required')"
    assert interactions[0][0] == "boss"


def test_search_recruitment_does_not_open_browser_for_liepin_ambiguous(monkeypatch) -> None:
    import server

    interactions = []
    class Lease:
        acquired = True
        lease_id = "test-liepin-lease"

        def to_dict(self) -> dict:
            return {"acquired": True, "lease_id": self.lease_id}

    monkeypatch.setattr(
        recruitment_governance,
        "check_platform_admission",
        lambda platform, **kwargs: {"allowed": True, "admission": "open", "reason_code": "ok"},
    )
    monkeypatch.setattr(recruitment_governance, "acquire_platform_lease", lambda platform, **kwargs: Lease())
    monkeypatch.setattr(recruitment_governance, "release_platform_lease", lambda lease_id: None)
    monkeypatch.setattr(
        server,
        "_RECRUITMENT_ADAPTERS",
        {
            "liepin": lambda request: {
                "items": [],
                "total": 0,
                "platform": "猎聘",
                "error": {
                    "error": "猎聘搜索无结果",
                    "type": "ambiguous_page_state",
                    "failure_type": "ambiguous_page_state",
                    "platform_state": "suspected_manual_gate_not_confirmed",
                    "manual_action_required": False,
                    "manual_confidence": "suspected",
                },
            }
        },
    )
    monkeypatch.setattr(server, "managed_browser_platforms", lambda: ("liepin",))
    monkeypatch.setattr(
        server,
        "request_browser_interaction",
        lambda platform, reason: interactions.append((platform, reason))
        or {"status": "waiting_for_user", "platform": platform, "reason": reason, "session_id": "liepin-test"},
    )

    result = server.search_recruitment("liepin", "Python", city="杭州", limit=1)

    assert result["error"]["manual_action_required"] is False
    assert result["error"]["manual_confidence"] == "suspected"
    assert "manual_interaction" not in result["error"]
    assert interactions == []


def test_search_recruitment_uses_weak_web_fallback_for_native_route_failure(monkeypatch) -> None:
    import server

    class Lease:
        acquired = True
        lease_id = "test-boss-lease"

        def to_dict(self) -> dict:
            return {"acquired": True, "lease_id": self.lease_id}

    class WebResult:
        def to_mcp_dict(self) -> dict:
            return {
                "provider": "test",
                "items": [
                    {
                        "title": "AI产品经理 - 杭州",
                        "url": "https://www.zhipin.com/job_detail/1.html",
                        "snippet": "杭州 AI产品经理 招聘",
                    }
                ],
            }

    interactions = []
    monkeypatch.setattr(
        recruitment_governance,
        "check_platform_admission",
        lambda platform, **kwargs: {"allowed": True, "admission": "open", "reason_code": "ok"},
    )
    monkeypatch.setattr(recruitment_governance, "acquire_platform_lease", lambda platform, **kwargs: Lease())
    monkeypatch.setattr(recruitment_governance, "release_platform_lease", lambda lease_id: None)
    monkeypatch.setattr(
        server,
        "_RECRUITMENT_ADAPTERS",
        {
            "boss": lambda request: {
                "items": [],
                "total": 0,
                "platform": "BOSS直聘",
                "error": {
                    "error": "BOSS原生列表不可读",
                    "type": "tool_failure_needs_repair",
                    "failure_type": "tool_failure_needs_repair",
                    "platform_state": "search_route_unreadable",
                    "manual_action_required": False,
                },
            }
        },
    )
    monkeypatch.setattr(server, "managed_browser_platforms", lambda: ("boss",))
    monkeypatch.setattr(server, "request_browser_interaction", lambda platform, reason: interactions.append((platform, reason)))
    monkeypatch.setattr(server, "search_web", lambda request: WebResult())

    result = server.search_recruitment("boss", "AI产品经理", city="杭州", limit=3)

    assert result["items"]
    assert result["items"][0]["source"] == "web_search_fallback_after_native_route_failure"
    assert result["items"][0]["evidence_strength"] == "weak_open_index"
    assert result["metadata"]["native_route_failure"]["failure_type"] == "tool_failure_needs_repair"
    assert result["metadata"]["market_claim_allowed"] is False
    assert interactions == []






def test_search_recruitment_rejects_busy_platform_lease(monkeypatch) -> None:
    import server

    monkeypatch.setattr(
        recruitment_governance,
        "check_platform_admission",
        lambda platform, **kwargs: {"allowed": True, "admission": "open", "reason_code": "ok"},
    )

    class BusyLease:
        acquired = False
        retry_after_s = 17

        def to_dict(self) -> dict:
            return {"acquired": False, "reason": "lease_unavailable", "retry_after_s": 17}

    monkeypatch.setattr(recruitment_governance, "acquire_platform_lease", lambda platform, **kwargs: BusyLease())
    monkeypatch.setattr(recruitment_governance, "release_platform_lease", lambda lease_id: (_ for _ in ()).throw(AssertionError("no lease acquired")))
    monkeypatch.setattr(
        server,
        "_RECRUITMENT_ADAPTERS",
        {"liepin": lambda request: (_ for _ in ()).throw(AssertionError("adapter should not run"))},
    )

    result = server.search_recruitment("liepin", "Python", city="杭州", limit=1)

    assert result["error"]["failure_type"] == "platform_lease_busy"
    assert result["error"]["reason_code"] == "platform_lease_busy"
    assert result["error"]["retry_after_s"] == 17
    assert result["market_claim_allowed"] is False


def test_recruitment_admission_blocks_existing_manual_session(monkeypatch):
    monkeypatch.setattr(liepin,'admission',lambda:{'allowed':False,'reason':'pending_manual_interaction'})
    result=recruitment_governance.check_platform_admission('liepin',keyword='Python',city='杭州')
    assert not result['allowed'] and result['manual_action_required']
    assert result['reason_code']=='pending_manual_interaction'


def test_recruitment_admission_auto_recovers_authenticated_manual_session(monkeypatch):
    # 准入不发探针；人工确认完成才换认证代际并允许显式重试。
    from runtime.state_file import write_state
    write_state(liepin._site().state_path(),{'expires_at':time.time()+300})
    patch_native(monkeypatch,lambda **kw: (_ for _ in ()).throw(AssertionError('admission must be pure')))
    assert not recruitment_governance.check_platform_admission('liepin')['allowed']
    patch_native(monkeypatch,lambda **kw:{'ok':True,'data':{'authenticated_ui':True}})
    assert liepin.complete_login()['status']=='ok'
    assert recruitment_governance.check_platform_admission('liepin')['allowed']


def test_liepin_city_filter_drops_explicit_mismatch() -> None:
    items = [
        {"title": "AI 产品经理", "area": "深圳"},
        {"title": "AI 产品经理", "area": "杭州"},
        {"title": "AI 产品经理", "area": "远程"},
        {"title": "AI 产品经理", "area": ""},
        {"title": "行政【合肥-瑶海区】", "area": ""},
    ]

    filtered, summary = liepin._apply_city_filter(items, "杭州")

    assert [item["city_match"] for item in filtered] == ["match", "remote", "unknown"]
    assert summary["dropped_count"] == 2


def test_liepin_detail_runtime_evaluate_error_is_collector_script_error(monkeypatch):
    monkeypatch.setattr(liepin,'_static_liepin_detail',lambda url:None)
    patch_native(monkeypatch,lambda **kw:{'ok':False,'code':'content_failed','request_executed':True})
    result=liepin.detail('https://www.liepin.com/job/1977755243.shtml')
    assert result['failure_type']=='content_failed' and not result['manual_action_required']


def test_liepin_soft_security_prompt_with_results_is_readable() -> None:
    result = liepin._finalize_liepin_search_state(
        {
            "items": [
                {
                    "title": "Python 工程师",
                    "area": "杭州",
                    "url": "https://www.liepin.com/job/1",
                }
            ],
            "blocked": True,
            "loginRequired": False,
            "cardCount": 12,
            "jobLinkCount": 8,
        },
        city="杭州",
        limit=3,
    )

    assert result["status"] == "ok"
    assert result["manual_action_required"] is False
    assert result["warning_type"] == "soft_security_prompt_with_results"
    assert result["page_state"]["result_readability"] == "readable"
    assert result["items"][0]["title"] == "Python 工程师"


















def test_liepin_weak_security_prompt_without_results_is_not_manual() -> None:
    result = liepin._finalize_liepin_search_state(
        {
            "items": [],
            "blocked": True,
            "loginRequired": False,
            "cardCount": 0,
            "jobLinkCount": 0,
        },
        city="杭州",
        limit=3,
    )

    assert result["status"] == "ambiguous"
    assert result["failure_type"] == "ambiguous_page_state"
    assert result["platform_state"] == "suspected_manual_gate_not_confirmed"
    assert result["manual_action_required"] is False
    assert result["manual_confidence"] == "suspected"


def test_liepin_hard_security_prompt_without_results_requires_interaction() -> None:
    result = liepin._finalize_liepin_search_state(
        {
            "items": [],
            "blocked": True,
            "loginRequired": False,
            "cardCount": 0,
            "jobLinkCount": 0,
            "captchaElementCount": 1,
            "page_state": {
                "blocked_marker": True,
                "captcha_element_count": 1,
                "security_evidence_strength": "strong",
            },
        },
        city="杭州",
        limit=3,
    )

    assert result["status"] == "needs_interaction"
    assert result["failure_type"] == "platform_verification_required"
    assert result["platform_state"] == "hard_security_block"
    assert result["manual_action_required"] is True
    assert result["manual_confidence"] == "confirmed"


def test_liepin_login_prompt_without_results_requires_interaction() -> None:
    result = liepin._finalize_liepin_search_state(
        {
            "items": [],
            "blocked": False,
            "loginRequired": True,
            "cardCount": 0,
            "jobLinkCount": 0,
            "loginModalCount": 1,
            "page_state": {
                "login_marker": True,
                "login_modal_count": 1,
                "login_evidence_strength": "strong",
            },
        },
        city="杭州",
        limit=3,
    )

    assert result["status"] == "needs_interaction"
    assert result["failure_type"] == "login_required"
    assert result["platform_state"] == "login_required"
    assert result["manual_action_required"] is True
    assert result["manual_confidence"] == "confirmed"


def test_liepin_detail_readability_beats_security_marker(monkeypatch):
    jd=('岗位职责：负责Python开发设计。任职要求：熟悉数据和异步，具备项目经验。'*12)
    url='https://www.liepin.com/job/1'
    monkeypatch.setattr(liepin,'_static_liepin_detail',lambda url:None)
    patch_native(monkeypatch,lambda **kw:{'ok':True,'data':{'url':url,'title':'工程师','content':jd,'extraction_source':'job_intro_primary','risk':False}})
    result=liepin.detail(url)
    assert result['status']=='ok' and '岗位职责' in result['content']
    assert result['detail_quality']['ok']


def test_search_error_normalizes_failure_type_when_type_missing() -> None:
    result = format_search_error(
        "猎聘",
        {
            "error": "猎聘搜索无结果",
            "hint": "页面异常，未自动判定登录或安全验证。",
            "failure_type": "city_mismatch",
            "platform_state": "city_mismatch",
            "manual_action_required": False,
        },
        strategy="chrome_cdp_page",
    )

    assert result["error"]["type"] == "city_mismatch"
    assert "login_required" not in result["error"]["failure_tags"]
    assert "anti_bot_verification" not in result["error"]["failure_tags"]


def test_search_error_preserves_ambiguous_page_state() -> None:
    result = format_search_error(
        "猎聘",
        {
            "error": "猎聘搜索无结果",
            "failure_type": "ambiguous_page_state",
            "platform_state": "suspected_manual_gate_not_confirmed",
            "manual_action_required": False,
            "manual_confidence": "suspected",
        },
        strategy="chrome_cdp_page",
    )

    assert result["error"]["type"] == "ambiguous_page_state"
    assert result["error"]["manual_action_required"] is False
    assert result["error"]["manual_confidence"] == "suspected"


def test_boss_failure_budget_is_scoped_by_keyword_and_city(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KR_TASK_DB_PATH", str(tmp_path / "gate.sqlite3"))
    monkeypatch.setitem(
        recruitment_governance.PLATFORM_CONFIG,
        "boss",
        {
            "search_cooldown_s": 0,
            "min_search_interval_s": 0,
            "max_searches_per_hour": 20,
            "max_failures_per_hour": 3,
        },
    )

    for _ in range(3):
        recruitment_governance.record_search_outcome("boss", "failed", "login_required", keyword="Python", city="北京")

    same_scope = recruitment_governance.check_search_gate("boss", keyword="Python", city="北京")
    other_city = recruitment_governance.check_search_gate("boss", keyword="Python", city="杭州")

    assert same_scope["allowed"] is False
    assert "失败次数已达上限" in same_scope["reason"]
    assert other_city["allowed"] is True


def test_boss_login_failure_does_not_hard_cooldown_recovery(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KR_TASK_DB_PATH", str(tmp_path / "gate.sqlite3"))
    monkeypatch.setitem(
        recruitment_governance.PLATFORM_CONFIG,
        "boss",
        {
            "search_cooldown_s": 1800,
            "min_search_interval_s": 0,
            "max_searches_per_hour": 20,
            "max_failures_per_hour": 3,
        },
    )

    recruitment_governance.record_search_outcome("boss", "failed", "login_required", keyword="Python", city="北京")

    gate = recruitment_governance.check_search_gate("boss", keyword="Python", city="北京")

    assert gate["allowed"] is True
    assert gate["failures_this_hour"] == 1


def test_tool_route_failure_does_not_hard_cooldown_recovery(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KR_TASK_DB_PATH", str(tmp_path / "gate.sqlite3"))
    monkeypatch.setitem(
        recruitment_governance.PLATFORM_CONFIG,
        "liepin",
        {
            "search_cooldown_s": 1800,
            "min_search_interval_s": 0,
            "max_searches_per_hour": 20,
            "max_failures_per_hour": 10,
        },
    )

    recruitment_governance.record_search_outcome("liepin", "failed", "tool_failure_needs_repair", keyword="AI产品经理", city="杭州")

    gate = recruitment_governance.check_search_gate("liepin", keyword="AI产品经理", city="杭州")

    assert gate["allowed"] is True
    assert gate["failures_this_hour"] == 1


def test_legacy_tool_route_cooldown_row_is_ignored(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "gate.sqlite3"
    monkeypatch.setenv("KR_TASK_DB_PATH", str(db_path))
    monkeypatch.setitem(
        recruitment_governance.PLATFORM_CONFIG,
        "liepin",
        {
            "search_cooldown_s": 1800,
            "min_search_interval_s": 0,
            "max_searches_per_hour": 20,
            "max_failures_per_hour": 10,
        },
    )
    conn = recruitment_governance._get_conn()
    now = time.time()
    conn.execute(
        """
        INSERT INTO recruitment_search_gate
          (platform, ts, outcome, reason, cooldown_until, account_slot, keyword_norm, city_norm)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("liepin", now, "failed", "tool_failure_needs_repair", now + 1800, "", "ai产品经理", "杭州"),
    )
    conn.commit()
    conn.close()

    gate = recruitment_governance.check_search_gate("liepin", keyword="AI产品经理", city="杭州")

    assert gate["allowed"] is True
    assert gate["cooldown_remaining_s"] == 0


def test_recruitment_cooldown_doubles_previous_failure(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KR_TASK_DB_PATH", str(tmp_path / "gate.sqlite3"))
    monkeypatch.setitem(
        recruitment_governance.PLATFORM_CONFIG,
        "boss",
        {
            "search_cooldown_s": 10,
            "search_cooldown_max_s": 40,
            "min_search_interval_s": 0,
            "max_searches_per_hour": 20,
            "max_failures_per_hour": 10,
        },
    )

    recruitment_governance.record_search_outcome("boss", "failed", "temporary_blocked", keyword="Python", city="北京")
    first = recruitment_governance.check_search_gate("boss", keyword="Python", city="北京")
    assert first["cooldown_remaining_s"] <= 10

    monkeypatch.setenv("KR_TASK_DB_PATH", str(tmp_path / "gate.sqlite3"))
    recruitment_governance.record_search_outcome("boss", "failed", "temporary_blocked", keyword="Python", city="北京")
    second = recruitment_governance.check_search_gate("boss", keyword="Python", city="北京")

    assert second["allowed"] is False
    assert second["cooldown_remaining_s"] > 10
    assert second["cooldown_remaining_s"] <= 20
    assert second["next_retry_at"] > 0


def test_liepin_empty_search_is_not_manual_action(monkeypatch):
    url=liepin.search_url('zzzz unlikely')
    patch_native(monkeypatch,lambda **kw:{'ok':True,'data':{'items':[],'empty':True,'url':url}})
    result=liepin.legacy_search_liepin('zzzz unlikely','',3)
    assert result['status']=='empty' and not result.get('manual_action_required')




def test_liepin_search_state_marks_unreadable_route_as_tool_failure() -> None:
    result = liepin._finalize_liepin_search_state(
        {
            "items": [],
            "blocked": False,
            "loginRequired": False,
            "cardCount": 0,
            "jobLinkCount": 0,
            "url": "https://www.liepin.com/zhaopin/?key=Python&city=杭州",
            "title": "猎聘",
            "textSample": "Python 杭州 推荐",
            "keyword": "Python",
            "waitState": {"empty": False},
        },
        city="杭州",
        limit=3,
    )

    assert result["status"] == "failed"
    assert result["failure_type"] == "tool_failure_needs_repair"
    assert result["platform_state"] == "search_route_unreadable"
    assert result["manual_action_required"] is False
    assert result["page_state"]["route_probe"]["route_decision"] == "needs_route_repair"


def test_liepin_unknown_city_stops_before_browser(monkeypatch):
    patch_native(monkeypatch,lambda **kw: (_ for _ in ()).throw(AssertionError('must not borrow')))
    result=liepin.legacy_search_liepin('AI产品经理',city='火星',limit=3)
    assert result['failure_type']=='city_mapping_missing' and not result['manual_action_required']
    assert result['request_executed'] is False




def test_liepin_search_state_accepts_network_route_items() -> None:
    result = liepin._finalize_liepin_search_state(
        {
            "items": [
                {
                    "title": "AI平台产品经理",
                    "company": "杭州样例公司",
                    "area": "杭州",
                    "url": "https://www.liepin.com/job/1.shtml",
                    "source": "liepin_network_search",
                    "evidence_strength": "strong_platform_network",
                }
            ],
            "blocked": False,
            "loginRequired": False,
            "cardCount": 0,
            "jobLinkCount": 0,
            "url": "https://www.liepin.com/zhaopin/?key=AI产品经理&city=杭州",
            "title": "猎聘",
            "textSample": "AI产品经理 杭州 推荐",
            "keyword": "AI产品经理",
            "waitState": {"empty": False},
        },
        city="杭州",
        limit=3,
    )

    assert result["status"] == "ok"
    assert result["items"][0]["source"] == "liepin_network_search"
    assert result["failure_type"] == ""
    assert result["manual_action_required"] is False
    assert result["page_state"]["route_probe"]["route_decision"] == "network_items"


@pytest.fixture(autouse=True)
def recruitment_isolation(monkeypatch,tmp_path):
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path/'state'))
    monkeypatch.setenv('KR_TASK_DB_PATH',str(tmp_path/'tasks.sqlite3'))
