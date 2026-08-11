import pytest

from understanding import siliconflow


def test_call_video_url_model_builds_openai_compatible_video_payload(monkeypatch) -> None:
    captured = {}

    monkeypatch.setenv("SILICONFLOW_API_KEY", "test-key")

    def fake_post(model, messages, *, temperature, max_tokens, timeout):
        captured["model"] = model
        captured["messages"] = messages
        captured["temperature"] = temperature
        captured["max_tokens"] = max_tokens
        captured["timeout"] = timeout
        return "ok", {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}

    monkeypatch.setattr(siliconflow, "_post_chat_completion", fake_post)
    monkeypatch.setattr(siliconflow.get_usage_tracker(), "record", lambda **kwargs: kwargs)

    content, provider, usage = siliconflow.call_video_url_model(
        "https://cdn.example.test/video.mp4",
        models=["Qwen/Qwen3-VL-30B-A3B-Instruct"],
        prompt="describe",
        max_frames=8,
        fps=1,
        max_pixels=1280 * 720,
    )

    assert content == "ok"
    assert provider == "SiliconFlow Qwen/Qwen3-VL-30B-A3B-Instruct"
    assert usage["total_tokens"] == 14
    parts = captured["messages"][1]["content"]
    assert parts[1]["type"] == "video_url"
    assert parts[1]["video_url"]["url"] == "https://cdn.example.test/video.mp4"
    assert parts[1]["video_url"]["num_frames"] == 8
    assert parts[1]["video_url"]["fps"] == 1.0


def test_call_audio_url_model_builds_openai_compatible_audio_payload(monkeypatch) -> None:
    captured = {}

    monkeypatch.setenv("SILICONFLOW_API_KEY", "test-key")

    def fake_post(model, messages, *, temperature, max_tokens, timeout):
        captured["messages"] = messages
        return "audio ok", {}

    monkeypatch.setattr(siliconflow, "_post_chat_completion", fake_post)
    monkeypatch.setattr(siliconflow.get_usage_tracker(), "record", lambda **kwargs: kwargs)

    siliconflow.call_audio_url_model(
        "https://cdn.example.test/audio.mp3",
        models=["Qwen/Qwen3-Omni-30B-A3B-Instruct"],
        prompt="transcribe",
    )

    parts = captured["messages"][1]["content"]
    assert parts[1]["type"] == "audio_url"
    assert parts[1]["audio_url"]["url"] == "https://cdn.example.test/audio.mp3"


def test_call_video_url_model_requires_url() -> None:
    with pytest.raises(RuntimeError, match="video_url is empty"):
        siliconflow.call_video_url_model("", models=["Qwen/Qwen3-VL-30B-A3B-Instruct"], prompt="describe")
