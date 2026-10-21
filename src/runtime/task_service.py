"""单机持久任务：受控子进程、deadline、原子结果与可回读失败。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import subprocess
from pathlib import Path
import sys
import threading
import time
import uuid

from runtime.paths import project_root
from runtime.process import silent_subprocess_popen, silent_subprocess_run
from runtime.tasks import get_task_store, compact_task_ref, TERMINAL_STATUSES
from runtime.process_identity import process_identity

_MONITORS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kr-task-monitor")
_SUBMIT_LOCK = threading.RLock()


def content_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def source_identity(root=None):
    root = Path(root or project_root()) / "src"
    hashes = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in root.rglob("*.py") if not any(x in p.parts for x in {"__pycache__", "data"})}
    return content_hash(hashes)


class TaskService:
    def __init__(self, store=None):
        self.store = store or get_task_store()
        self.root = Path(self.store.db_path).resolve().parent / "task_artifacts"
        self.root.mkdir(parents=True, exist_ok=True)

    def submit(self, operation, payload, *, timeout_s=600, scope=None, retry_of=""):
        if operation not in {"content_detail", "fixture_echo", "fixture_sleep"}:
            raise ValueError("unsupported durable operation")
        if operation.startswith("fixture_") and os.environ.get("KR_TASK_TEST_MODE") != "1":
            raise ValueError("fixture worker is disabled outside isolated tests")
        if timeout_s <= 0 or timeout_s > 3600:
            raise ValueError("invalid task deadline")
        from generic_web.url_policy import public_evidence_payload
        # 临时凭据仅经匿名管道交给本次worker，绝不进入任务文件或命令行。
        execution_input = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(execution_input) > 1024 * 1024:
            raise ValueError("task input exceeds bounded worker size")
        payload = public_evidence_payload(payload)
        scope = public_evidence_payload(scope or {})
        identity = {"source": source_identity(), "python": sys.version, "models": {k: os.environ.get(k, "") for k in
                    ["KR_ASR_MODELS", "KR_OCR_MODELS", "KR_FRAME_VISION_MODELS", "KR_NATIVE_VIDEO_MODELS"]}, "contract": 2}
        if operation == "content_detail" and payload.get("options", {}).get("include_transcript"):
            from runtime.asset_identity import model_identity
            from runtime.asr_policy import AsrPolicy
            identity["asr_asset"] = model_identity(AsrPolicy.from_env())
        # 宿主代际属于运行证据，不属于业务输入；同一完成请求可跨server重启复用。
        canonical_payload = json.loads(json.dumps(payload))
        canonical_payload.get("options", {}).get("task_scope", {}).pop("server_run_id", None)
        key = content_hash({"operation": operation, "payload": canonical_payload, "identity": identity, "retry_of": retry_of})
        task_id = "durable-" + key[:32]
        with _SUBMIT_LOCK:
            existing = self.store.get_task(task_id)
            if existing:
                return existing
            active = [row for row in self.store.recent_tasks(200) if (row.get("metadata") or {}).get("adapter") == "durable_process" and row["status"] in {"queued", "running"}]
            if len(active) >= 24:
                raise RuntimeError("durable task admission full; retry later")
            folder = self.root / task_id
            try:
                folder.mkdir(exist_ok=False)
            except FileExistsError as exc:
                raise RuntimeError("task reservation incomplete; inspect before retry") from exc
            job = {"schema": "knowledgeradar-durable-job/v1", "task_id": task_id, "attempt_id": uuid.uuid4().hex,
                   "operation": operation, "payload": payload, "identity": identity, "timeout_s": timeout_s,
                   "submitted_at": time.time(), "db_path": self.store.db_path, "scope": scope or {}}
            (folder / "input.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
            task = self.store.upsert_task(task_id, "durable_" + operation, payload.get("platform", "runtime"),
                    metadata={**(scope or {}), "adapter": "durable_process", "request_id": key, "attempt_id": job["attempt_id"],
                              "owner_pid": os.getpid(),
                              "owner_identity": process_identity(),
                              "source_identity": identity["source"], "deadline_at": job["submitted_at"] + timeout_s,
                              "phase": "queued", "blocks_final_report": True, "retry_of": retry_of})
            _MONITORS.submit(self._monitor, job, folder, execution_input)
            return task

    def _monitor(self, job, folder, execution_input):
        task_id = job["task_id"]
        deadline = job["submitted_at"] + job["timeout_s"]
        proc = None
        try:
            if time.time() >= deadline:
                self.store.mark_failed(task_id, "deadline expired in queue", error_code="deadline_exceeded")
                return
            if (self.store.get_task(task_id) or {}).get("status") in TERMINAL_STATUSES:
                return
            env = dict(os.environ, KR_DURABLE_WORKER="1", KR_TASK_INPUT_STDIN="1", KR_TASK_DB_PATH=self.store.db_path, PYTHONIOENCODING="utf-8")
            script = Path(__file__).with_name("task_process.py")
            with (folder / "stdout.txt").open("a", encoding="utf-8") as out, (folder / "stderr.txt").open("a", encoding="utf-8") as err:
                proc = silent_subprocess_popen([sys.executable, "-X", "utf8", str(script), str(folder / "input.json")], env=env, stdin=subprocess.PIPE, stdout=out, stderr=err)
                try:
                    proc.stdin.write(execution_input)
                finally:
                    proc.stdin.close()
                del execution_input
                while proc.poll() is None:
                    current = self.store.get_task(task_id) or {}
                    if current.get("status") in {"cancelled", "interrupted"} or time.time() >= deadline:
                        if current.get("status") not in TERMINAL_STATUSES:
                            self.store.mark_failed(task_id, "task deadline exceeded", error_code="deadline_exceeded", metadata={"phase": "deadline_exceeded"})
                        self._stop_owned_worker(proc)
                        break
                    time.sleep(0.05)
                current = self.store.get_task(task_id) or {}
                if current.get("status") not in {*TERMINAL_STATUSES, "waiting_for_user"}:
                    self.store.mark_interrupted(task_id, "worker_exited_without_committed_result", metadata={"worker_returncode": proc.returncode})
        except Exception as exc:
            if proc is not None and proc.poll() is None:
                self._stop_owned_worker(proc)
            self.store.mark_failed(task_id, type(exc).__name__, error_code="worker_launch_error")

    @staticmethod
    def _stop_owned_worker(proc):
        # 仅持有由本Service创建的Popen句柄，不对宿主或共享服务发信号。
        if proc.poll() is None:
            if os.name == "nt":
                silent_subprocess_run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=10)
            else:
                proc.terminate()
            proc.wait(timeout=10)

    def result(self, task_id):
        task = self.store.get_task(task_id)
        if not task:
            return {"status": "not_found", "task_id": task_id}
        response = {"task": compact_task_ref(task), "status": task["status"]}
        if task["status"] == "completed":
            path = Path(task["result_path"]).resolve()
            if not path.is_relative_to(self.root) or not path.is_file():
                return {**response, "status": "artifact_missing", "error": "committed artifact unavailable"}
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != (task.get("metadata") or {}).get("artifact_sha256"):
                return {**response, "status": "artifact_corrupt", "error": "artifact integrity mismatch"}
            response["result"] = json.loads(raw.decode("utf-8"))["result"]
        return response

    def recover(self):
        interrupted = []
        for task in self.store.recent_tasks(500):
            meta = task.get("metadata") or {}
            if meta.get("adapter") != "durable_process" or task["status"] not in {"queued", "running"}:
                continue
            owner = process_identity(meta["owner_pid"]) if meta.get("owner_pid") else ""
            worker = process_identity(meta["worker_pid"]) if meta.get("worker_pid") else ""
            alive = bool(owner and owner == meta.get("owner_identity"))
            if task["status"] == "running":
                alive = alive and bool(worker and worker == meta.get("worker_identity"))
            if alive and time.time() < float(meta.get("deadline_at") or 0):
                continue
            # 不推测闭包可恢复；明确中断，用户可经retry入口重提。
            self.store.mark_interrupted(task["task_id"], "worker_not_alive_after_recovery", metadata={"phase": "interrupted", "resume_policy": "explicit_retry"})
            interrupted.append(task["task_id"])
        return {"interrupted": interrupted, "automatic_reexecution": False}

    def retry(self, task_id):
        task = self.store.get_task(task_id) or {}
        if task.get("status") not in {"failed", "cancelled", "interrupted", "waiting_for_user"}:
            return task
        if not task_id.startswith("durable-") or Path(task_id).name != task_id:
            raise ValueError("not a durable task")
        job = json.loads((self.root / task_id / "input.json").read_text(encoding="utf-8"))
        return self.submit(job["operation"], job["payload"], timeout_s=job["timeout_s"], scope=job["scope"], retry_of=task_id)
