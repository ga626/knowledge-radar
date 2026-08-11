from kr_core.affordance import attach_result_affordance
from kr_core.collection import format_search_response
from kr_core.models import SearchResultItem
from collectors.platform import youtube


def test_video_search_item_exposes_detail_affordance() -> None:
    item = SearchResultItem(
        title="Demo",
        url="https://www.bilibili.com/video/BV1example",
        platform="B站",
        content_type="video",
    ).to_mcp_dict()

    assert item["affordance_schema"] == "knowledgeradar-result-affordance/v1"
    assert item["detail_supported"] is True
    assert item["detail_tool"] == "get_content_detail"
    assert "video" in item["content_modalities"]
    assert "transcript" in item["detail_capabilities"]
    assert "video_analysis" in item["detail_capabilities"]
    assert "auto_multimodal" in item["expensive_capabilities"]
    assert item["detail_wait_policy"]["wait_tool"] == "get_task_status"


def test_xiaohongshu_search_item_exposes_image_ocr_affordance() -> None:
    item = attach_result_affordance(
        "小红书",
        {
            "title": "note",
            "url": "https://www.xiaohongshu.com/explore/abc",
            "metadata": {"type": "image"},
        },
    )

    assert item["detail_supported"] is True
    assert "image" in item["content_modalities"]
    assert "image_ocr" in item["detail_capabilities"]
    assert "enable_deep_analysis" in item["expensive_capabilities"]


def test_format_search_response_attaches_affordance_to_legacy_items() -> None:
    response = format_search_response(
        "web",
        [{"title": "Demo", "url": "https://example.com/article"}],
    )

    item = response["items"][0]
    assert item["detail_supported"] is False
    assert item["detail_tool"] == ""
    assert item["recommended_extract_tool"] == "extract_web_page"
    assert item["detail_unavailable_reason"] == "generic_web_use_extract_web_page"
    assert item["detail_wait_policy"]["result_reread_tool"] == ""


def test_supported_video_url_keeps_platform_detail_affordance() -> None:
    response = format_search_response(
        "YouTube",
        [{"title": "Demo", "url": "https://www.youtube.com/watch?v=abc12345678", "content_type": "video"}],
    )

    item = response["items"][0]
    assert item["detail_supported"] is True
    assert item["detail_tool"] == "get_content_detail"
    assert "video_analysis" in item["detail_capabilities"]
    assert item["source_ecology"] == "youtube_video_ecology"
    assert item["evidence_role"] == "media_context_candidate"


def test_wechat_article_result_exposes_source_ecology_without_detail_claim() -> None:
    item = attach_result_affordance(
        "微信公众号",
        {"title": "article", "url": "https://mp.weixin.qq.com/s/example"},
    )

    assert item["detail_supported"] is False
    assert item["recommended_extract_tool"] == "extract_web_page"
    assert item["source_ecology"] == "wechat_public_article_ecology"
    assert item["evidence_role"] == "public_article_candidate"
    assert item["evidence_strength"] == "candidate_until_detail_or_cross_checked"
    assert "verify_account_or_source" in item["recommended_verification"]


def test_metadata_affordance_cleanup_removes_evidence_fields() -> None:
    item = attach_result_affordance(
        "web",
        {
            "title": "page",
            "url": "https://example.com/post",
            "metadata": {
                "source_ecology": "old",
                "evidence_role": "old",
                "recommended_verification": ["old"],
            },
        },
    )

    assert item["source_ecology"] == "generic_web_ecology"
    assert "source_ecology" not in item["metadata"]
    assert "evidence_role" not in item["metadata"]


def test_format_search_response_does_not_duplicate_affordance_in_metadata() -> None:
    response = format_search_response(
        "小红书",
        [
            {
                "title": "note",
                "url": "https://www.xiaohongshu.com/explore/abc",
                "metadata": {
                    "type": "image",
                    "affordance_schema": "old",
                    "detail_supported": True,
                    "content_modalities": ["image"],
                },
            }
        ],
    )

    item = response["items"][0]
    assert item["affordance_schema"] == "knowledgeradar-result-affordance/v1"
    assert "affordance_schema" not in item["metadata"]
    assert "detail_supported" not in item["metadata"]
    assert "recommended_extract_tool" not in item["metadata"]


def test_non_url_item_marks_detail_unavailable() -> None:
    item = attach_result_affordance("B站", {"title": "broken", "url": ""})

    assert item["detail_supported"] is False
    assert item["detail_tool"] == ""
    assert item["detail_unavailable_reason"] == "missing_http_url"


def test_youtube_native_search_attaches_affordance(monkeypatch) -> None:
    monkeypatch.setattr(youtube, "youtube_api_key", lambda: "key")
    monkeypatch.setattr(youtube, "_youtube_operation_timeout_s", lambda: 1.0)
    monkeypatch.setattr(
        youtube,
        "_run_with_timeout",
        lambda fn, timeout_s: {
            "items": [
                {
                    "id": {"videoId": "abc12345678"},
                    "snippet": {
                        "title": "Demo",
                        "channelTitle": "Channel",
                        "description": "desc",
                        "publishedAt": "2026-01-01T00:00:00Z",
                        "channelId": "channel",
                        "thumbnails": {},
                    },
                }
            ]
        },
    )

    result = youtube.search_youtube("demo", limit=1)

    item = result["items"][0]
    assert item["detail_supported"] is True
    assert item["detail_tool"] == "get_content_detail"
    assert "video_analysis" in item["detail_capabilities"]
