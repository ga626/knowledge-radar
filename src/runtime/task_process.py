"""受控任务子进程；原子写artifact后CAS提交成功状态。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.tasks import TaskStore, TERMINAL_STATUSES
from runtime.process_identity import process_identity


def execute_job(input_path):
    folder = input_path.resolve().parent
    job = json.loads(input_path.read_text(encoding="utf-8"))
    if os.environ.get("KR_TASK_INPUT_STDIN") == "1":
        private_input = sys.stdin.buffer.read(1024 * 1024 + 1)
        if len(private_input) > 1024 * 1024:
            raise ValueError("worker input exceeds bound")
        job["payload"] = json.loads(private_input)
        del private_input
    store = TaskStore(job["db_path"])
    task_id = job["task_id"]
    if not store.claim_queued(task_id, {"worker_pid": os.getpid(), "worker_identity": process_identity(), "attempt_id": job["attempt_id"], "phase": "worker_start"}):
        return
    stop = threading.Event()
    def phase(name):
        with (folder / "spans.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": time.time(), "phase": name, "attempt_id": job["attempt_id"]}) + "\n")
        store.heartbeat(task_id, {"phase": name, "worker_pid": os.getpid()})
    def heartbeat():
        while not stop.wait(1):
            store.heartbeat(task_id, {"worker_pid": os.getpid()})
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        phase("worker_imports_begin")
        operation = job["operation"]
        payload = job["payload"]
        if operation == "content_detail":
            import server
            from kr_core import DetailRequest
            phase("worker_imports_end")
            phase("detail_execute_begin")
            result = server._execute_detail_request(DetailRequest(**payload))
        elif operation in {"fixture_echo", "fixture_sleep"} and os.environ.get("KR_TASK_TEST_MODE") == "1":
            phase("fixture_begin")
            time.sleep(float(payload.get("sleep", 0)))
            result = {"value": payload.get("value", "fixture"), "completed": True}
            if payload.get("required_token_sha256"):
                from urllib.parse import parse_qs, urlsplit
                token = parse_qs(urlsplit(payload["value"]["url"]).query).get("xsec_token", [""])[0]
                result["token_received"] = hashlib.sha256(token.encode()).hexdigest() == payload["required_token_sha256"]
        else:
            raise ValueError("unsupported worker operation")
        phase("detail_execute_end")
        if isinstance(result, dict):
            # scope快照在本任务运行期间生成；自身不是自己的依赖。
            result = dict(result)
            for key in ("pending_tasks", "blocking_tasks"):
                if isinstance(result.get(key), list):
                    result[key] = [ref for ref in result[key] if ref.get("task_id") != task_id]
        if (store.get_task(task_id) or {}).get("status") in TERMINAL_STATUSES:
            return
        if isinstance(result, dict) and (result.get("manual_action_required") or result.get("status") == "needs_interaction"):
            store.mark_waiting_for_user(task_id, {"phase": "waiting_for_user", "resume_policy": "explicit_retry"})
            return
        if isinstance(result, dict) and (result.get("error") or result.get("failure_type") or result.get("status") in {"failed", "error", "empty"}):
            store.mark_failed(task_id, "detail returned structured error", error_code=str(result.get("failure_type") or "detail_failed"), metadata={"phase": "failed"})
            return
        if isinstance(result, dict) and result.get("blocking_tasks"):
            store.mark_failed(task_id, "required child tasks remain pending", error_code="dependency_pending",
                              metadata={"phase": "failed", "blocking_task_ids": [ref["task_id"] for ref in result["blocking_tasks"]]})
            return
        from generic_web.url_policy import public_evidence_payload
        artifact = {"schema": "knowledgeradar-task-artifact/v1", "task_id": task_id, "attempt_id": job["attempt_id"], "identity": job["identity"], "result": public_evidence_payload(result)}
        raw = json.dumps(artifact, ensure_ascii=False, sort_keys=True).encode("utf-8")
        temporary = folder / "result.pending"
        with temporary.open("xb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        destination = folder / "result.json"
        temporary.replace(destination)
        phase("artifact_atomic_commit")
        store.mark_completed(task_id, result_path=str(destination), metadata={"artifact_sha256": hashlib.sha256(raw).hexdigest(), "phase": "completed"})
    except Exception as exc:
        store.mark_failed(task_id, type(exc).__name__, error_code="worker_execution_error", metadata={"phase": "failed"})
        raise
    finally:
        stop.set()
        thread.join(timeout=2)
        from runtime.storage_lifecycle import StorageLeases
        StorageLeases().release_process()


if __name__ == "__main__":
    execute_job(Path(sys.argv[1]))
