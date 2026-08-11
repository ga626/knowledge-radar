from media_bundle import MediaBundle, MediaKind, MediaLocationType, MediaRef, TextItem, TextKind
from media_router import (
    ExecutionMode,
    InformationSourceMode,
    MediaActionPlan,
    MediaBudget,
    MediaOperation,
    StoragePolicy,
)


def test_video_bundle_and_native_video_plan_construct_without_download() -> None:
    bundle = MediaBundle.build(
        platform="bilibili",
        source_url="https://www.bilibili.com/video/BV1example",
        content_id="BV1example",
        title="Video example",
        texts=[
            TextItem("video title", kind=TextKind.TITLE, source="api"),
            TextItem("existing transcript", kind=TextKind.TRANSCRIPT, source="subtitle"),
        ],
        video_refs=[
            MediaRef(
                kind=MediaKind.VIDEO,
                uri="https://cdn.example.test/video.mp4",
                location_type=MediaLocationType.URL,
                duration_seconds=120,
                mime_type="video/mp4",
            )
        ],
    )
    plan = MediaActionPlan.from_bundle(
        bundle,
        source_mode=InformationSourceMode.NATIVE_MEDIA,
        target_kinds=(MediaKind.VIDEO,),
        operations=(MediaOperation.PASS_VIDEO_URL,),
        provider="siliconflow",
        model="qwen3-vl",
        execution_mode=ExecutionMode.ASYNC,
        storage_policy=StoragePolicy.NO_DOWNLOAD,
        budget=MediaBudget(max_frames=8, fps=1, max_input_tokens=12000),
        reasons=("direct video_url candidate is present",),
    )

    assert bundle.has_video
    assert bundle.video_refs[0].is_remote
    assert plan.source_mode == InformationSourceMode.NATIVE_MEDIA
    assert plan.requires_network
    assert not plan.allows_download
    assert plan.to_dict()["operations"] == ["pass_video_url"]


def test_image_bundle_and_sampled_media_plan_construct_without_download() -> None:
    bundle = MediaBundle.build(
        platform="xiaohongshu",
        source_url="https://www.xiaohongshu.com/explore/example",
        content_id="note-example",
        title="Image note",
        texts=[TextItem("note caption", kind=TextKind.DESCRIPTION, source="api")],
        image_refs=[
            MediaRef(
                kind=MediaKind.IMAGE,
                uri="https://img.example.test/1.jpg",
                location_type=MediaLocationType.URL,
                width=1280,
                height=720,
                mime_type="image/jpeg",
            )
        ],
    )
    plan = MediaActionPlan.from_bundle(
        bundle,
        source_mode=InformationSourceMode.SAMPLED_MEDIA_WITH_TEXT,
        target_kinds=(MediaKind.IMAGE,),
        operations=(MediaOperation.PASS_IMAGE_URL, MediaOperation.RUN_OCR),
        provider="siliconflow",
        model="paddleocr-vl",
        storage_policy=StoragePolicy.NO_DOWNLOAD,
        budget=MediaBudget(max_pixels=921600, max_input_tokens=5000),
        reasons=("image text may be important",),
    )

    assert bundle.has_images
    assert bundle.image_refs[0].width == 1280
    assert plan.source_mode == InformationSourceMode.SAMPLED_MEDIA_WITH_TEXT
    assert plan.requires_network
    assert not plan.allows_download
    assert plan.to_dict()["target_kinds"] == ["image"]


def test_audio_bundle_and_derived_text_plan_construct_without_download() -> None:
    bundle = MediaBundle.build(
        platform="youtube",
        source_url="https://www.youtube.com/watch?v=example",
        content_id="example",
        title="Audio example",
        texts=[TextItem("audio title", kind=TextKind.TITLE, source="api")],
        audio_refs=[
            MediaRef(
                kind=MediaKind.AUDIO,
                uri="yt:example:audio",
                location_type=MediaLocationType.PLATFORM_ID,
                duration_seconds=300,
            )
        ],
    )
    plan = MediaActionPlan.from_bundle(
        bundle,
        source_mode=InformationSourceMode.DERIVED_TEXT,
        target_kinds=(MediaKind.AUDIO,),
        operations=(MediaOperation.RUN_ASR,),
        provider="whisper",
        model="local-whisper",
        execution_mode=ExecutionMode.ASYNC,
        storage_policy=StoragePolicy.TEMPORARY_ONLY,
        budget=MediaBudget(max_duration_seconds=600, timeout_seconds=90),
        reasons=("speech is required to understand the item",),
    )

    assert bundle.has_audio
    assert bundle.audio_refs[0].location_type == MediaLocationType.PLATFORM_ID
    assert plan.source_mode == InformationSourceMode.DERIVED_TEXT
    assert not plan.requires_network
    assert plan.allows_download
    assert plan.to_dict()["storage_policy"] == "temporary_only"


def test_basic_text_plan_constructs_for_any_media_type() -> None:
    bundle = MediaBundle.build(
        platform="web",
        source_url="https://example.test/item",
        title="Text-only item",
        texts=[TextItem("description and comments are enough", kind=TextKind.DESCRIPTION)],
    )
    plan = MediaActionPlan.from_bundle(
        bundle,
        source_mode=InformationSourceMode.BASIC_TEXT,
        target_kinds=(MediaKind.TEXT,),
        operations=(MediaOperation.READ_BASIC_TEXT,),
        reasons=("basic text is enough",),
    )

    assert bundle.has_text
    assert plan.source_mode == InformationSourceMode.BASIC_TEXT
    assert not plan.requires_network
    assert not plan.allows_download
