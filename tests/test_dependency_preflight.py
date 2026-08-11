from pathlib import Path

from runtime.asr_policy import AsrPolicy, transcript_cache_key
from runtime.dependency_preflight import external_dependency_preflight_summary
import runtime.dependency_preflight as dependency_preflight_module


def test_asr_policy_declares_model_cache_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("KR_ASR_BATCH_SIZE", "4")

    policy = AsrPolicy.from_env()

    assert policy.batch_size == 4
    assert policy.model_cache_dir == str(tmp_path / "runtime" / "models" / "whisper")
    assert policy.compact()["model_cache_dir"] == policy.model_cache_dir
    assert "audio_hash" in policy.compact()["transcript_cache_key_fields"]


def test_transcript_cache_key_includes_engine_policy_fields() -> None:
    key_a = transcript_cache_key(
        platform="bilibili",
        content_id="BV1",
        audio_hash="hash",
        engine="faster-whisper",
        model="base",
        device="cpu",
        compute_type="int8",
        language="zh",
        vad=True,
        beam=5,
    )
    key_b = transcript_cache_key(
        platform="bilibili",
        content_id="BV1",
        audio_hash="hash",
        engine="faster-whisper",
        model="base",
        device="cuda",
        compute_type="float16",
        language="zh",
        vad=True,
        beam=3,
    )

    assert key_a["schema"] == "knowledgeradar-transcript-cache-key/v1"
    assert key_a["fields"]["audio_hash"] == "hash"
    assert key_a["fields"]["device"] == "cpu"
    assert key_a["key"] != key_b["key"]


def test_external_preflight_uses_configured_ffmpeg(monkeypatch, tmp_path):
    exe = tmp_path / "ffmpeg.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("KR_FFMPEG_EXE", str(exe))
    monkeypatch.setenv("KR_WHISPER_MODEL_DIR", str(tmp_path / "models"))

    summary = external_dependency_preflight_summary()

    assert summary["schema"] == "knowledgeradar-external-dependency-preflight/v1"
    assert summary["tools"]["ffmpeg"]["path"] == str(exe)
    assert summary["tools"]["faster_whisper"]["model_cache_dir"] == str(Path(tmp_path / "models"))
    assert summary["tools"]["faster_whisper"]["concurrency"]["status"] == "implemented_p2_2"
    assert summary["tools"]["faster_whisper"]["lifecycle"]["schema"] == "knowledgeradar-asr-lifecycle/v1"
    assert summary["tools"]["funasr"]["default_enabled"] is False
    assert summary["tools"]["sherpa_onnx"]["default_enabled"] is False


def test_external_preflight_discovers_project_local_ffmpeg(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    exe = root / "runtime" / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")
    monkeypatch.delenv("KR_FFMPEG_EXE", raising=False)
    monkeypatch.delenv("KR_FFMPEG_BIN", raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(dependency_preflight_module, "project_root", lambda: root)

    summary = external_dependency_preflight_summary()

    assert summary["tools"]["ffmpeg"]["available"] is True
    assert summary["tools"]["ffmpeg"]["path"] == str(exe)
