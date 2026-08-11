from __future__ import annotations

from kr_core import DetailRequest, EvidenceItem
from detail_strategies import xiaohongshu as xhs_detail_mod
from detail_strategies.xiaohongshu import XiaohongshuDetailDeps, XiaohongshuDetailStrategy
from runtime.xhs_detail_selection import read_xhs_details_with_replacement, select_xhs_detail_targets
from runtime import xhs_health
from runtime.xhs_page_state import classify_xhs_page_state
from runtime.xhs_account_pool import xhs_account_pool_summary
from collectors.platform import xiaohongshu as xhs_collector


def _evidence(url: str, platform: str, data: dict) -> EvidenceItem:
    return EvidenceItem(source_url=url, source_platform=platform, summary=str(data.get("title") or ""))


def _strategy(note_data: dict, ocr_calls: list[list[str]]) -> XiaohongshuDetailStrategy:
    strategy = XiaohongshuDetailStrategy(
        XiaohongshuDetailDeps(
            bridge_path="bridge.js",
            node_exe="node",
            recover_xsec_token=lambda note_id: "",
            detail_needs_fallback=xhs_collector.detail_needs_fallback,
            extract_via_cdp=lambda note_id, xsec_token, xsec_source: None,
            ocr_first_image=lambda images, **kwargs: ocr_calls.append(list(images)) or {"status": "ok", "text": "图片文字"},
            attach_routing=lambda url, result: result,
            evidence_builder=_evidence,
            log_info=lambda message: None,
            log_warning=lambda message: None,
            log_error=lambda message: None,
        )
    )
    strategy._call_bridge = lambda note_id, xsec_token, xsec_source: {"status": "ok", "noteData": note_data}  # type: ignore[method-assign]
    return strategy


def test_xhs_account_pool_filters_non_xhs_accounts() -> None:
    registry = {
        "raw": {
            "accounts": [
                {"platform": "liepin", "account_slot": "liepin-personal-9338", "priority": 1, "risk_score": 1},
                {"platform": "xiaohongshu", "account_slot": "xhs-primary", "priority": 2, "risk_score": 5},
            ],
            "bindings": [
                {"platform": "liepin", "account_slot": "liepin-personal-9338", "profile_id": "liepin-profile"},
                {"platform": "xiaohongshu", "account_slot": "xhs-primary", "profile_id": "xhs-profile"},
            ],
            "policy": {"default_mode": "manual"},
        },
        "profiles": [
            {"platform": "liepin", "account_slot": "liepin-personal-9338", "profile_id": "liepin-profile"},
            {"platform": "xiaohongshu", "account_slot": "xhs-primary", "profile_id": "xhs-profile"},
        ],
        "runtime_state": {"profiles": []},
    }

    summary = xhs_account_pool_summary(registry)

    assert summary["counts"]["accounts"] == 1
    assert summary["accounts"][0]["account_slot"] == "xhs-primary"
    assert summary["recommendations"]["search"]["recommended_account_slot"] == "xhs-primary"
    assert "liepin-personal-9338" not in str(summary)


def test_xhs_image_only_detail_is_not_empty_fallback() -> None:
    assert not xhs_collector.detail_needs_fallback({"images": ["https://sns-img.example/xhs.jpg"]})
    assert xhs_collector.detail_needs_fallback({})
    assert xhs_collector.detail_needs_fallback({"title": "页面不见了", "content": ""})


