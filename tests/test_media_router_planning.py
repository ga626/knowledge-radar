from media_bundle import MediaBundle, MediaKind, MediaLocationType, MediaRef, TextItem, TextKind
from media_router import (
    InformationSourceMode,
    MediaOperation,
    StoragePolicy,
    WaitMode,
    plan_media_action,
)
from media_policy import MediaModelPolicy
from routing.models import ContentKind, ModalitySignals, RouteDecision


def _route(path: str, *, visual_score: float = 0.0, transcript_length: int = 0) -> RouteDecision:
    return RouteDecision(
        stage="test",
        content_kind=ContentKind.VIDEO,
        recommended_path=path,
        should_run_l2=path in {"recommend_l2_video", "need_more_probe"},
        signals=ModalitySignals(has_video=True, transcript_length=transcript_length),
        scores={"visual_dependency_score": visual_score},
        confidence=0.8,
        reason_codes=[f"PATH_{path.upper()}"],
    )


def test_video_l1_transcript_route_maps_to_derived_text() -> None:
    bundle = MediaBundle.build(
        platform="bilibili",
        source_url="https://www.bilibili.com/video/BV1example",
        title="Language-first video",
        texts=[TextItem("full transcript", kind=TextKind.TRANSCRIPT, source="subtitle")],
        video_refs=[MediaRef(kind=MediaKind.VIDEO, uri="https://cdn.example.test/video.mp4")],
    )
    plan = plan_media_action(bundle, _route("l1_transcript_enough", transcript_length=1200))

    assert plan.source_mode == InformationSourceMode.DERIVED_TEXT
    assert plan.operations == (MediaOperation.READ_TRANSCRIPT,)
    assert plan.storage_policy == StoragePolicy.NO_DOWNLOAD
    assert plan.wait_mode == WaitMode.NONE
    assert not plan.allows_download


def test_video_visual_route_maps_to_sampled_media_without_download_by_default() -> None:
    bundle = MediaBundle.build(
        platform="bilibili",
        source_url="https://www.bilibili.com/video/BV1visual",
        title="PPT and chart tutorial",
        texts=[TextItem("partial transcript", kind=TextKind.TRANSCRIPT, source="subtitle")],
        video_refs=[MediaRef(kind=MediaKind.VIDEO, uri="https://cdn.example.test/visual.mp4")],
    )
    plan = plan_media_action(bundle, _route("recommend_l2_video", visual_score=8.0))

    assert plan.source_mode == InformationSourceMode.SAMPLED_MEDIA_WITH_TEXT
    assert plan.operations == (MediaOperation.READ_TRANSCRIPT, MediaOperation.SAMPLE_FRAMES)
    assert plan.storage_policy == StoragePolicy.NO_DOWNLOAD
    assert plan.budget.max_frames == 8
    assert not plan.allows_download


def test_video_native_preference_maps_to_video_url_plan() -> None:
    bundle = MediaBundle.build(
        platform="bilibili",
        source_url="https://www.bilibili.com/video/BV1native",
        title="Native video candidate",
        video_refs=[
            MediaRef(
                kind=MediaKind.VIDEO,
                uri="https://cdn.example.test/native.mp4",
                location_type=MediaLocationType.URL,
                duration_seconds=120,
            )
        ],
    )
    plan = plan_media_action(bundle, _route("recommend_l2_video"), prefer_native_media=True, policy=MediaModelPolicy())

    assert plan.source_mode == InformationSourceMode.NATIVE_MEDIA
    assert plan.operations == (MediaOperation.PASS_VIDEO_URL,)
    assert plan.storage_policy == StoragePolicy.NO_DOWNLOAD
    assert plan.wait_mode == WaitMode.CLIENT_BUDGET
    assert plan.provider == "bailian"
    assert plan.model == "bailian:qwen3-vl-flash"
    assert plan.budget.max_duration_seconds == 300
    assert plan.budget.max_frames == 16
    assert plan.requires_network


def test_bundle_with_image_and_video_prefers_video_route() -> None:
    bundle = MediaBundle.build(
        platform="bilibili",
        source_url="https://www.bilibili.com/video/BV1mixed",
        title="Mixed media video",
        image_refs=[MediaRef(kind=MediaKind.IMAGE, uri="https://img.example.test/cover.jpg")],
        video_refs=[MediaRef(kind=MediaKind.VIDEO, uri="https://cdn.example.test/mixed.mp4")],
    )
    plan = plan_media_action(bundle, _route("recommend_l2_video"), prefer_native_media=True, policy=MediaModelPolicy())

    assert plan.source_mode == InformationSourceMode.NATIVE_MEDIA
    assert plan.target_kinds == (MediaKind.VIDEO,)
    assert plan.operations == (MediaOperation.PASS_VIDEO_URL,)
    assert MediaOperation.RUN_OCR not in plan.operations


