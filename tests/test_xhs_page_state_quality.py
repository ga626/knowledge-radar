from runtime.xhs_page_state import classify_xhs_page_state
from runtime.xhs_candidates import normalize_xhs_detail_snapshot


def test_short_xhs_detail_is_degraded_not_success():
    result = classify_xhs_page_state(
        "有效正文很短",
        selector_hit_count=1,
        body_text_len=12,
        loading_state="complete",
        account_hint=True,
    )
    assert result["platform_state"] == "ok"
    assert result["detail_quality"]["status"] == "EXPECTED_DEGRADED"
    assert result["detail_quality"]["body_text_chars"] == 12


def test_selector_miss_has_explicit_failure_subtype():
    result = classify_xhs_page_state("", selector_hit_count=0, body_text_len=0, loading_state="complete")
    assert result["failure_subtype"] == "selector_miss"


def test_xhs_snapshot_keeps_content_images_and_excludes_dom_noise():
    result = normalize_xhs_detail_snapshot(
        {
            "selectorTexts": {"#detail-title": ["标题"], "#detail-desc": ["正文内容" * 10]},
            "images": ["https://example.invalid/fallback.jpg"],
            "image_assets": [
                {"url": "https://img.example/content.jpg", "role": "content"},
                {"url": "https://img.example/avatar.jpg", "role": "noise"},
            ],
            "image_count": 2,
        }
    )
    assert result["images"] == ["https://img.example/content.jpg"]
    assert result["image_quality"] == {"content_count": 1, "unknown_count": 0, "noise_count": 1}