def test_xhs_empty_detail_does_not_request_manual_login(monkeypatch, tmp_path) -> None:
    health_path = tmp_path / "xhs-health.jsonl"
    tracker = xhs_health.XhsDetailHealthTracker(str(health_path))
    monkeypatch.setattr(xhs_detail_mod, "get_xhs_detail_health_tracker", lambda: tracker)
    login_calls: list[tuple[str, str, dict]] = []
    strategy = XiaohongshuDetailStrategy(
        XiaohongshuDetailDeps(
            bridge_path="bridge.js",
            node_exe="node",
            recover_xsec_token=lambda note_id: "",
            detail_needs_fallback=xhs_collector.detail_needs_fallback,
            extract_via_cdp=lambda note_id, xsec_token, xsec_source: None,
            ocr_first_image=lambda images, **kwargs: {"status": "ok", "text": ""},
            attach_routing=lambda url, result: result,
            evidence_builder=_evidence,
            log_info=lambda message: None,
            log_warning=lambda message: None,
            log_error=lambda message: None,
            request_user_login=lambda platform, reason, **kwargs: login_calls.append((platform, reason, kwargs)) or {"status": "waiting"},
            selected_profile_id=lambda: "xhs-a",
            allow_auto_user_login_request=True,
        )
    )
    strategy._call_bridge = lambda note_id, xsec_token, xsec_source: {"status": "ok", "noteData": {}}  # type: ignore[method-assign]

    response = strategy.extract(DetailRequest(url="https://www.xiaohongshu.com/explore/0123456789abcdef01234567"))

    assert response.data["failure_type"] == "empty_detail"
    assert response.data["failure_subtype"] == "xsec_missing"
    assert response.data["user_message"]
    assert response.data["diagnostics"]["failure_subtype"] == "xsec_missing"
    assert response.data["manual_action_required"] is False
    assert response.data["platform_state"] == "ok"
    assert login_calls == []


def test_xhs_bridge_error_attempts_cdp_fallback(monkeypatch, tmp_path) -> None:
    health_path = tmp_path / "xhs-health.jsonl"
    tracker = xhs_health.XhsDetailHealthTracker(str(health_path))
    monkeypatch.setattr(xhs_detail_mod, "get_xhs_detail_health_tracker", lambda: tracker)
    cdp_calls: list[tuple[str, str, str]] = []
    strategy = XiaohongshuDetailStrategy(
        XiaohongshuDetailDeps(
            bridge_path="bridge.js",
            node_exe="node",
            recover_xsec_token=lambda note_id: "",
            detail_needs_fallback=xhs_collector.detail_needs_fallback,
            extract_via_cdp=lambda note_id, xsec_token, xsec_source: cdp_calls.append((note_id, xsec_token, xsec_source)) or {
                "title": "CDP 标题",
                "content": "CDP 正文",
            },
            ocr_first_image=lambda images, **kwargs: {"status": "ok", "text": ""},
            attach_routing=lambda url, result: result,
            evidence_builder=_evidence,
            log_info=lambda message: None,
            log_warning=lambda message: None,
            log_error=lambda message: None,
        )
    )
    strategy._call_bridge = lambda note_id, xsec_token, xsec_source: {"status": "error", "error": "empty_detail", "failure_type": "empty_detail"}  # type: ignore[method-assign]

    response = strategy.extract(DetailRequest(url="https://www.xiaohongshu.com/explore/0123456789abcdef01234567"))

    assert response.data["title"] == "CDP 标题"
    assert response.data["content"] == "CDP 正文"
    assert cdp_calls == [("0123456789abcdef01234567", "", "pc_search")]


def test_xhs_empty_cdp_fallback_preserves_snapshot_diagnostics(monkeypatch, tmp_path) -> None:
    health_path = tmp_path / "xhs-health.jsonl"
    tracker = xhs_health.XhsDetailHealthTracker(str(health_path))
    monkeypatch.setattr(xhs_detail_mod, "get_xhs_detail_health_tracker", lambda: tracker)
    strategy = XiaohongshuDetailStrategy(
        XiaohongshuDetailDeps(
            bridge_path="bridge.js",
            node_exe="node",
            recover_xsec_token=lambda note_id: "",
            detail_needs_fallback=xhs_collector.detail_needs_fallback,
            extract_via_cdp=lambda note_id, xsec_token, xsec_source: {
                "title": "",
                "content": "",
                "snapshot_status": "empty",
                "text_len": 0,
                "textSample": "",
                "url": "https://www.xiaohongshu.com/explore/0123456789abcdef01234567",
                "selector_keys": [],
            },
            ocr_first_image=lambda images, **kwargs: {"status": "ok", "text": ""},
            attach_routing=lambda url, result: result,
            evidence_builder=_evidence,
            log_info=lambda message: None,
            log_warning=lambda message: None,
            log_error=lambda message: None,
        )
    )
    strategy._call_bridge = lambda note_id, xsec_token, xsec_source: {"status": "ok", "noteData": {}}  # type: ignore[method-assign]

    response = strategy.extract(DetailRequest(url="https://www.xiaohongshu.com/explore/0123456789abcdef01234567"))

    attempt = response.data["fallback_attempts"][0]
    assert attempt["status"] == "empty"
    assert attempt["snapshot_status"] == "empty"
    assert attempt["text_len"] == 0
    assert attempt["selector_hit_count"] == 0
    assert response.data["failure_subtype"] == "xsec_missing"
    assert response.data["diagnostics"]["fallback_attempts"][0]["failure_subtype"] == "selector_miss"
    assert attempt["page_url"].endswith("/0123456789abcdef01234567")


