import pytest

from runtime.xhs_account_pool import select_account
from runtime.xhs_task_failover import run_browser_accounts
from runtime.xhs_tikhub_fallback import _browser_pool_unavailable


def fixture():
    registry = {"raw": {"policy": {"default_mode": "safe_auto", "max_switches_per_task": 2}}}
    pool = {"accounts": [
        {"profile_id": name, "risk": {"risk_score": 0}, "runtime_state": "healthy"}
        for name in ("A", "B", "C")
    ]}
    return registry, pool


@pytest.mark.parametrize("start", ["A", "B", "C"])
def test_each_start_tries_distinct_accounts_before_paid_admission(start):
    registry, pool = fixture()
    visited = []

    def attempt(profile):
        visited.append(profile)
        return {"error": {"type": "login_required"}}

    _, receipt = run_browser_accounts(registry, pool, start, attempt, lambda *_: None)
    assert visited[0] == start
    assert len(visited) == len(set(visited)) == 3
    assert _browser_pool_unavailable(receipt, {}) is True


def test_cdp_failure_retries_full_search_on_alternate_and_stops_on_success():
    registry, pool = fixture()
    visited = []

    def attempt(profile):
        visited.append(profile)
        return {"error": {"type": "cdp_unavailable"}} if profile == "A" else {"items": [{"title": "real"}]}

    result, receipt = run_browser_accounts(registry, pool, "A", attempt, lambda *_: None)
    assert visited == ["A", "B"]
    assert result["items"]
    assert not _browser_pool_unavailable(receipt, {})


@pytest.mark.parametrize("failure", ["dependency_missing", "configuration_error", "rate_limited", "request_failed"])
def test_shared_or_global_failure_never_rotates_or_spends(failure):
    registry, pool = fixture()
    recorded = []
    _, receipt = run_browser_accounts(registry, pool, "A", lambda _: {"error": {"type": failure}}, lambda *args: recorded.append(args))
    assert len(receipt["attempts"]) == 1
    assert receipt["common_failure"]
    assert not recorded
    assert not _browser_pool_unavailable(receipt, {})


def test_unknown_snapshot_and_zero_available_count_do_not_authorize_spend():
    for availability in ({}, {"healthy_count": 0}, {"observed_available_account_count": 0}, {"observed_available_account_count": 3}):
        assert not _browser_pool_unavailable(availability, {})


@pytest.mark.parametrize("subtype,attempt_count", [("security_verification", 3), ("environment_risk", 1)])
def test_detail_empty_body_uses_observed_account_or_global_boundary(subtype, attempt_count):
    registry, pool = fixture()
    _, receipt = run_browser_accounts(registry, pool, "A", lambda _: {
        "error": "empty", "failure_type": "empty_detail",
        "page_state": {"manual_action_required": True, "platform_state": "platform_verification_required", "failure_subtype": subtype},
    }, lambda *_: None, purpose="detail")
    assert len(receipt["attempts"]) == attempt_count
    assert receipt["common_failure"] is (attempt_count == 1)


def test_zero_risk_and_excluded_current_select_next_account():
    registry, pool = fixture()
    result = select_account("search", registry=registry, account_rows=pool["accounts"], reason_code="LOGIN_REQUIRED", excluded_profile_ids=["A"])
    assert result["recommended_profile_id"] == "B"
    assert result["switch_decision"]["allowed"]


def test_policy_limit_is_not_account_exhaustion():
    registry, pool = fixture()
    registry["raw"]["policy"]["max_switches_per_task"] = 1
    _, receipt = run_browser_accounts(registry, pool, "C", lambda _: {"error": {"type": "login_required"}}, lambda *_: None)
    assert len(receipt["attempts"]) == 2
    assert not _browser_pool_unavailable(receipt, {})


def test_cooldown_account_is_not_probed_and_is_explicit_in_receipt():
    registry, pool = fixture()
    pool["accounts"][1]["cooldown_active"] = True
    _, receipt = run_browser_accounts(registry, pool, "B", lambda _: {"error": {"type": "login_required"}}, lambda *_: None)
    assert [item["profile_id"] for item in receipt["attempts"]] == ["A", "C"]
    assert receipt["policy_excluded_profile_ids"] == ["B"]
    assert _browser_pool_unavailable(receipt, {})


def test_detail_wrapper_rebinds_entire_attempt_and_does_not_use_paid(monkeypatch):
    from contextlib import nullcontext
    from dataclasses import replace
    from detail_strategies.xiaohongshu import XiaohongshuDetailDeps, XiaohongshuDetailStrategy
    from kr_core import DetailRequest
    from collectors.platform import xiaohongshu as xhs
    from runtime import chrome_manager, profile_registry, xhs_account_pool, xhs_account_events
    from runtime.xhs_task_failover import CURRENT_XHS_PROFILE

    registry, pool = fixture()
    monkeypatch.setattr(profile_registry, "profile_registry_internal", lambda: registry)
    monkeypatch.setattr(xhs_account_pool, "xhs_account_pool_summary", lambda *_: pool)
    monkeypatch.setattr(xhs_account_events, "record_xhs_account_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(chrome_manager, "chrome_active_operation", lambda *_: nullcontext())
    ensured = []
    monkeypatch.setattr(xhs, "_ensure_chrome_debugging", lambda resource, **_: ensured.append(resource) or resource != "xhs:A")
    monkeypatch.setattr(xhs, "_chrome_debug_url", lambda resource: resource)
    monkeypatch.setattr(xhs, "xiaohongshu_account_state", lambda url: {"ok": True})
    monkeypatch.setattr(xhs, "_xhs_login_state_ok", lambda state: True)
    deps = XiaohongshuDetailDeps(
        bridge_path="unused", node_exe="unused", recover_xsec_token=lambda _: "",
        detail_needs_fallback=lambda _: False, extract_via_cdp=lambda *_: None,
        ocr_first_image=lambda *_: {}, attach_routing=lambda _, data: data,
        evidence_builder=lambda *_: None, log_info=lambda _: None, log_warning=lambda _: None, log_error=lambda _: None,
        selected_profile_id=lambda: "A",
    )
    strategy = XiaohongshuDetailStrategy(deps)
    selected = []
    strategy._extract_single = lambda *_args, **_kwargs: selected.append(CURRENT_XHS_PROFILE.get()) or {"title": "real detail"}
    strategy._try_tikhub_detail_fallback = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("paid must not run"))
    result = strategy._extract("https://www.xiaohongshu.com/explore/0123456789abcdef01234567", {}, request=DetailRequest(url="test"))
    assert ensured == ["xhs:A", "xhs:B"]
    assert selected == ["B"]
    assert result["metadata"]["account_failover"]["attempts"][1]["status"] == "ok"
    assert CURRENT_XHS_PROFILE.get() == ""
    assert replace(deps, allow_paid_fallback=False).allow_paid_fallback is False
