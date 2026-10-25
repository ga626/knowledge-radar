import pytest

from understanding import bailian


class FakePolicy:
    def is_open(self, key):
        return {"open": False}

    def retry_with_jitter(self, key, scope, fn, retryable_exceptions=(), metadata=None):
        return fn()

    def record_degradation(self, *args, **kwargs):
        return {}


class FakeUsageTracker:
    def __init__(self):
        self.records = []

    def record(self, **kwargs):
        self.records.append(kwargs)
        return kwargs


class FakeMonitorTracker:
    def __init__(self):
        self.records = []

    def record(self, **kwargs):
        self.records.append(kwargs)
        return kwargs


def test_build_multimodal_payload_uses_dashscope_official_schema() -> None:
    payload = bailian.build_multimodal_payload(
        model="bailian:qwen3-vl-flash",
        system_prompt="system",
        prompt="describe",
        image_urls=["https://example.test/image.jpg"],
        video_urls=["https://example.test/video.mp4"],
        media_params={"fps": 1, "num_frames": 8},
        max_tokens=256,
    )

    assert payload["model"] == "qwen3-vl-flash"
    content = payload["input"]["messages"][0]["content"]
    assert content[0] == {"text": "system"}
    assert content[1] == {"image": "https://example.test/image.jpg", "fps": 1, "num_frames": 8}
    assert content[2] == {"video": "https://example.test/video.mp4", "fps": 1, "num_frames": 8}
    assert content[3] == {"text": "describe"}
    assert payload["parameters"]["max_tokens"] == 256


def test_call_video_url_model_records_usage_and_cached_tokens(monkeypatch, tmp_path) -> None:
    captured = {}
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    fake_usage = FakeUsageTracker()
    fake_monitor = FakeMonitorTracker()
    monkeypatch.setattr(bailian, "get_degradation_policy", lambda: FakePolicy())
    monkeypatch.setattr(bailian, "get_usage_tracker", lambda: fake_usage)
    monkeypatch.setattr(bailian, "get_monitor_tracker", lambda: fake_monitor)

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {
                "output": {
                    "choices": [
                        {
                            "message": {
                                "content": [{"text": "video summary"}],
                                "role": "assistant",
                            }
                        }
                    ]
                },
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 9,
                    "total_tokens": 109,
                    "prompt_tokens_details": {"cached_tokens": 40},
                },
            }

    def fake_post(url, *, headers, json, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(bailian.httpx, "post", fake_post)

    content, provider, usage = bailian.call_video_url_model(
        "https://media.example.test/video.mp4",
        model="bailian:qwen3-vl-flash",
        prompt="describe",
        max_frames=8,
        timeout=30,
        max_tokens=128,
    )

    assert content == "video summary"
    assert provider == "Bailian qwen3-vl-flash"
    assert usage["prompt_tokens"] == 100
    assert usage["completion_tokens"] == 9
    assert usage["cached_tokens"] == 40
    assert captured["url"].endswith("/multimodal-generation/generation")
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    video_part = captured["json"]["input"]["messages"][0]["content"][1]
    assert video_part == {"video": "https://media.example.test/video.mp4", "num_frames": 8}
    assert fake_usage.records[0]["metadata"]["cached_tokens"] == 40
    assert fake_monitor.records[0]["name"] == "bailian:native_video"


def test_smoke_video_url_skips_without_api_key(monkeypatch) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)

    result = bailian.smoke_video_url("https://media.example.test/video.mp4")

    assert result["status"] == "skipped"
    assert "API key" in result["reason"]


def test_omni_default_disables_thinking_and_records_reasoning_usage(monkeypatch) -> None:
    captured = {}
    tracker = FakeUsageTracker()
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setattr(bailian, "get_degradation_policy", lambda: FakePolicy())
    monkeypatch.setattr(bailian, "get_usage_tracker", lambda: tracker)
    monkeypatch.setattr(bailian, "get_monitor_tracker", lambda: FakeMonitorTracker())

    def post(url, *, headers, json, timeout):
        captured.update(json)
        class Response:
            status_code = 200
            def json(self):
                return {
                    "output": {"choices": [{"message": {"content": [{"text": "red square"}]}}]},
                    "usage": {"input_tokens": 20, "output_tokens": 8,
                              "output_tokens_details": {"reasoning_tokens": 3}},
                }
        return Response()

    monkeypatch.setattr(bailian.httpx, "post", post)
    content, provider, usage = bailian.call_image_url_model("https://example.test/image.png", prompt="describe")
    assert content == "red square"
    assert provider == "Bailian qwen3.8-omni-flash"
    assert captured["parameters"]["reasoning_effort"] == "none"
    assert usage["reasoning_tokens"] == 3
    assert tracker.records[0]["metadata"]["reasoning_tokens"] == 3
    assert tracker.records[0]["metadata"]["reasoning_effort"] == "none"


