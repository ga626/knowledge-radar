"""现行采集/增强边界：失败转写不作为证据，持久结果保留正文和真实依赖。"""
from runtime.detail_composition import build_detail_strategy

import hashlib
import json
from pathlib import Path

import pytest

from kr_core import DetailRequest
from runtime.tasks import TaskStore


@pytest.fixture(autouse=True)
def isolate_worker_data(tmp_path, monkeypatch):
    for key, value in {
        "KR_DATA_ROOT": "data", "KR_STATE_DIR": "state", "KR_LOG_DIR": "logs",
        "KR_EVIDENCE_ROOT": "assets", "KR_TASK_DB_PATH": "tasks.sqlite3",
        "KR_RUNTIME_LEASE_DB_PATH": "leases.sqlite3",
    }.items():
        monkeypatch.setenv(key, str(tmp_path / value))
    from runtime import leases, failure_cache
    monkeypatch.setattr(leases, "_COORDINATOR", leases.RuntimeLeaseCoordinator(tmp_path / "leases.sqlite3"))
    monkeypatch.setattr(failure_cache, "detail_failure_cache", failure_cache.FailureStateCache())


@pytest.fixture
def failed_asr(tmp_path, monkeypatch):
    from collectors.platform import bilibili
    from runtime import media_acquisition, optional_media, optional_media_worker, component_environment
    audio = tmp_path / "fixture.wav"
    audio.write_bytes(b"isolated audio fixture; model boundary is replaced")
    sha = hashlib.sha256(audio.read_bytes()).hexdigest()
    receipt = tmp_path / "receipts"
    receipt.mkdir()
    (receipt / "qwen3-asr-0.6b.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(optional_media, "receipt_root", lambda: receipt)
    monkeypatch.setattr(optional_media, "validate_receipt", lambda _: {"model_root": str(tmp_path / "model")})
    monkeypatch.setattr(component_environment, "component_environment", lambda *a, **kw: None)
    monkeypatch.setattr(media_acquisition, "acquire_media_ref", lambda ref: {"path": str(audio), "sha256": sha, "download_s": 0})
    monkeypatch.setattr(bilibili, "collect_video_detail", lambda request: {
        "title": "fixture", "content": "独立采集正文", "transcript": "", "comments": [],
        "media_manifest": [{"type": "audio", "url": "https://example.org/audio.wav"}],
    })
    def configure(message):
        monkeypatch.setattr(optional_media_worker, "execute", lambda request: {"status": "failed", "text": message})
    configure("[transcribe] failed: completed")
    return configure


def request():
    return DetailRequest(platform="B站", url="https://www.bilibili.com/video/BV1HhaA6hEsn",
                         options={"include_transcript": True, "include_comments": False})


def assert_failed_transcript(data):
    assert data["content"] == "独立采集正文"
    assert not data["transcript"]
    assert data["enhancement_status"] == "partial"
    assert data["transcript_status"]["status"] == "failed"
    steps = data["media_execution"]["steps"]
    assert any(step["status"] == "failed" and step["error_code"] == "optional_component_empty_or_failed" for step in steps)


@pytest.mark.parametrize("message", ["[transcribe] failed: completed", "[transcribe] 转写失败: decoder error", ""])
def test_transcription_failure_is_not_usable_evidence(message, failed_asr):
    failed_asr(message)
    assert_failed_transcript(build_detail_strategy("B站").extract(request()).data)


def write_job(tmp_path, store):
    from dataclasses import asdict
    path = tmp_path / "input.json"
    path.write_text(json.dumps({"db_path": store.db_path, "task_id": "durable-parent", "attempt_id": "one",
        "operation": "content_detail", "payload": asdict(request()), "identity": {}}), encoding="utf-8")
    return path


def test_worker_excludes_itself_but_preserves_real_child_and_fails_blocked_result(tmp_path, monkeypatch):
    from runtime import task_process, platform_execution
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    for task_id in ("durable-parent", "child-running"):
        store.upsert_task(task_id, "durable_content_detail", "B站", metadata={"blocks_final_report": True})
    refs = [{"task_id": tid, "status": "running"} for tid in ("durable-parent", "child-running")]
    monkeypatch.setattr(platform_execution, "worker_detail", lambda *a, **kw: {
        "transcript": "", "pending_tasks": refs, "blocking_tasks": refs})
    task_process.execute_job(write_job(tmp_path, store))
    parent = store.get_task("durable-parent")
    assert parent["status"] == "failed" and parent["error_code"] == "dependency_pending"
    data = json.loads(Path(parent["result_path"]).read_text(encoding="utf-8"))["result"]
    assert data["blocking_tasks"] == data["pending_tasks"] == [refs[1]]
    assert store.get_task("child-running")["status"] == "queued"


def test_worker_completed_result_has_no_self_reference(tmp_path, monkeypatch):
    from runtime import task_process, platform_execution
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    store.upsert_task("durable-parent", "durable_content_detail", "B站")
    self_ref = [{"task_id": "durable-parent", "status": "running"}]
    monkeypatch.setattr(platform_execution, "worker_detail", lambda *a, **kw: {
        "transcript": "actual words", "pending_tasks": self_ref, "blocking_tasks": self_ref})
    task_process.execute_job(write_job(tmp_path, store))
    parent = store.get_task("durable-parent")
    assert parent["status"] == "completed"
    data = json.loads(Path(parent["result_path"]).read_text(encoding="utf-8"))["result"]
    assert data["pending_tasks"] == data["blocking_tasks"] == []


def test_detail_strategy_failure_survives_server_and_worker_boundary(tmp_path, monkeypatch, failed_asr):
    import server
    from runtime import task_process
    from dataclasses import asdict
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    monkeypatch.setattr(server, "get_task_store", lambda: store)
    # 同步用例经过真实策略/媒体执行/失败校验，不把门面当作worker依赖。
    assert_failed_transcript(server._execute_detail_request(request()))
    store.upsert_task("durable-parent", "durable_content_detail", "B站")
    path = write_job(tmp_path, store)
    task_process.execute_job(path)
    collected_task = store.get_task("durable-parent")
    assert collected_task["status"] == "queued"
    collected = json.loads(Path(collected_task["result_path"]).read_text(encoding="utf-8"))["result"]
    assert collected["content"] == "独立采集正文" and collected["_enhancement_pending"] is True
    task_process.execute_job(path, stage="enhancement", execution_input=json.dumps({
        "request": asdict(request()), "collected": collected}))
    parent = store.get_task("durable-parent")
    assert parent["status"] == "failed" and parent["error_code"] == "enhancement_incomplete"
    data = json.loads(Path(parent["result_path"]).read_text(encoding="utf-8"))["result"]
    assert_failed_transcript(data)
    assert not data.get("blocking_tasks")
