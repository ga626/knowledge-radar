import video_processor
import pytest


@pytest.fixture(autouse=True)
def isolate_cache(monkeypatch, tmp_path):
    monkeypatch.setenv('KR_DATA_ROOT', str(tmp_path/'data'))
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path/'state'))
    monkeypatch.setenv('KR_MEDIA_CACHE_DIR', str(tmp_path/'media'))


def test_video_text_analysis_defaults_to_safe_bailian_model(monkeypatch) -> None:
    monkeypatch.delenv("KR_ENABLE_SILICONFLOW_FALLBACK", raising=False)
    called = {}

    def fake_call_text_model(**kwargs):
        called.update(kwargs)
        return '{"score": 7, "rationale": "ok"}', "Bailian qwen3.8-omni-flash", {}

    monkeypatch.setattr("understanding.bailian.call_text_model", fake_call_text_model)

    content, provider = video_processor._call_llm("system", "user")

    assert content.startswith("{")
    assert provider == "Bailian qwen3.8-omni-flash"
    assert called["model"] == "qwen3.8-omni-flash"
    assert called["capability"] == "video_text_analysis"


def test_frame_analysis_defaults_to_bailian(monkeypatch) -> None:
    monkeypatch.delenv("KR_ENABLE_SILICONFLOW_FALLBACK", raising=False)
    called = {}

    def fake_call_frame_images_model(images_base64, **kwargs):
        called["images"] = list(images_base64)
        called.update(kwargs)
        return '{"core_summary":"ok"}', "Bailian qwen3.8-omni-flash", {}

    monkeypatch.setattr("understanding.bailian.call_frame_images_model", fake_call_frame_images_model)

    content, provider = video_processor._call_multimodal("system", "text", ["abc"])

    assert content.startswith("{")
    assert provider == "Bailian qwen3.8-omni-flash"
    assert called["images"] == ["abc"]
    assert called["model"] == "qwen3.8-omni-flash"
