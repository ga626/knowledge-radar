from runtime.xhs_detail_selection import read_xhs_details_with_replacement, select_xhs_detail_targets
from runtime import xhs_health
from runtime.xhs_page_state import classify_xhs_page_state

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