def test_xhs_security_detail_requests_manual_login(monkeypatch, tmp_path) -> None:
    health_path = tmp_path / "xhs-health.jsonl"
    tracker = xhs_health.XhsDetailHealthTracker(str(health_path))
    monkeypatch.setattr(xhs_detail_mod, "get_xhs_detail_health_tracker", lambda: tracker)
    login_calls: list[tuple[str, str, dict]] = []
    note_data = {"title": "安全验证", "content": "请完成验证后继续访问"}
    strategy = XiaohongshuDetailStrategy(
        XiaohongshuDetailDeps(
            bridge_path="bridge.js",
            node_exe="node",
            recover_xsec_token=lambda note_id: "",
            detail_needs_fallback=lambda data: True,
            extract_via_cdp=lambda note_id, xsec_token, xsec_source: None,
            ocr_first_image=lambda images, **kwargs: {"status": "ok", "text": ""},
            attach_routing=lambda url, result: result,
            evidence_builder=_evidence,
            log_info=lambda message: None,
            log_warning=lambda message: None,
            log_error=lambda message: None,
            request_user_login=lambda platform, reason, **kwargs: login_calls.append((platform, reason, kwargs)) or {"status": "waiting"},
            selected_profile_id=lambda: "xhs-a",
            allow_auto_user_login_request=True,
        )
    )
    strategy._call_bridge = lambda note_id, xsec_token, xsec_source: {"status": "ok", "noteData": note_data}  # type: ignore[method-assign]

    response = strategy.extract(DetailRequest(url="https://www.xiaohongshu.com/explore/0123456789abcdef01234567"))

    assert response.data["manual_action_required"] is True
    assert response.data["platform_state"] == "platform_verification_required"
    assert login_calls == [("xhs", "platform_verification_required", {
        "target_profile_id": "xhs-a",
        "trigger_evidence": ["xhs_detail_page_state=platform_verification_required"],
        "source": "get_content_detail.empty_detail",
    })]


def test_xhs_ocr_runs_for_long_text_when_images_present(monkeypatch, tmp_path) -> None:
    health_path = tmp_path / "xhs-health.jsonl"
    tracker = xhs_health.XhsDetailHealthTracker(str(health_path))
    monkeypatch.setattr(xhs_detail_mod, "get_xhs_detail_health_tracker", lambda: tracker)
    monkeypatch.setenv("KR_XHS_OCR_TRIGGER_POLICY", "image_presence")
    ocr_calls: list[list[str]] = []
    note_data = {
        "title": "图文笔记",
        "content": "这是一段已经很长的正文。" * 20,
        "images": ["https://sns-img.example/xhs-one.jpg"],
    }
    strategy = _strategy(note_data, ocr_calls)

    response = strategy.extract(
        DetailRequest(
            url="https://www.xiaohongshu.com/explore/0123456789abcdef01234567",
            auto_multimodal=True,
        )
    )

    assert response.data["ocr"]["status"] == "ok"
    assert response.data["ocr_decision"]["enabled"] is True
    assert response.data["ocr_decision"]["reason"] == "images_present"
    assert ocr_calls == [["https://sns-img.example/xhs-one.jpg"]]


def test_xhs_observed_short_detail_is_not_returned_as_success() -> None:
    strategy = _strategy({"title": "短文", "content": "很短", "selector_hit_count": 1, "text_len": 2}, [])
    response = strategy.extract(DetailRequest(url="https://www.xiaohongshu.com/explore/0123456789abcdef01234567"))
    assert response.data["failure_type"] == "empty_detail"
    assert response.data["detail_quality"]["status"] == "EXPECTED_DEGRADED"


