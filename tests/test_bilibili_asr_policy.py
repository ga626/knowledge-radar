from pathlib import Path


def test_language_per_request_isolated_task_cache_and_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv('KR_ASR_LANGUAGE','zh')
    calls=[]
    def subtitles(bvid, **kwargs):
        calls.append(kwargs['language'])
        return {'hit':True,'text':'English words' if kwargs['language']=='en' else '中文字幕','timestamped_segments':[{'text':'English words' if kwargs['language']=='en' else '中文字幕','start_s':0,'end_s':2}]}
    monkeypatch.setattr(bili,'_probe_bilibili_subtitle',subtitles)
    english=bili.transcribe_bilibili('BVlanguages',output_dir=str(tmp_path/'transcripts'),language='en')
    chinese=bili.transcribe_bilibili('BVlanguages',output_dir=str(tmp_path/'transcripts'),language='zh')
    assert english=='English words' and chinese=='中文字幕'
    assert calls==['en','zh']
    assert bili.transcribe_bilibili('BVlanguages',output_dir=str(tmp_path/'transcripts'),language='en')=='English words'
    assert calls==['en','zh']
    metadata=bili.get_bilibili_transcript_metadata('BVlanguages','en')
    assert metadata['policy']['language']=='en'
    assert metadata['locators']['status']=='aligned'
    assert bili.AsrPolicy.from_env().language=='zh'
    tasks=bili.get_task_store().tasks_for_source(content_id='BVlanguages')
    assert len(tasks)==2
    assert {task['metadata']['language'] for task in tasks}=={'en','zh'}


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
    bili._write_transcript_cache(str(cache_path), "cached transcript")
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
    monkeypatch.setattr(bili, "_missing_transcription_components", lambda: [])

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


def test_bilibili_asr_missing_components_returns_before_queuing_worker(monkeypatch, tmp_path: Path) -> None:
    output_dir = tmp_path / "transcripts"
    monkeypatch.setattr(bili, "_probe_bilibili_subtitle", lambda *args, **kwargs: {"hit": False, "duration_s": 0.01})
    monkeypatch.setattr(bili, "_missing_transcription_components", lambda: ["本地转写运行时（faster-whisper）", "FFmpeg"])

    message = bili.transcribe_bilibili("BVmissing", output_dir=str(output_dir))

    assert message.startswith("[transcribe] INSTALL_COMPONENT_MISSING:")
    assert bili.get_task_store().get_task("bilibili_transcribe_BVmissing") is None


def test_invalid_cache_reexecutes_without_rewriting_completed_history(monkeypatch, tmp_path, isolated_runtime):
    import sys
    from types import SimpleNamespace

    output = tmp_path / "transcripts"
    output.mkdir()
    cache = output / "BVstale_transcript.txt"
    cache.write_text("old unverified transcript", encoding="utf-8")
    (output / "BVstale.m4a").write_bytes(b"isolated audio")
    original = "bilibili_transcribe_BVstale"
    isolated_runtime.upsert_task(original, "bilibili_transcribe", "B站", result_path=str(cache))
    isolated_runtime.mark_completed(original, metadata={"historical": True})
    before = isolated_runtime.get_task(original)
    monkeypatch.setenv("KR_DURABLE_WORKER", "1")
    monkeypatch.setattr(bili, "_probe_bilibili_subtitle", lambda *a, **kw: {"hit": False})
    monkeypatch.setattr(bili, "_missing_transcription_components", lambda: [])
    calls = []
    def transcribe(*a, **kw):
        calls.append(1)
        return {"txt": "new usable transcript"}
    monkeypatch.setitem(sys.modules, "transcribe_bilibili", SimpleNamespace(transcribe=transcribe))
    # 该离线用例只用既有音频；不能依赖开发机已安装的可选下载器。
    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(
        YoutubeDL=lambda *a, **kw: pytest.fail("cached audio must not download")))

    assert bili.transcribe_bilibili("BVstale", output_dir=str(output)) == "new usable transcript"
    assert isolated_runtime.get_task(original) == before
    assert len(calls) == 1
    assert bili.transcribe_bilibili("BVstale", output_dir=str(output)) == "new usable transcript"
    assert len(calls) == 1
    # Deleting output after a completed regeneration must also be recoverable.
    cache.unlink()
    assert bili.transcribe_bilibili("BVstale", output_dir=str(output)) == "new usable transcript"
    assert len(calls) == 2
