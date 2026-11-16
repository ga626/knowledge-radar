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
            "note_id": "fixture-note",
            "selectorTexts": {"#detail-title": ["标题"], "#detail-desc": ["正文内容" * 10]},
            "images": ["https://example.invalid/fallback.jpg"],
            "image_assets": [
                {"url": "https://img.example/content.jpg", "role": "content", "note_id": "fixture-note"},
                {"url": "https://img.example/avatar.jpg", "role": "noise"},
            ],
            "image_count": 2,
        }
    )
    assert result["images"] == ["https://img.example/content.jpg"]
    assert result["image_quality"] == {"content_count": 1, "unknown_count": 0, "noise_count": 1}


def test_xhs_unattributed_images_are_not_automatically_sent_to_ocr():
    from runtime.xhs_candidates import normalize_xhs_image_assets
    assets,images,quality=normalize_xhs_image_assets({
        'note_id':'fixture-note','image_assets':[
            {'url':'https://img.example/unknown.jpg','role':'content'},
            {'url':'https://img.example/foreign.jpg','role':'content','note_id':'another-note'}]})
    assert images==[]
    assert [row['role'] for row in assets]==['unknown','noise']
    assert quality=={'content_count':0,'unknown_count':1,'noise_count':1}
