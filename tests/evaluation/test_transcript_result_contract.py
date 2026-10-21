import json
import sys
from types import SimpleNamespace

import pytest

from detail_strategies import BilibiliDetailDeps, BilibiliDetailStrategy
from kr_core import DetailRequest, EvidenceItem
from runtime.tasks import TaskStore


@pytest.fixture(autouse=True)
def isolate_worker_data(tmp_path, monkeypatch):
    monkeypatch.setenv("KR_DATA_ROOT", str(tmp_path / "data"))


@pytest.mark.parametrize("message", ["[transcribe] failed: completed", "[transcribe] 转写失败: decoder error", ""])
def test_transcription_failure_is_not_usable_evidence(message, tmp_path):
    strategy = BilibiliDetailStrategy(BilibiliDetailDeps(
        extract_bvid=lambda url: "BVtest", get_info=lambda bvid: {"title": "test"},
        transcribe=lambda *a: message, get_comments=lambda *a: [], filter_comments=lambda *a: [],
        attach_routing=lambda url, data: data, routing_recommends_l2=lambda data: False,
        deep_analyze=lambda *a: {}, direct_media_probe=lambda *a: {},
        evidence_builder=lambda url, platform, data: EvidenceItem(source_url=url, source_platform=platform),
        data_dir=str(tmp_path)))
    data = strategy.extract(DetailRequest(url="https://www.bilibili.com/video/BVtest",
        options={"include_transcript": True, "include_comments": False})).data
    assert data["transcript"] == ""
    assert data["failure_type"] == "transcript_failed"
    assert data["multimodal_pipeline"]["events"][0]["status"] == "failed"


def test_worker_excludes_itself_but_preserves_real_child_and_fails_blocked_result(tmp_path, monkeypatch):
    from runtime import task_process
    db = tmp_path / "tasks.sqlite3"
    store = TaskStore(str(db))
    for task_id in ("durable-parent", "child-running"):
        store.upsert_task(task_id, "durable_content_detail", "B站", metadata={"blocks_final_report": True})
    refs = [{"task_id": tid, "status": "running"} for tid in ("durable-parent", "child-running")]
    monkeypatch.setitem(sys.modules, "server", SimpleNamespace(_execute_detail_request=lambda request: {
        "transcript": "", "pending_tasks": refs, "blocking_tasks": refs}))
    job = {"db_path": str(db), "task_id": "durable-parent", "attempt_id": "one",
           "operation": "content_detail", "payload": {"url": "https://www.bilibili.com/video/BVtest"}, "identity": {}}
    path = tmp_path / "input.json"
    path.write_text(json.dumps(job), encoding="utf-8")
    monkeypatch.delenv("KR_TASK_INPUT_STDIN", raising=False)
    task_process.execute_job(path)
    parent = store.get_task("durable-parent")
    assert parent["status"] == "failed"
    assert parent["error_code"] == "dependency_pending"
    assert parent["metadata"]["blocking_task_ids"] == ["child-running"]
    assert store.get_task("child-running")["status"] == "queued"
    assert not (tmp_path / "result.json").exists()


def test_worker_completed_result_has_no_self_reference(tmp_path, monkeypatch):
    from runtime import task_process
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    store.upsert_task("durable-parent", "durable_content_detail", "B站")
    self_ref = [{"task_id": "durable-parent", "status": "running"}]
    monkeypatch.setitem(sys.modules, "server", SimpleNamespace(_execute_detail_request=lambda request: {
        "transcript": "actual words", "pending_tasks": self_ref, "blocking_tasks": self_ref}))
    job = {"db_path": store.db_path, "task_id": "durable-parent", "attempt_id": "one",
           "operation": "content_detail", "payload": {"url": "https://www.bilibili.com/video/BVtest"}, "identity": {}}
    path = tmp_path / "input.json"
    path.write_text(json.dumps(job), encoding="utf-8")
    monkeypatch.delenv("KR_TASK_INPUT_STDIN", raising=False)
    task_process.execute_job(path)
    assert store.get_task("durable-parent")["status"] == "completed"
    data = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))["result"]
    assert data["pending_tasks"] == data["blocking_tasks"] == []


def test_detail_strategy_failure_survives_server_and_worker_boundary(tmp_path, monkeypatch):
    import server
    from runtime import task_process
    strategy = BilibiliDetailStrategy(BilibiliDetailDeps(
        extract_bvid=lambda url: "BVtest", get_info=lambda bvid: {"title": "test"},
        transcribe=lambda *a: "[transcribe] failed: completed",
        get_comments=lambda *a: [], filter_comments=lambda *a: [],
        attach_routing=lambda url, data: data, routing_recommends_l2=lambda data: False,
        deep_analyze=lambda *a: {}, direct_media_probe=lambda *a: {},
        evidence_builder=lambda url, platform, data: EvidenceItem(source_url=url, source_platform=platform),
        data_dir=str(tmp_path)))
    monkeypatch.setattr(server, "_bilibili_detail_strategy", lambda: strategy)
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    monkeypatch.setattr(server, "get_task_store", lambda: store)
    store.upsert_task("durable-parent", "durable_content_detail", "B站")
    job = {"db_path": store.db_path, "task_id": "durable-parent", "attempt_id": "one",
           "operation": "content_detail", "identity": {},
           "payload": {"url": "https://www.bilibili.com/video/BV1HhaA6hEsn",
                       "options": {"include_transcript": True, "include_comments": False}}}
    path = tmp_path / "input.json"
    path.write_text(json.dumps(job), encoding="utf-8")
    monkeypatch.delenv("KR_TASK_INPUT_STDIN", raising=False)
    task_process.execute_job(path)
    assert store.get_task("durable-parent")["status"] == "failed"
    assert store.get_task("durable-parent")["error_code"] == "transcript_failed"
    assert not (tmp_path / "result.json").exists()
