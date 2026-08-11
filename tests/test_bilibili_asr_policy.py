from pathlib import Path

import pytest
import collectors.platform.bilibili as bili
from runtime.tasks import TaskStore


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path: Path):
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    monkeypatch.setattr(bili, "get_task_store", lambda: store)
    monkeypatch.setattr(bili, "record_media_cache_entry", lambda *args, **kwargs: {})
    return store


def test_bilibili_asr_cache_hit_skips_subtitle_download_and_asr(monkeypatch, tmp_path: Path) -> None:
    output_dir = tmp_path / "transcripts"
    output_dir.mkdir()
    cache_path = output_dir / "BVcache_transcript.txt"
    cache_path.write_text("cached transcript", encoding="utf-8")
    called = {"subtitle": 0}

    def fake_subtitle(*args, **kwargs):
        called["subtitle"] += 1
        return {"hit": False, "duration_s": 0}

    monkeypatch.setattr(bili, "_probe_bilibili_subtitle", fake_subtitle)

    text = bili.transcribe_bilibili("BVcache", output_dir=str(output_dir), research_session_id="session-cache")
    task = bili.get_task_store().get_task("bilibili_transcribe_BVcache")

    assert text == "cached transcript"
    assert called["subtitle"] == 0
    assert task is not None
    assert task["status"] == "completed"
    assert task["metadata"]["cache_hit"] is True
    assert task["metadata"]["research_session_id"] == "session-cache"


def test_bilibili_asr_subtitle_hit_skips_download_and_asr(monkeypatch, tmp_path: Path) -> None:
    output_dir = tmp_path / "transcripts"
    called = {"thread": 0}

    monkeypatch.setattr(
        bili,
        "_probe_bilibili_subtitle",
        lambda *args, **kwargs: {"hit": True, "text": "subtitle transcript", "duration_s": 0.01, "line_count": 1},
    )

    class FakeThread:
        def __init__(self, *args, **kwargs):
            called["thread"] += 1

        def start(self):
            called["thread"] += 1

    monkeypatch.setattr("runtime.task_adapter.threading.Thread", FakeThread)

    text = bili.transcribe_bilibili("BVsubtitle", output_dir=str(output_dir), research_session_id="session-subtitle")
    task = bili.get_task_store().get_task("bilibili_transcribe_BVsubtitle")

    assert text == "subtitle transcript"
    assert called["thread"] == 0
    assert task is not None
    assert task["status"] == "completed"
    assert task["metadata"]["subtitle_hit"] is True
    assert task["metadata"]["download_s"] == 0.0
    assert task["metadata"]["transcribe_s"] == 0.0


def test_bilibili_asr_no_subtitle_starts_task_with_policy_metadata(monkeypatch, tmp_path: Path) -> None:
    output_dir = tmp_path / "transcripts"
    monkeypatch.setenv("KR_ASR_MODELS", "local:faster-whisper/base,local:faster-whisper/tiny")
    monkeypatch.setenv("KR_ASR_DEVICE", "cpu")
    monkeypatch.setenv("KR_ASR_COMPUTE_TYPE", "int8")
    monkeypatch.setenv("KR_ASR_BEAM_SIZE", "3")
    monkeypatch.setenv("KR_ASR_VAD", "1")
    monkeypatch.setenv("KR_ASR_LANGUAGE", "zh")
    monkeypatch.setattr(
        bili,
        "_probe_bilibili_subtitle",
        lambda *args, **kwargs: {"hit": False, "duration_s": 0.02, "reason": "no_subtitles"},
    )

    started = {"value": False}

    class FakeThread:
        def __init__(self, target, args=(), daemon=True):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            started["value"] = True

    monkeypatch.setattr(bili.threading, "Thread", FakeThread)

    message = bili.transcribe_bilibili("BVnosub", output_dir=str(output_dir), research_session_id="session-nosub")
    task = bili.get_task_store().get_task("bilibili_transcribe_BVnosub")

    assert "后台启动" in message
    assert started["value"] is True
    assert task is not None
    assert task["status"] == "queued"
    assert task["metadata"]["research_session_id"] == "session-nosub"
    assert task["metadata"]["blocks_final_report"] is True
    assert task["metadata"]["adapter"] == "local_thread_lifecycle"
    assert task["metadata"]["model"] == "base"
    assert task["metadata"]["beam_size"] == 3
    assert task["metadata"]["vad_enabled"] is True
    assert task["metadata"]["subtitle_probe_s"] == 0.02
