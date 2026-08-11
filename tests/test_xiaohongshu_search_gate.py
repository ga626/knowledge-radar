import server
from collectors.platform import xiaohongshu as xhs


def test_xhs_account_state_accepts_the_concrete_selected_profile_cdp_url() -> None:
    endpoint = "http://127.0.0.1:9223"

    assert xhs._resolve_xhs_cdp_url(endpoint) == endpoint
    assert xhs._resolve_xhs_cdp_url(lambda platform: f"http://127.0.0.1/{platform}") == "http://127.0.0.1/xhs"


def test_xiaohongshu_search_ignores_detail_health_degradation(monkeypatch) -> None:
    monkeypatch.setattr(server, "_xhs_search_gate_state", lambda: {"active": False})
    monkeypatch.setattr(server, "_xhs_try_clear_login_cooldown", lambda gate: gate)
    monkeypatch.setattr(server, "_xiaohongshu_expected_degraded", lambda: {"type": "expected_degraded"})
    monkeypatch.setattr(server, "_xhs_budget_state", lambda: {"status": "ok"})
    monkeypatch.setattr(server, "_record_search_evidence", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_record_xhs_search_gate", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_attach_xiaohongshu_error_context", lambda result, **kwargs: result)

    def fake_cached_search(*args, **kwargs):
        return {
            "items": [{"title": "sample note", "url": "https://www.xiaohongshu.com/explore/test"}],
            "total": 1,
            "platform": "小红书",
        }

    monkeypatch.setattr(server, "_cached_search", fake_cached_search)

    result = server.search_xiaohongshu("Python 3.13 新特性", limit=1, search_type="image")

    assert result["total"] == 1
    assert result["items"][0]["title"] == "sample note"
    assert not result.get("metadata", {}).get("skipped_by_health_gate")


def test_xiaohongshu_search_gate_forwards_active_cooldown_to_collector(monkeypatch) -> None:
    gate = {"active": True, "last_outcome": "rate_limited", "cooldown_until": 1234, "next_retry_at": 1234}
    monkeypatch.setattr(server, "_xhs_search_gate_state", lambda: gate)
    monkeypatch.setattr(server, "_xhs_budget_state", lambda: {"status": "ok"})
    monkeypatch.setattr(server, "_record_search_evidence", lambda *args, **kwargs: None)
    seen = []

    def fake_cached_search(*args, **kwargs):
        seen.append((args, kwargs))
        return {
            "items": [],
            "total": 0,
            "platform": "小红书",
            "error": {"type": "no_usable_xhs_evidence", "error": "all admitted routes exhausted"},
            "metadata": {"collection": {"attempts": [{"name": "previous_global_gate_observed", "status": "ok"}, {"name": "external_search_then_detail", "status": "failed"}, {"name": "tikhub_break_glass", "status": "skipped"}]}},
        }

    monkeypatch.setattr(server, "_cached_search", fake_cached_search)

    result = server.search_xiaohongshu("Python 3.13 新特性", limit=1, search_type="image")

    assert seen
    assert result["total"] == 0
    receipt = result["metadata"]["xhs_route_receipt"]
    assert receipt["entry_gate_observed"] is True
    assert [item["stage"] for item in receipt["attempts"]] == ["previous_global_gate_observed", "external_search_then_detail", "tikhub_break_glass"]
    assert "scrapling_cdp" in receipt["not_attempted_routes"]


def test_xiaohongshu_historical_manual_gate_is_collector_context_not_a_top_level_stop(monkeypatch) -> None:
    gate = {
        "active": True,
        "last_outcome": "blocked",
        "last_reason": "搜索入口触发账号安全验证；详情页链路可能仍可用",
        "last_search_type": "all",
        "cooldown_until": 1234,
        "next_retry_at": 1234,
        "metadata": {
            "manual_action_required": True,
            "platform_state": "platform_verification_required",
            "failure_type": "anti_bot_verification",
        },
    }
    monkeypatch.setattr(server, "_xhs_search_gate_state", lambda: gate)
    monkeypatch.setattr(server, "_xhs_budget_state", lambda: {"status": "degraded"})
    monkeypatch.setattr(server, "_record_search_evidence", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        server,
        "_cached_search",
        lambda *args, **kwargs: {
            "items": [],
            "total": 0,
            "platform": "小红书",
            "error": {"type": "no_usable_xhs_evidence", "manual_action_required": True, "platform_state": "platform_verification_required"},
            "metadata": {"collection": {"attempts": [{"name": "previous_global_gate_observed", "status": "ok"}, {"name": "login_preflight", "status": "failed"}, {"name": "tikhub_break_glass", "status": "skipped"}]}},
        },
    )

    result = server.search_xiaohongshu("Python 3.13 新特性", limit=1, search_type="image")

    assert result["total"] == 0
    assert result["error"]["type"] == "no_usable_xhs_evidence"
    assert result["error"]["status_class"] == "NEEDS_INTERACTION"
    assert result["error"]["manual_action_required"] is True
    assert result["error"]["platform_state"] == "platform_verification_required"
    assert result["metadata"]["status_class"] == "NEEDS_INTERACTION"
    assert result["error"]["manual_interaction_envelope"]["status"] == "NEEDS_INTERACTION"
    assert result["metadata"]["xhs_route_receipt"]["entry_gate_observed"] is True