def test_xhs_health_ignores_legacy_success_rate_samples(tmp_path) -> None:
    path = tmp_path / "xhs-health.jsonl"
    path.write_text(
        '{"success":false,"elapsed_s":1.0,"error_type":"empty_detail"}\n',
        encoding="utf-8",
    )
    tracker = xhs_health.XhsDetailHealthTracker(str(path))

    before = tracker.summary()
    tracker.record(success=True, elapsed_s=2.0, note_id="0123456789abcdef01234567")
    after = tracker.summary()

    assert before["total"] == 0
    assert before["legacy_ignored_count"] == 1
    assert after["total"] == 1
    assert after["success_rate"] == 1.0
    assert after["legacy_ignored_count"] == 1


def test_xhs_selector_zero_hit_alert_threshold(tmp_path) -> None:
    path = tmp_path / "xhs-health.jsonl"
    tracker = xhs_health.XhsDetailHealthTracker(str(path))
    for idx in range(5):
        tracker.record(
            success=False,
            elapsed_s=1.0,
            error_type="empty_detail",
            failure_subtype="selector_miss",
            note_id=f"note{idx}",
            url=f"https://www.xiaohongshu.com/explore/{idx:024d}",
            page_state={"platform_state": "ok", "manual_action_required": False},
            selector_hit_count=0 if idx in {0, 2, 4} else 2,
            text_len=0,
        )

    summary = tracker.summary(recent_limit=5)

    assert summary["selector_contract_alert"]["active"] is True
    assert summary["selector_contract_alert"]["selector_zero_hit_count"] == 3
    assert summary["selector_contract_alert"]["scheduled_patrol"] is False


def test_xhs_detail_selection_top2_plus_one_replacement() -> None:
    search_result = {
        "items": [
            {"url": "https://www.xiaohongshu.com/explore/000000000000000000000001", "note_id": "1", "title": "一"},
            {"url": "https://www.xiaohongshu.com/explore/000000000000000000000002", "note_id": "2", "title": "二"},
            {"url": "https://www.xiaohongshu.com/explore/000000000000000000000003", "note_id": "3", "title": "三"},
            {"url": "https://www.xiaohongshu.com/explore/000000000000000000000004", "note_id": "4", "title": "四"},
        ]
    }

    selected = select_xhs_detail_targets(search_result)
    assert [item["role"] for item in selected] == ["primary", "primary", "replacement"]

    calls: list[str] = []

    def reader(url: str) -> dict:
        calls.append(url)
        if url.endswith("000000000000000000000001"):
            return {"error": "empty", "failure_type": "empty_detail"}
        return {"title": url[-1], "content": "ok"}

    result = read_xhs_details_with_replacement(search_result, reader)

    assert len(result["details"]) == 2
    assert len(result["attempts"]) == 3
    assert len(calls) == 3


def test_xhs_detail_selection_env_override(monkeypatch) -> None:
    monkeypatch.setenv("KR_XHS_DETAIL_TOP_K", "1")
    monkeypatch.setenv("KR_XHS_DETAIL_RETRY_REPLACEMENTS", "2")
    search_result = {
        "items": [
            {"url": f"https://www.xiaohongshu.com/explore/{idx:024d}", "note_id": str(idx), "title": str(idx)}
            for idx in range(4)
        ]
    }

    selected = select_xhs_detail_targets(search_result)

    assert [item["role"] for item in selected] == ["primary", "replacement", "replacement"]


def test_xhs_page_state_uses_multi_signal_antibot_classification() -> None:
    captcha = classify_xhs_page_state(
        "",
        url="https://www.xiaohongshu.com/explore/0123456789abcdef01234567",
        selector_hit_count=0,
        body_text_len=20,
        captcha_element_count=1,
        loading_state="complete",
    )
    assert captcha["platform_state"] == "platform_verification_required"
    assert captcha["failure_subtype"] == "captcha_element_detected"
    assert captcha["safe_to_switch_account"] is True

    rate_limited = classify_xhs_page_state("正常标题", http_status=429)
    assert rate_limited["platform_state"] == "platform_verification_required"
    assert rate_limited["failure_subtype"] == "http_429"

    selector_miss = classify_xhs_page_state(
        "",
        selector_hit_count=0,
        body_text_len=0,
        loading_state="complete",
    )
    assert selector_miss["platform_state"] == "empty_detail"
    assert selector_miss["failure_subtype"] == "selector_miss"
