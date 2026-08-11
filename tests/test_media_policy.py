from media_policy import MediaModelPolicy
from runtime.media_provider_preflight import classify_provider_error, preflight_contract
from understanding.siliconflow import configured_models


def test_media_model_policy_defaults(monkeypatch) -> None:
    for name in (
        "KR_BASIC_TEXT_MODELS",
        "KR_ASR_MODELS",
        "KR_OCR_MODELS",
        "KR_FRAME_VISION_MODELS",
        "KR_NATIVE_VIDEO_MODELS",
        "KR_NATIVE_AUDIO_VIDEO_MODELS",
        "KR_COMMENT_FILTER_MODELS",
    ):
        monkeypatch.delenv(name, raising=False)

    policy = MediaModelPolicy.from_env()

    assert policy.basic_text_models == ()
    assert policy.asr_models == ("local:faster-whisper/base", "local:faster-whisper/tiny")
    assert policy.ocr_models == ("PaddlePaddle/PaddleOCR-VL-1.5",)
    assert policy.frame_vision_models == ("bailian:qwen3-vl-flash",)
    assert policy.native_video_models == ("bailian:qwen3-vl-flash",)
    assert policy.native_audio_video_models == ("bailian:qwen3.5-omni-flash",)
    assert policy.comment_filter_models == ("bailian:qwen3.5-flash",)
    assert policy.native_auto_max_duration_seconds == 300
    assert policy.native_auto_max_frames == 16


def test_media_model_policy_reads_env(monkeypatch) -> None:
    monkeypatch.setenv("KR_FRAME_VISION_MODELS", "model-a, model-b")
    monkeypatch.setenv("KR_NATIVE_AUTO_MAX_DURATION_SECONDS", "120")
    monkeypatch.setenv("KR_NATIVE_AUTO_MAX_FRAMES", "4")

    policy = MediaModelPolicy.from_env()

    assert policy.frame_vision_models == ("model-a", "model-b")
    assert policy.native_auto_max_duration_seconds == 120
    assert policy.native_auto_max_frames == 4


def test_siliconflow_configured_models_use_new_names_and_old_fallback(monkeypatch) -> None:
    monkeypatch.setenv("KR_FRAME_VISION_MODELS", "new-frame")
    monkeypatch.setenv("KR_VIDEO_MODELS", "old-video")
    assert configured_models("video") == ["new-frame"]

    monkeypatch.delenv("KR_FRAME_VISION_MODELS", raising=False)
    assert configured_models("video") == ["old-video"]

    monkeypatch.setenv("KR_OCR_MODELS", "new-ocr")
    monkeypatch.setenv("KR_IMAGE_MODELS", "old-image")
    assert configured_models("image") == ["new-ocr"]


def test_siliconflow_configured_models_ignore_bailian_defaults(monkeypatch) -> None:
    monkeypatch.setenv("KR_FRAME_VISION_MODELS", "bailian:qwen3-vl-flash")
    monkeypatch.setenv("KR_VIDEO_MODELS", "legacy-video")

    assert configured_models("video") == ["legacy-video"]

    monkeypatch.delenv("KR_VIDEO_MODELS", raising=False)
    assert not any(model.startswith("bailian:") for model in configured_models("video"))


def test_media_registry_marks_new_video_models_as_canary_only() -> None:
    registry = MediaModelPolicy.from_env().capability_registry()
    assert registry["bailian:qwen3.7-flash"]["canary_required"] is True
    assert registry["bailian:qwen3.7-flash"]["blocked_default"] is True
    assert preflight_contract("bailian:qwen3.7-flash")["status"] == "canary_required"
    assert classify_provider_error("HTTP 403 Access denied")["failure_class"] == "AUTH_OR_PERMISSION"