def test_collector_historical_gate_attempts_external_then_admitted_fallbacks(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(xhs, "_xhs_search_gate_active", lambda: {"active": True, "last_outcome": "failed", "cooldown_remaining_s": 60})
    monkeypatch.setattr(xhs, "_recent_xhs_search_verified", lambda: False)
    monkeypatch.setattr(xhs, "_xhs_low_frequency_guard", lambda *args, **kwargs: {})
    def fake_external(*args, **kwargs):
        calls.append("external")
        kwargs["trace"].add("external_search_then_detail", "failed", detail="no_candidates")
        return {"items": [], "error": {"type": "no_candidates"}}

    monkeypatch.setattr(xhs, "_external_search_then_detail", fake_external)
    monkeypatch.setattr(xhs, "_ensure_chrome_debugging", lambda *_args, **_kwargs: calls.append("cdp") or False)
    monkeypatch.setattr(xhs, "_auto_switch_xhs_account", lambda *args, **kwargs: calls.append("switch") or {"status": "skipped"})
    monkeypatch.setattr(xhs, "_try_tikhub_break_glass_search", lambda *args, **kwargs: calls.append("tikhub") or None)
    monkeypatch.setattr(xhs, "_xhs_probe_payload", lambda **kwargs: kwargs)

    result = xhs._legacy_search_xiaohongshu_impl("真实调研问题", limit=1)

    assert calls == ["external", "cdp", "switch", "tikhub"]
    attempts = result["metadata"]["collection"]["attempts"]
    assert any(item["name"] == "previous_global_gate_observed" for item in attempts)
    assert any(item["name"] == "external_search_then_detail" for item in attempts)


def test_security_verification_prompts_each_profile_while_following_route_order(monkeypatch) -> None:
    states = [
        {"code": -1, "has_verify_prompt": True, "msg": "B verification"},
        {"code": -1, "has_verify_prompt": True, "msg": "A verification"},
        {"code": -1, "has_verify_prompt": True, "msg": "C verification"},
    ]
    switches = iter(["xhs-a", "xhs-c"])
    prompts = []
    switch_calls = []

    monkeypatch.setattr(xhs, "_xhs_search_gate_active", lambda: {"active": False})
    monkeypatch.setattr(xhs, "_recent_xhs_search_verified", lambda: False)
    monkeypatch.setattr(xhs, "_xhs_low_frequency_guard", lambda *args, **kwargs: {})
    monkeypatch.setattr(xhs, "_selected_xhs_profile_id", lambda: "xhs-b")
    monkeypatch.setattr(xhs, "_ensure_chrome_debugging", lambda *args, **kwargs: True)
    monkeypatch.setattr(xhs, "_chrome_debug_url", lambda resource: f"http://127.0.0.1/{resource}")
    monkeypatch.setattr(xhs, "xiaohongshu_account_state", lambda *_args: states.pop(0))
    monkeypatch.setattr(xhs, "_xhs_login_state_ok", lambda _state: False)
    monkeypatch.setattr(
        xhs,
        "request_user_login",
        lambda platform, reason, **kwargs: prompts.append((platform, reason, kwargs["target_profile_id"])) or {"status": "waiting_for_user"},
    )
    monkeypatch.setattr(
        xhs,
        "_auto_switch_xhs_account",
        lambda **kwargs: switch_calls.append(kwargs) or {"switch": {"status": "ok", "target_profile_id": next(switches)}},
    )
    monkeypatch.setattr(xhs, "_try_tikhub_break_glass_search", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        xhs,
        "_xhs_login_preflight_result",
        lambda trace, state, **kwargs: {"items": [], "total": 0, "platform": "小红书", "error": {"type": "login_required"}},
    )

    xhs._legacy_search_xiaohongshu_impl("验证路由矩阵", limit=1)

    assert [item[2] for item in prompts] == ["xhs-b", "xhs-a", "xhs-c"]
    assert [item["current_profile_id"] for item in switch_calls] == ["xhs-b", "xhs-a"]
    assert all(item["allow_manual_recovery_followup"] is True for item in switch_calls)


def test_xiaohongshu_manual_error_exposes_resumable_envelope() -> None:
    result = server._attach_xiaohongshu_error_context(
        {
            "items": [],
            "total": 0,
            "platform": "小红书",
            "error": {
                "type": "expected_degraded",
                "manual_action_required": True,
                "platform_state": "platform_verification_required",
            },
        }
    )

    envelope = result["error"]["manual_interaction_envelope"]
    assert envelope["status"] == "NEEDS_INTERACTION"
    assert envelope["platform"] == "xhs"
    assert envelope["original_tool"] == "search_xiaohongshu"
    assert envelope["resume_policy"] == "retry_once_after_complete"
    assert result["error"]["status_class"] == "NEEDS_INTERACTION"
    assert result["metadata"]["status_class"] == "NEEDS_INTERACTION"
    assert result["metadata"]["expected_degraded"] is False


def test_xiaohongshu_cdp_unavailable_does_not_request_manual_interaction(monkeypatch) -> None:
    interactions = []
    monkeypatch.setattr(
        server,
        "request_browser_interaction",
        lambda platform, reason: interactions.append((platform, reason)) or {"status": "waiting_for_user"},
    )

    result = server._attach_xiaohongshu_error_context(
        {
            "items": [],
            "total": 0,
            "platform": "小红书",
            "error": {
                "error": "Chrome/CDP 不可用，无法执行小红书搜索",
                "type": "cdp_unavailable",
                "retryable": True,
            },
        }
    )

    assert result["error"]["status_class"] == "RETRY_LATER"
    assert result["error"]["manual_action_required"] is False
    assert "manual_interaction" not in result["error"]
    assert "manual_interaction_request" not in result.get("metadata", {})
    assert interactions == []