def test_bilibili_provider_blocked_direct_url_falls_back_to_sampled_media() -> None:
    bundle = MediaBundle.build(
        platform="bilibili",
        source_url="https://www.bilibili.com/video/BV1blocked",
        title="Blocked CDN candidate",
        video_refs=[
            MediaRef(
                kind=MediaKind.VIDEO,
                uri="https://upos-sz.example.test/video.m4s",
                location_type=MediaLocationType.URL,
                duration_seconds=120,
            )
        ],
    )
    direct_probe = {
        "status": "ok",
        "reachability": {"status": "reachable"},
        "provider_downloadability": {
            "status": "provider_blocked",
            "allow_native_media": False,
            "reason": "Bilibili raw CDN URLs are provider-blocked",
        },
    }

    plan = plan_media_action(
        bundle,
        _route("recommend_l2_video"),
        prefer_native_media=True,
        direct_media_probe=direct_probe,
    )

    assert plan.source_mode == InformationSourceMode.SAMPLED_MEDIA_WITH_TEXT
    assert plan.operations == (MediaOperation.RUN_ASR, MediaOperation.SAMPLE_FRAMES)
    assert plan.metadata["direct_media_probe"]["provider_downloadability_status"] == "provider_blocked"
    assert "provider-blocked" in plan.metadata["native_media_fallback_reason"]


def test_provider_downloadable_direct_url_can_use_native_media() -> None:
    bundle = MediaBundle.build(
        platform="web",
        source_url="https://example.test/video",
        title="Provider downloadable candidate",
        video_refs=[
            MediaRef(
                kind=MediaKind.VIDEO,
                uri="https://media.w3.org/2010/05/sintel/trailer.mp4",
                location_type=MediaLocationType.URL,
                duration_seconds=120,
            )
        ],
    )
    direct_probe = {
        "status": "ok",
        "reachability": {"status": "reachable"},
        "provider_downloadability": {
            "status": "provider_downloadable",
            "allow_native_media": True,
        },
    }

    plan = plan_media_action(
        bundle,
        _route("recommend_l2_video"),
        prefer_native_media=True,
        direct_media_probe=direct_probe,
    )

    assert plan.source_mode == InformationSourceMode.NATIVE_MEDIA
    assert plan.operations == (MediaOperation.PASS_VIDEO_URL,)
    assert plan.metadata["direct_media_probe"]["allow_native_media"] is True


def test_long_video_native_preference_falls_back_to_sampled_media() -> None:
    bundle = MediaBundle.build(
        platform="bilibili",
        source_url="https://www.bilibili.com/video/BV1long",
        title="Long visual video",
        video_refs=[
            MediaRef(
                kind=MediaKind.VIDEO,
                uri="https://cdn.example.test/long.mp4",
                location_type=MediaLocationType.URL,
                duration_seconds=901,
            )
        ],
    )
    plan = plan_media_action(bundle, _route("recommend_l2_video"), prefer_native_media=True, policy=MediaModelPolicy())

    assert plan.source_mode == InformationSourceMode.SAMPLED_MEDIA_WITH_TEXT
    assert plan.operations == (MediaOperation.RUN_ASR, MediaOperation.SAMPLE_FRAMES)
    assert plan.provider == "bailian"
    assert plan.model == "bailian:qwen3-vl-flash"


def test_image_bundle_maps_to_sampled_media_with_text() -> None:
    bundle = MediaBundle.build(
        platform="xiaohongshu",
        source_url="https://www.xiaohongshu.com/explore/example",
        title="Image note",
        texts=[TextItem("caption", kind=TextKind.DESCRIPTION)],
        image_refs=[MediaRef(kind=MediaKind.IMAGE, uri="https://img.example.test/note.jpg")],
    )
    plan = plan_media_action(bundle)

    assert plan.source_mode == InformationSourceMode.SAMPLED_MEDIA_WITH_TEXT
    assert plan.operations == (MediaOperation.PASS_IMAGE_URL, MediaOperation.RUN_OCR)
    assert plan.storage_policy == StoragePolicy.NO_DOWNLOAD
    assert not plan.allows_download


def test_audio_bundle_maps_to_asr_when_transcript_missing() -> None:
    bundle = MediaBundle.build(
        platform="youtube",
        source_url="https://www.youtube.com/watch?v=example",
        title="Audio only",
        audio_refs=[MediaRef(kind=MediaKind.AUDIO, uri="yt:example:audio", location_type=MediaLocationType.PLATFORM_ID)],
    )
    plan = plan_media_action(bundle, allow_download=True)

    assert plan.source_mode == InformationSourceMode.DERIVED_TEXT
    assert plan.operations == (MediaOperation.RUN_ASR,)
    assert plan.storage_policy == StoragePolicy.TEMPORARY_ONLY
    assert plan.allows_download
