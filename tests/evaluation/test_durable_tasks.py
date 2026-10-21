import time

import pytest

from runtime.task_service import TaskService
from runtime.tasks import TaskStore


def await_terminal(service, task_id, timeout=12):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        row = service.result(task_id)
        if row["status"] not in {"queued", "running"}:
            return row
        time.sleep(.04)
    pytest.fail("isolated worker did not terminate")


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("KR_TASK_TEST_MODE", "1")
    monkeypatch.setenv("KR_DATA_ROOT", str(tmp_path / "data"))
    return TaskService(TaskStore(str(tmp_path / "tasks.sqlite3")))


def test_process_result_idempotency_and_integrity(service):
    start = time.monotonic()
    row = service.submit("fixture_echo", {"value": "same"}, timeout_s=10)
    assert time.monotonic() - start < 2
    result = await_terminal(service, row["task_id"])
    assert result["status"] == "completed"
    assert result["result"]["value"] == "same"
    assert service.submit("fixture_echo", {"value": "same"}, timeout_s=10)["task_id"] == row["task_id"]
    artifact = service.root / row["task_id"] / "result.json"
    artifact.write_text("{}", encoding="utf-8")
    assert service.result(row["task_id"])["status"] == "artifact_corrupt"


def test_process_input_and_artifact_never_persist_access_token(service):
    import hashlib
    marker = "PRIVATE_XSEC_CANARY"
    url = "https://www.xiaohongshu.com/explore/fixture?xsec_token=" + marker
    row = service.submit("fixture_echo", {"value": {"url": url, "fetch_url": url}, "required_token_sha256": hashlib.sha256(marker.encode()).hexdigest()}, timeout_s=10)
    result = await_terminal(service, row["task_id"])
    assert result["status"] == "completed"
    assert result["result"]["token_received"] is True
    folder = service.root / row["task_id"]
    assert all(marker not in path.read_text(encoding="utf-8") for path in folder.glob("*.json"))
    assert result["result"]["value"]["url"] == "https://www.xiaohongshu.com/explore/fixture"


def test_hard_deadline_and_explicit_retry(service):
    row = service.submit("fixture_sleep", {"sleep": 3}, timeout_s=.4)
    result = await_terminal(service, row["task_id"])
    assert result["status"] == "failed"
    assert service.store.get_task(row["task_id"])["error_code"] == "deadline_exceeded"
    retry = service.retry(row["task_id"])
    assert retry["task_id"] != row["task_id"]
    await_terminal(service, retry["task_id"])


def test_cancel_and_restart_recovery(service):
    row = service.submit("fixture_sleep", {"sleep": 3, "value": "cancel"}, timeout_s=10)
    service.store.cancel_task(row["task_id"])
    time.sleep(.4)
    assert service.result(row["task_id"])["status"] == "cancelled"
    service.store.upsert_task("durable-abandoned", "durable_fixture", "test", metadata={"adapter": "durable_process", "owner_pid": 0})
    assert "durable-abandoned" in service.recover()["interrupted"]
    assert service.store.mark_completed("durable-abandoned")["status"] == "interrupted"


def test_terminal_heartbeat_does_not_change_committed_metadata(service):
    service.store.upsert_task("terminal", "fixture", "test")
    service.store.mark_completed("terminal", metadata={"phase": "completed"})
    service.store.heartbeat("terminal", {"phase": "late"})
    assert service.store.get_task("terminal")["metadata"]["phase"] == "completed"


def test_reused_pid_does_not_keep_old_task_alive(service,monkeypatch):
    from runtime import task_service
    monkeypatch.setattr(task_service,'process_identity',lambda pid:'same-pid-new-generation')
    service.store.upsert_task('durable-old-generation','durable_fixture','test',metadata={
        'adapter':'durable_process','owner_pid':123,'owner_identity':'same-pid-old-generation','deadline_at':time.time()+60})
    assert 'durable-old-generation' in service.recover()['interrupted']