def test_omni_joint_audio_video_uses_low_effort_and_allows_explicit_none(monkeypatch) -> None:
    requests = []
    def post(payload, *, timeout):
        requests.append(payload)
        return bailian.BailianResponse("ok", "Bailian qwen3.8-omni-flash", {}, 0.1, {})
    monkeypatch.setattr(bailian, "_post_multimodal_generation", post)
    monkeypatch.setattr(bailian, "get_degradation_policy", lambda: FakePolicy())
    monkeypatch.setattr(bailian, "get_usage_tracker", lambda: FakeUsageTracker())
    monkeypatch.setattr(bailian, "get_monitor_tracker", lambda: FakeMonitorTracker())
    kwargs = dict(prompt="relate sound to picture", capability="native_audio_video",
                  image_urls=["https://example.test/image.png"], audio_urls=["https://example.test/audio.wav"])
    bailian.call_multimodal_generation(**kwargs)
    bailian.call_multimodal_generation(**kwargs, reasoning_effort="none")
    assert requests[0]["parameters"]["reasoning_effort"] == "low"
    assert requests[1]["parameters"]["reasoning_effort"] == "none"
    assert requests[0]["input"]["messages"][0]["content"][1:3] == [
        {"image": "https://example.test/image.png"}, {"audio": "https://example.test/audio.wav"}]


def test_omni_rejects_invalid_effort_before_network() -> None:
    with pytest.raises(ValueError, match="reasoning_effort"):
        bailian.build_multimodal_payload(model="bailian:qwen3.8-omni-flash", prompt="x", reasoning_effort="typo")


def test_compatible_endpoint_accepts_official_base_url(monkeypatch) -> None:
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://workspace.example/compatible-mode/v1/")
    assert bailian.bailian_compatible_base_url() == "https://workspace.example/compatible-mode/v1"


def test_call_image_url_model_requires_api_key(monkeypatch) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setattr(bailian, "get_degradation_policy", lambda: FakePolicy())

    with pytest.raises(RuntimeError, match="API key"):
        bailian.call_image_url_model(
            "https://example.test/image.jpg",
            prompt="describe",
            timeout=1,
        )


def test_call_text_model_uses_openai_compatible_endpoint(monkeypatch) -> None:
    captured = {}
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    fake_usage = FakeUsageTracker()
    monkeypatch.setattr(bailian, "get_degradation_policy", lambda: FakePolicy())
    monkeypatch.setattr(bailian, "get_usage_tracker", lambda: fake_usage)
    monkeypatch.setattr(bailian, "get_monitor_tracker", lambda: FakeMonitorTracker())

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {
                "choices": [{"message": {"content": "text result"}}],
                "usage": {
                    "prompt_tokens": 42,
                    "completion_tokens": 5,
                    "prompt_tokens_details": {"cached_tokens": 12},
                },
            }

    def fake_post(url, *, headers, json, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return FakeResponse()

    monkeypatch.setattr(bailian.httpx, "post", fake_post)

    content, provider, usage = bailian.call_text_model(
        model="bailian:qwen-turbo",
        system_prompt="system",
        user_prompt="user",
        max_tokens=64,
    )

    assert content == "text result"
    assert provider == "Bailian qwen-turbo"
    assert usage["cached_tokens"] == 12
    assert captured["url"].endswith("/chat/completions")
    assert captured["json"]["model"] == "qwen-turbo"
    assert fake_usage.records[0]["metadata"]["schema"] == "openai-compatible"


def test_call_frame_images_model_wraps_base64_as_data_url(monkeypatch) -> None:
    captured = {}
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setattr(bailian, "get_degradation_policy", lambda: FakePolicy())
    monkeypatch.setattr(bailian, "get_usage_tracker", lambda: FakeUsageTracker())
    monkeypatch.setattr(bailian, "get_monitor_tracker", lambda: FakeMonitorTracker())

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {
                "output": {"choices": [{"message": {"content": [{"text": "frame result"}]}}]},
                "usage": {"input_tokens": 10, "output_tokens": 3},
            }

    def fake_post(url, *, headers, json, timeout):
        captured["json"] = json
        return FakeResponse()

    monkeypatch.setattr(bailian.httpx, "post", fake_post)

    content, provider, usage = bailian.call_frame_images_model(
        ["abc123"],
        model="bailian:qwen3-vl-flash",
        prompt="describe frames",
    )

    assert content == "frame result"
    assert provider == "Bailian qwen3-vl-flash"
    assert usage["prompt_tokens"] == 10
    image_part = captured["json"]["input"]["messages"][0]["content"][1]
    assert image_part == {"image": "data:image/jpeg;base64,abc123"}
