from media_policy import MediaModelPolicy
from runtime.media_provider_preflight import classify_provider_error


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
    assert policy.asr_models == ("local:qwen3-asr/0.6b-hf",)
    assert policy.ocr_models == ("paddleocr-vl-1.6",)
    assert policy.frame_vision_models == ("bailian:qwen3.8-omni-flash",)
    assert policy.native_video_models == ("bailian:qwen3.8-omni-flash",)
    assert policy.native_audio_video_models == ("bailian:qwen3.8-omni-flash",)
    assert policy.comment_filter_models == ()
    assert policy.native_auto_max_duration_seconds == 300
    assert policy.native_auto_max_frames == 16
    entry = policy.capability_registry()["bailian:qwen3.8-omni-flash"]
    assert entry["capabilities"] == ["frame_vision", "native_video", "native_audio_video"]
    assert entry["input_modalities"] == ["image", "video", "audio+video"]


def test_media_model_policy_reads_env(monkeypatch) -> None:
    monkeypatch.setenv("KR_FRAME_VISION_MODELS", "model-a, model-b")
    monkeypatch.setenv("KR_NATIVE_AUTO_MAX_DURATION_SECONDS", "120")
    monkeypatch.setenv("KR_NATIVE_AUTO_MAX_FRAMES", "4")

    policy = MediaModelPolicy.from_env()

    assert policy.frame_vision_models == ("bailian:qwen3.8-omni-flash",)
    assert policy.native_auto_max_duration_seconds == 120
    assert policy.native_auto_max_frames == 4


def test_retired_models_do_not_reenter_local_roles(monkeypatch):
    monkeypatch.setenv('KR_ASR_MODELS', 'local:faster-whisper/base')
    monkeypatch.setenv('KR_OCR_MODELS', 'PaddlePaddle/PaddleOCR-VL-1.5')
    policy = MediaModelPolicy.from_env()
    assert policy.asr_models == ('local:qwen3-asr/0.6b-hf',)
    assert policy.ocr_models == ('paddleocr-vl-1.6',)
    assert 'bailian:qwen3.7-flash' not in policy.capability_registry()
    assert classify_provider_error('HTTP 403 Access denied')['failure_class'] == 'AUTH_OR_PERMISSION'
