"""单机持久任务：同进程执行、deadline、原子结果与可回读失败。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time
import uuid

from runtime.tasks import get_task_store, compact_task_ref, TERMINAL_STATUSES
from runtime.process_identity import process_identity

_MONITORS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kr-task-monitor")
_ENHANCEMENT_MONITORS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kr-media-monitor")
_SUBMIT_LOCK = threading.RLock()
_MAINTENANCE_LOCK = threading.RLock()
_MAINTENANCE = {}
_ARCHIVE_IO = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kr-archive-io")
_ARCHIVE_FUTURES = {}
log = logging.getLogger(__name__)


def content_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def source_identity(root=None):
    root = Path(root or Path(__file__).resolve().parents[2]) / "src"
    hashes = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in root.rglob("*") if p.is_file() and p.suffix in {".py", ".js", ".mjs"}
              and not any(x in p.parts for x in {"__pycache__", "data"})}
    return content_hash(hashes)


class TaskService:
    def __init__(self, store=None):
        self.store = store or get_task_store()
        self.root = Path(self.store.db_path).resolve().parent / "task_artifacts"
        self._enhancement_condition = threading.Condition()
        self._enhancement_pending = {}
        self._enhancement_active = 0
        self._enhancement_dispatcher = None

    def start_maintenance(self, *, interval_s=5.0, keep_alive=False):
        """一个任务库一个进程内维护者；只由启动/接单入口调用，查询从不启动。"""
        key = str(Path(self.store.db_path).resolve())
        with _MAINTENANCE_LOCK:
            current = _MAINTENANCE.get(key)
            if current and not current["stop"].is_set():
                current["keep_alive"] |= keep_alive
                return current["stop"]
            state = {"stop": threading.Event(), "keep_alive": keep_alive}
            _MAINTENANCE[key] = state

            def maintain():
                try:
                    while not state["stop"].wait(interval_s):
                        try:
                            self.maintain_once()
                        except Exception:
                            log.exception("task maintenance failed; next bounded pass will retry")
                            continue
                        with _MAINTENANCE_LOCK:
                            active = self.store.managed_tasks(adapter="durable_process", statuses=("queued", "running", "waiting_for_user", "waiting_cooldown"))
                            pending = self.store.managed_tasks(adapter="durable_process",
                                statuses=tuple(TERMINAL_STATUSES), recording_pending=True)
                            from runtime.research_ledger import has_pending_submissions
                            if not state["keep_alive"] and not active and not pending and not has_pending_submissions():
                                # 与接单使用同一把锁，避免新工作错过即将退出的维护者。
                                state["stop"].set()
                                break
                finally:
                    with _MAINTENANCE_LOCK:
                        if _MAINTENANCE.get(key) is state:
                            _MAINTENANCE.pop(key, None)

            state["thread"] = threading.Thread(target=maintain, daemon=True, name="kr-task-maintenance")
            state["thread"].start()
            return state["stop"]

    def stop_maintenance(self):
        """停止本实例的维护定时器；不隐含取消已接受的执行。"""
        with _MAINTENANCE_LOCK:
            current = _MAINTENANCE.get(str(Path(self.store.db_path).resolve()))
            if current:
                current["stop"].set()
        if current:
            current["thread"].join(timeout=2)

    def maintain_once(self):
        """运行时有界维护入口；等待/归档失败不占采集和增强执行槽。"""
        try:
            from runtime.research_ledger import repair_pending_submissions
            repair_pending_submissions(limit=50)
            from runtime.research_ledger import repair_pending_projections
            repair_pending_projections(limit=20)
        except Exception:
            log.exception("synchronous research receipt maintenance failed")
        try:
            from runtime.research_archive import ResearchArchive
            from runtime.knowledge_delivery import DeliveryStore
            # 交付根来自运行配置；任务库可能在logs或显式外置目录，不能反推state。
            archive = ResearchArchive(DeliveryStore())
            key = str(archive.store.root.parent.resolve())
            if archive.store.db.is_file() and archive._has_contexts():
                with _MAINTENANCE_LOCK:
                    future=_ARCHIVE_FUTURES.get(key)
                    if future is None or future.done():
                        if future is not None and future.exception():
                            log.error("archive IO pass failed: %s",type(future.exception()).__name__)
                        _ARCHIVE_FUTURES[key]=_ARCHIVE_IO.submit(archive.maintain,limit=1)
        except Exception:
            log.exception("research archive maintenance failed; durable intent preserved")
        for task in self.store.managed_tasks(adapter="durable_process", statuses=("waiting_for_user",)):
            try:
                self._advance_waiting(task["task_id"])
            except Exception:
                log.exception("interaction maintenance failed for %s", task["task_id"])
        for task in self.store.managed_tasks(adapter='durable_process',statuses=('waiting_cooldown',)):
            try:self._advance_cooldown(task['task_id'])
            except Exception:log.exception('cooldown maintenance failed for %s',task['task_id'])
        for task in self.store.managed_tasks(adapter="durable_process",
                statuses=(*TERMINAL_STATUSES, "waiting_for_user"), recording_pending=True):
            try:
                self.record_completion(task["task_id"])
            except Exception:
                log.exception("completion maintenance failed for %s", task["task_id"])
        return self.store.cleanup_stale_tasks(reason="runtime_maintenance_stale_task_cleanup")

    def _advance_cooldown(self, task_id):
        task=self.store.get_task(task_id) or {}
        if task.get('status')!='waiting_cooldown':return
        meta=task.get('metadata') or {};now=time.time()
        if now>=float(meta.get('deadline_at',0)):
            self.store.mark_failed(task_id,'cooldown exceeded original task deadline',error_code='deadline_exceeded')
            self.record_completion(task_id);return
        if int(meta.get('cooldown_attempts',0))>=int(meta.get('cooldown_limit',3)):
            self.store.mark_failed(task_id,'bounded cooldown probes exhausted',error_code='cooldown_probe_exhausted')
            self.record_completion(task_id);return
        if now<float(meta.get('not_before',float('inf'))):return
        from runtime.leases import get_runtime_lease_coordinator, default_owner
        coordinator=get_runtime_lease_coordinator()
        grant=coordinator.acquire_exclusive('cooldown_probe',task.get('platform') or 'unknown',
            owner=default_owner('cooldown_probe',request_id=uuid.uuid4().hex),ttl_s=max(1,min(3600,float(meta['deadline_at'])-now)))
        if not grant.acquired:return
        try:
            folder=self.root/task_id
            job=json.loads((folder/'input.json').read_text(encoding='utf-8'))
            payload=job['payload']
            if job['operation']=='content_detail':payload.setdefault('options',{})['resume_checkpoint']=True
            if not self.store.claim_cooldown_probe(task_id,now=now):
                coordinator.release(grant.lease_id);return
            job['cooldown_probe_lease_id']=grant.lease_id
            self.store._mark(task_id,'queued',metadata={'cooldown_probe_lease_id':grant.lease_id,'owner_pid':os.getpid(),'owner_identity':process_identity()})
            _MONITORS.submit(self._monitor,job,folder,json.dumps(payload,ensure_ascii=False).encode())
        except Exception:
            coordinator.release(grant.lease_id)
            self.store.mark_failed(task_id,'cooldown dispatch failed',error_code='worker_launch_error')
            raise

    def _advance_waiting(self, task_id):
        task = self.store.get_task(task_id)
        if not task or task["status"] != "waiting_for_user":
            return
        deadline = float((task.get("metadata") or {}).get("deadline_at") or 0)
        if deadline and time.time() >= deadline:
            self.store.mark_failed(task_id, "interaction exceeded task deadline", error_code="deadline_exceeded")
            self.record_completion(task_id)
            return
        if not deadline:
            return  # 无期限的旧记录不能获得一个凭空重置的期限。
        from runtime.platform_execution import ensure_bindings
        from kr_core.registry import registry
        ensure_bindings()
        job = json.loads((self.root / task_id / "input.json").read_text(encoding="utf-8"))
        from runtime.leases import get_runtime_lease_coordinator, default_owner
        coordinator = get_runtime_lease_coordinator()
        grant = coordinator.acquire_exclusive("task_resume", task_id,
            owner=default_owner("resume_task", request_id=uuid.uuid4().hex), ttl_s=180)
        if not grant.acquired:
            return
        try:
            current = self.store.get_task(task_id)
            if not current or current["status"] != "waiting_for_user":
                return
            # 探测也归同一恢复租约；多进程不能同时消费一次人工恢复。
            # expired/none/blocked 都不是授权或认证成功，不能自动重发。
            if registry.interaction_status(job["payload"].get("platform", "")) != "ready":
                return
            current = self.store.get_task(task_id)
            if not current or current["status"] != "waiting_for_user":
                return
            remaining = float((current.get("metadata") or {}).get("deadline_at") or 0) - time.time()
            if remaining <= 0:
                self.store.mark_failed(task_id, "interaction exceeded task deadline", error_code="deadline_exceeded")
                self.record_completion(task_id)
                return
            successor = self.retry(task_id, timeout_s=remaining)
            updated = self.store.mark_skipped(task_id, "continued_after_interaction",
                metadata={"continuation_task_id": successor["task_id"]})
            if updated["status"] != "skipped":
                self.cancel(successor["task_id"])
        finally:
            coordinator.release(grant.lease_id)

    def _queue_enhancement(self, job, folder, execution_input, stage="enhancement"):
        with self._enhancement_condition:
            self._enhancement_pending[job["task_id"]] = (job, folder, execution_input, stage)
            if self._enhancement_dispatcher is None:
                self._enhancement_dispatcher = threading.Thread(target=self._dispatch_enhancements, daemon=True, name="kr-resource-dispatch")
                self._enhancement_dispatcher.start()
            self._enhancement_condition.notify()

    def _dispatch_enhancements(self):
        """只派发资源可用的工作；等待不占增强执行槽，不另建任务账本。"""
        from runtime.resource_concurrency import resource_available
        while True:
            with self._enhancement_condition:
                if not self._enhancement_pending and not self._enhancement_active:
                    self._enhancement_dispatcher = None
                    return
                self._enhancement_condition.wait(timeout=0.2 if self._enhancement_pending else None)
                for task_id, item in list(self._enhancement_pending.items()):
                    job, folder, execution_input, stage = item
                    current = self.store.get_task(task_id) or {}
                    if current.get("status") in TERMINAL_STATUSES or not current:
                        self._enhancement_pending.pop(task_id, None)
                        continue
                    if time.time() >= job["submitted_at"] + job["timeout_s"]:
                        self.store.mark_failed(task_id, "deadline expired waiting for resource", error_code="deadline_exceeded")
                        self._enhancement_pending.pop(task_id, None)
                        self.record_completion(task_id)
                        continue
                    resource = (current.get("metadata") or {}).get("waiting_resource", "")
                    if self._enhancement_active >= 2:
                        continue
                    try:
                        from kr_core.registry import registry
                        readiness = registry.resource_ready(current.get("platform", ""), resource) if resource else None
                        ready = not resource or (resource_available(resource) if readiness is None else readiness)
                    except Exception:
                        self.store.mark_failed(task_id, "resource coordinator unavailable", error_code="resource_coordinator_unavailable")
                        self._enhancement_pending.pop(task_id, None)
                        self.record_completion(task_id)
                        continue
                    if ready:
                        self._enhancement_pending.pop(task_id, None)
                        self._enhancement_active += 1
                        try:
                            _ENHANCEMENT_MONITORS.submit(self._run_enhancement, *item)
                        except Exception:
                            self._enhancement_active -= 1
                            self.store.mark_failed(task_id, "enhancement dispatch failed", error_code="worker_launch_error")
                            self.record_completion(task_id)

    def _run_enhancement(self, *args):
        try:
            self._monitor(*args)
        finally:
            with self._enhancement_condition:
                self._enhancement_active -= 1
                self._enhancement_condition.notify()

    def submit(self, operation, payload, *, timeout_s=600, scope=None, retry_of="", billing_scope_id=""):
        if operation not in {"research_query", "platform_discovery", "academic_fulltext", "academic_discovery", "content_detail", "optional_media", "fixture_echo", "fixture_sleep", "fixture_staged"}:
            raise ValueError("unsupported durable operation")
        if operation.startswith("fixture_") and os.environ.get("KR_TASK_TEST_MODE") != "1":
            raise ValueError("fixture worker is disabled outside isolated tests")
        if timeout_s <= 0 or timeout_s > 3600:
            raise ValueError("invalid task deadline")
        payload = json.loads(json.dumps(payload))
        # 入队前冻结有效语言；worker重启或环境变化不能改变同一任务的语言合同。
        from runtime.asr_policy import AsrPolicy
        if operation == 'content_detail' and payload.get('options', {}).get('include_transcript'):
            options = payload.setdefault('options', {})
            options['language'] = AsrPolicy.for_request(options.get('language')).language
        if operation == "content_detail":
            payload = json.loads(json.dumps(payload))
            payload.setdefault("options", {})["research_task_id"] = (scope or {}).get("research_task_id") or payload["options"].get("research_task_id", "")
            from runtime.platform_execution import ensure_bindings
            from kr_core.registry import registry
            from kr_core import DetailRequest
            from dataclasses import asdict
            ensure_bindings()
            payload = asdict(registry.prepare_detail(DetailRequest(**payload)))
        from generic_web.url_policy import public_evidence_payload
        # 临时凭据只在本次执行内存中传递，绝不进入任务文件或命令行。
        execution_input = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(execution_input) > 1024 * 1024:
            raise ValueError("task input exceeds bounded worker size")
        payload = public_evidence_payload(payload)
        scope = public_evidence_payload(scope or {})
        from runtime.research_queries import CURRENT_RESEARCH_ACTION
        action = CURRENT_RESEARCH_ACTION.get() or {}
        if scope.get('research_task_id') and action and action.get('_task_id', scope['research_task_id']) == scope['research_task_id']:
            scope.setdefault('research_action', {k:v for k,v in action.items() if not k.startswith('_')})
        from runtime.model_roles import MODEL_ENVIRONMENT
        identity = {"source": source_identity(), "python": sys.version,
                    "models": {k: value for k, value in MODEL_ENVIRONMENT.items() if k.endswith('_MODELS')}, "contract": 4}
        if operation == 'academic_fulltext':
            from runtime.pdf_text import PDF_PROCESSING_CONTRACT
            from runtime.model_roles import CLOUD_OCR_MODEL
            identity['academic_document'] = {'pdf_processing':PDF_PROCESSING_CONTRACT,
                'ocr_model':CLOUD_OCR_MODEL, 'ocr_configured':bool(os.environ.get('SILICONFLOW_API_KEY','').strip())}
        if operation in {"content_detail", "platform_discovery"}:
            from runtime.platform_execution import ensure_bindings
            from kr_core.registry import registry
            ensure_bindings()
            identity["platform_adapter"] = registry.runtime_identity(payload.get("platform", ""))
        if operation == "content_detail" and payload.get("options", {}).get("include_transcript"):
            from runtime.asset_identity import model_identity
            from runtime.asr_policy import AsrPolicy
            try:
                identity["asr_asset"] = model_identity(AsrPolicy.for_request(payload.get("options", {}).get("language")))
            except (ValueError, OSError, RuntimeError) as exc:
                # 模型未就绪只阻断增强，不阻断正文采集。
                identity["asr_asset"] = {"status": "not_ready", "reason": type(exc).__name__}
        if operation == "optional_media":
            from runtime.optional_media import validate_receipt, file_sha, receipt_root, select_component
            payload['component'] = select_component(payload)
            from runtime.model_roles import ASR_COMPONENT
            if payload['component'] == ASR_COMPONENT:
                from runtime.asr_policy import AsrPolicy
                payload['language'] = AsrPolicy.for_request(payload.get('language')).language
            execution_input = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            from runtime.model_roles import CLOUD_OCR_COMPONENT, CLOUD_OCR_MODEL
            if payload['component'] == CLOUD_OCR_COMPONENT:
                from runtime.cloud_ocr import ensure_ready
                ensure_ready()
                identity['cloud_ocr'] = {'provider': 'siliconflow', 'model': CLOUD_OCR_MODEL}
            elif payload['component'] == 'pypdf':
                from runtime.pdf_text import PDF_PROCESSING_CONTRACT
                identity['pdf_processing'] = {'contract': PDF_PROCESSING_CONTRACT,
                    'ocr_model': CLOUD_OCR_MODEL,
                    'ocr_key_available': bool(os.environ.get('SILICONFLOW_API_KEY', '').strip())}
            elif payload['component'] != 'pypdf':
                validate_receipt(payload['component'])
                identity["optional_install"] = file_sha(receipt_root() / (payload['component'] + ".json"))
                from runtime.model_roles import COMPONENTS
                identity["local_execution"] = {
                    "device": os.environ.get("KR_ASR_DEVICE", "cpu") if COMPONENTS[payload['component']]["kind"] == "asr"
                    else "cpu",
                    "schema": "local-evidence/v2",
                }
        # 宿主代际属于运行证据，不属于业务输入；同一完成请求可跨server重启复用。
        canonical_payload = json.loads(json.dumps(payload))
        canonical_payload.get("options", {}).get("task_scope", {}).pop("server_run_id", None)
        key = content_hash({"operation": operation, "payload": canonical_payload, "identity": identity,
                            "research_task_id": scope.get("research_task_id", ""), "retry_of": retry_of})
        task_id = "durable-" + key[:32]
        with _SUBMIT_LOCK:
            existing = self.store.get_task(task_id)
            if existing:
                if retry_of:
                    self.store.link_continuation(retry_of, task_id)
                self.start_maintenance()
                return existing
            active = self.store.managed_tasks(adapter="durable_process", statuses=("queued", "running"))
            if len(active) >= 24:
                raise RuntimeError("durable task admission full; retry later")
            folder = self.root / task_id
            try:
                folder.mkdir(parents=True, exist_ok=False)
            except FileExistsError as exc:
                raise RuntimeError("task reservation incomplete; inspect before retry") from exc
            job = {"schema": "knowledgeradar-durable-job/v1", "task_id": task_id, "attempt_id": uuid.uuid4().hex,
                   "operation": operation, "payload": payload, "identity": identity, "timeout_s": timeout_s,
                   "billing_scope_id": billing_scope_id or scope.get("research_task_id") or content_hash({"operation": operation, "payload": canonical_payload, "identity": identity}),
                   "submitted_at": time.time(), "db_path": self.store.db_path, "scope": scope or {}}
            from runtime.application import current_request_id
            job['origin_request_id'] = current_request_id()
            (folder / "input.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
            task = self.store.upsert_task(task_id, "durable_" + operation, payload.get("platform", "runtime"),
                    metadata={**(scope or {}), "adapter": "durable_process", "request_id": key, "origin_request_id": job['origin_request_id'], "attempt_id": job["attempt_id"],
                              "owner_pid": os.getpid(),
                              "owner_identity": process_identity(),
                              "source_identity": identity["source"], "deadline_at": job["submitted_at"] + timeout_s,
                              "phase": "queued", "blocks_final_report": True, "retry_of": retry_of})
            if retry_of:
                self.store.link_continuation(retry_of, task_id)
            if operation == "optional_media":
                self._queue_enhancement(job, folder, execution_input, "collection")
            else:
                try:
                    _MONITORS.submit(self._monitor, job, folder, execution_input)
                except Exception:
                    self.store.mark_failed(task_id, "collection dispatch failed", error_code="worker_launch_error")
                    self.record_completion(task_id)
                    self.start_maintenance()
                    return self.store.get_task(task_id)
            self.start_maintenance()
            return task

    def _monitor(self, job, folder, execution_input, stage="collection"):
        task_id = job["task_id"]
        deadline = job["submitted_at"] + job["timeout_s"]
        writer = {"attempt_id": job['attempt_id'], "execution_id": uuid.uuid4().hex}
        try:
            if time.time() >= deadline:
                self.store.mark_failed(task_id, "deadline expired in queue", error_code="deadline_exceeded")
                return
            if (self.store.get_task(task_id) or {}).get("status") in TERMINAL_STATUSES:
                return
            stop = threading.Event()
            def watch_deadline():
                while not stop.wait(.05):
                    current = self.store.get_task(task_id) or {}
                    if current.get('status') in TERMINAL_STATUSES:
                        return
                    if time.time() >= deadline:
                        self.store.mark_failed(task_id, 'task deadline exceeded', error_code='deadline_exceeded')
                        self._cancel_platform_operation(task_id)
                        return
            watcher = threading.Thread(target=watch_deadline, daemon=True)
            watcher.start()
            try:
                from runtime.task_process import execute_job
                execute_job(folder / 'input.json', execution_input=execution_input, stage=stage,
                            execution_id=writer['execution_id'])
            finally:
                stop.set()
                watcher.join(timeout=2)
            current = self.store.get_task(task_id) or {}
            metadata = current.get('metadata') or {}
            # 冷却可在旧monitor收尾前被下一轮接走；不能把后继执行标为中断。
            if metadata.get('execution_id') != writer['execution_id']:
                return
            if current.get('status') == 'queued' and metadata.get('phase') == 'cooldown_probe_queued':
                return
            if stage == "collection" and current.get("status") == "queued" and (current.get("metadata") or {}).get("phase") == "enhancement_queued":
                partial = self.result(task_id)
                if partial.get("status") in {"artifact_missing", "artifact_corrupt"}:
                    self.store.mark_failed(task_id, "collection handoff invalid", error_code="artifact_corrupt")
                    return
                next_input = json.dumps({"request": json.loads(execution_input), "collected": partial["result"]}, ensure_ascii=False).encode("utf-8")
                if len(next_input) > 1024 * 1024:
                    self.store.mark_failed(task_id, "enhancement input exceeds bound", error_code="enhancement_input_too_large")
                    return
                self._queue_enhancement(job, folder, next_input)
                return
            if current.get("status") == "queued" and (current.get("metadata") or {}).get("phase") == "waiting_resource":
                self._queue_enhancement(job, folder, execution_input, stage)
                return
            if current.get("status") not in {*TERMINAL_STATUSES, "waiting_for_user", "waiting_cooldown"}:
                self.store.mark_interrupted(task_id, "worker_exited_without_committed_result",
                                            metadata={**writer, "executor": "inprocess"})
        except Exception as exc:
            self.store.mark_failed(task_id, type(exc).__name__, error_code="worker_launch_error")
        finally:
            # 包括排队超时、启动异常和取消等无正文的终态。
            current = self.store.get_task(task_id) or {}
            probe_lease=job.get('cooldown_probe_lease_id')
            if probe_lease:
                from runtime.leases import get_runtime_lease_coordinator
                get_runtime_lease_coordinator().release(probe_lease)
            if current.get('status') in {*TERMINAL_STATUSES, 'waiting_for_user'}:
                self.record_completion(task_id)

    def _failure_context(self, task_id):
        row = self.store.get_task(task_id) or {}
        children = self.store.managed_tasks(parent_task_id=task_id)
        return {'worker_phase': (row.get('metadata') or {}).get('phase', ''), 'child_tasks': [
            {'task_id': child['task_id'], 'status': child['status'], 'phase': (child.get('metadata') or {}).get('phase', '')}
            for child in children]}

    def _settle_children(self, task_id):
        for child in self.store.managed_tasks(parent_task_id=task_id):
            if child['status'] not in TERMINAL_STATUSES:
                self.store.mark_interrupted(child['task_id'], 'owning durable worker stopped', metadata={'phase': 'interrupted'})

    def _cancel_platform_operation(self, task_id):
        try:
            task = self.store.get_task(task_id) or {}
            if not task.get('platform'):
                return None
            from runtime.platform_execution import ensure_bindings
            from kr_core.registry import registry
            ensure_bindings()
            return registry.cancel_task(task.get('platform', ''), task_id)
        except Exception as exc:
            return {'ok': False, 'code': 'platform_cancel_failed', 'error_type': type(exc).__name__}

    def result(self, task_id, *, _visited=None):
        """只读任务与已提交产物；不探测浏览器、不恢复、不补记。"""
        visited = set(_visited or ())
        if task_id in visited:
            return {"status": "artifact_corrupt", "task_id": task_id, "error": "continuation cycle"}
        visited.add(task_id)
        task = self.store.get_task(task_id)
        if not task:
            return {"status": "not_found", "task_id": task_id}
        continuation = (task.get("metadata") or {}).get("continuation_task_id")
        if continuation:
            return {**self.result(continuation, _visited=visited), "resumed_from": task_id}
        response = {"task": compact_task_ref(task), "status": task["status"]}
        progress = (task.get('metadata') or {}).get('media_progress')
        if progress:
            endpoint = task.get('finished_at') or time.time()
            response['media_progress'] = {**progress,
                'stage_elapsed_s': round(max(0, endpoint - progress.get('stage_started_at', endpoint)),3),
                'timing_basis':'last_worker_observation_and_task_clock',
                'partial_transcript_published':False}
        if task['status'] in {'failed','cancelled','interrupted'}:
            response['automatic_retry'] = False
            response['next_action'] = ('Inspect last stage and deadline; explicitly retry with an adequate budget if useful. '
                                       'A stopped enhancement is not still queued; collected material remains readable.')
        if task['status'] == 'failed' and task.get('error_code') in {
            'document_password_required', 'document_password_incorrect', 'document_corrupt'}:
            from runtime.pdf_text import DocumentError
            code = task['error_code']
            response['failure_type'] = code
            response['next_action'] = DocumentError(code).next_action
            response['error'] = {'type': code, 'failure_type': code, 'next_action': DocumentError(code).next_action,
                                 'ocr_attempted': False, 'retryable': False, 'automatic_retry': False}
        if (task.get('metadata') or {}).get('failure_context'):
            response['failure_context'] = task['metadata']['failure_context']
        if task.get("result_path"):
            path = Path(task["result_path"]).resolve()
            if not path.is_relative_to(self.root) or not path.is_file():
                return {**response, "status": "artifact_missing", "error": "committed artifact unavailable"}
            try:
                raw = path.read_bytes()
            except OSError as exc:
                return {**response, "status": "artifact_missing", "error": "committed artifact unreadable",
                        "error_type": type(exc).__name__}
            if hashlib.sha256(raw).hexdigest() != (task.get("metadata") or {}).get("artifact_sha256"):
                return {**response, "status": "artifact_corrupt", "error": "artifact integrity mismatch"}
            try:
                artifact = json.loads(raw.decode("utf-8"))
                if not isinstance(artifact, dict):
                    raise ValueError("invalid artifact envelope")
                metadata = task.get("metadata") or {}
                if (artifact.get('schema') != 'knowledgeradar-task-artifact/v1'
                        or artifact.get("task_id") != task_id or artifact.get("attempt_id") != metadata.get("attempt_id")
                        or (metadata.get('artifact_execution_id') and artifact.get('execution_id') != metadata['artifact_execution_id'])
                        or not isinstance(artifact.get('identity'), dict)
                        or artifact['identity'].get('source') != metadata.get('source_identity')):
                    raise ValueError("artifact identity mismatch")
                response["result"] = artifact["result"]
                if not isinstance(response["result"], dict):
                    raise ValueError("invalid artifact result")
            except (ValueError, KeyError, TypeError):
                return {**response, "status": "artifact_corrupt", "error": "artifact identity or schema mismatch"}
            response["partial_result"] = (task["status"] != "completed"
                or response["result"].get("enhancement_status") in {"queued", "partial"}
                or bool(response['result'].get('evidence_storage_gaps'))
                or (response['result'].get('content_coverage') or {}).get('request_satisfied') is False)
            if task['status'] in {'failed','cancelled','interrupted'}:
                # 只投影终态，不改写固定产物或把简介升为转写。
                import copy
                response['result'] = copy.deepcopy(response['result'])
                value = response['result']
                if value.get('enhancement_status') in {'queued','pending','running'}:
                    value.update(enhancement_status=task['status'], _enhancement_pending=False,
                                 enhancement_error_code=task.get('error_code') or task['status'])
                coverage = value.get('content_coverage') or {}
                for part in (coverage.get('parts') or {}).values():
                    if isinstance(part,dict) and part.get('status') in {'queued','pending','running'}:
                        part.update(status=task['status'], missing_reason=task.get('error_code') or task['status'])
                        coverage['request_satisfied'] = False
        recording = (task.get("metadata") or {}).get("research_recording") or {}
        if self._recording_matches(task, recording) and recording.get('handoff_version') == 1:
            response["research_receipt"] = recording["response"]
        elif task['status'] in {*TERMINAL_STATUSES, 'waiting_for_user'}:
            research_id = (task.get("metadata") or {}).get("research_task_id")
            if research_id:
                # 兼容旧任务；复用原账本的只读投影，不调用会创建锁文件的 read_task。
                from runtime.research_workspace import ResearchRecords
                try:
                    payload = ResearchRecords().snapshot(research_id)
                    suffix = ":waiting" if task['status'] == 'waiting_for_user' else ":result"
                    receipt = next((r for r in reversed(payload.get("tool_receipts", []))
                                    if r.get("trace_id") in {task_id + suffix, task_id + ":result:" + ("ok" if task['status'] == 'completed' else "failed")}), None)
                    response["research_receipt"] = {"status": ("recorded" if isinstance(receipt.get('candidate_association'), dict) else "receipt_only") if receipt else "not_recorded",
                        "research_task_id": research_id, **({"receipt": receipt} if receipt else {})}
                except (OSError, ValueError) as exc:
                    response["research_receipt"] = {"status": "unavailable", "error_type": type(exc).__name__}
        return response

    @staticmethod
    def _recording_matches(task, recording):
        return (recording.get("task_status") == task["status"]
                and recording.get("attempt_id") == (task.get("metadata") or {}).get("attempt_id")
                and recording.get("artifact_sha256") == (task.get("metadata") or {}).get("artifact_sha256", "")
                and isinstance(recording.get("response"), dict))

    def record_completion(self, task_id, *, research_task_ids=None):
        """执行侧收口/补偿；失败只记关联缺口，绝不把业务完成改成失败。"""
        task = self.store.get_task(task_id)
        response = self.result(task_id)
        if not task or task["status"] not in {*TERMINAL_STATUSES, "waiting_for_user"}:
            return response
        if (task.get("metadata") or {}).get("continuation_task_id"):
            return response
        if task["task_type"] == "durable_research_query":
            job = json.loads((self.root / task_id / "input.json").read_text(encoding="utf-8"))
            from runtime.research_index import ResearchIndex
            final = response.get("result") or {"error":task.get("error_code") or task["status"]}
            from runtime.research_index import query_outcome
            state = query_outcome(final) if task["status"]=="completed" else "failed"
            ResearchIndex().query(job["payload"]["research_task_id"],job["payload"]["family"],status=state,result=final)
        recording = (task.get("metadata") or {}).get("research_recording") or {}
        if (not research_task_ids and self._recording_matches(task, recording)
                and recording.get("status") == "recorded" and recording.get('handoff_version') == 1):
            return response
        research_ids = [*(research_task_ids or []), *recording.get('research_task_ids', []),
                        (task.get('metadata') or {}).get('research_task_id')]
        research_ids = list(dict.fromkeys(filter(None, research_ids)))
        if response["status"] in {"artifact_missing", "artifact_corrupt"}:
            # 损坏产物不伪造成功归档；修复后由显式 recover 重补，避免无限空转。
            self.store.record_result_association(task, {"status": response["status"],
                "handoff_version": 1, "response": {"status": "recording_blocked", "reason": response["status"]}})
            return response
        recordings = []
        for research_id in research_ids:
            from runtime.research_ledger import submit_tool_result
            result = response.get('result') or {'status': task['status'], 'error_code': task.get('error_code', ''),
                                               'failure_context': response.get('failure_context', {}), 'content_acquired': False}
            tool = "process_local_evidence" if task["task_type"] == "durable_optional_media" else "get_content_detail"
            if task['task_type'] == 'durable_academic_discovery':
                tool = 'search_academic'
            if task['task_type'] == 'durable_platform_discovery':
                discovery_job = json.loads((self.root / task_id / 'input.json').read_text(encoding='utf-8'))
                tool = discovery_job['payload']['tool']
            if task["task_type"] == "durable_research_query":
                tool = {"academic_literature_ecology":"search_academic", "github_repository_ecology":"search_github_repositories",
                    "youtube_video_ecology":"search_youtube", "wechat_public_article_ecology":"search_wechat_articles"}.get(job["payload"]["family"]["source_ecology"],"kr_web_search")
            status = ("ok" if not response.get('partial_result') else "partial") if task["status"] == "completed" else "failed" if task["status"] in TERMINAL_STATUSES else "partial"
            try:
                recorded = submit_tool_result(
                    task_id=research_id, trace_id=task_id + (":waiting" if task['status'] == 'waiting_for_user' else ":result"), tool=tool,
                    status=status, failure_code=task.get("error_code", ""),
                    result={**result, "task": compact_task_ref(task)},
                    association="explicit_background_task", receipt_kind="background_completion",
                    research_action=(task.get('metadata') or {}).get('research_action') if research_id == (task.get('metadata') or {}).get('research_task_id') else None)
                if recorded.get('reason') == 'receipt_identity_conflict' or (
                        recorded.get("receipt", {}).get("status") == "partial" and status != "partial"):
                    # 兼容旧等待记录及旧失败产物的终态投影，保留历史而非覆盖。
                    # 只补一个确定性身份；该身份仍冲突时保留缺口，不无限换ID。
                    recorded = submit_tool_result(
                        task_id=research_id, trace_id=task_id + ":result:" + status, tool=tool,
                        status=status, failure_code=task.get("error_code", ""),
                        result={**result, "task": compact_task_ref(task)},
                        association="explicit_background_task", receipt_kind="background_completion",
                        research_action=(task.get('metadata') or {}).get('research_action') if research_id == (task.get('metadata') or {}).get('research_task_id') else None)
            except Exception as exc:
                recorded = {'status': 'recording_failed', 'error_type': type(exc).__name__}
            response["research_receipt"] = recorded
            recordings.append(recorded)
        if recordings:
            failed = next((r for r in recordings if r["status"] != "recorded"), None)
            recorded = failed or recordings[-1]
            response["research_receipt"] = recorded
            self.store.record_result_association(task, {"status": recorded["status"], "response": recorded,
                "research_task_ids": research_ids, "handoff_version": 1})
        return response

    def _legacy_research_bindings(self, tasks):
        """只按旧工具回执的确切后台 ID 补归属，不按标题或时间猜测。"""
        from runtime.paths import runtime_state_dir
        from runtime.research_ledger import read_task
        wanted = {row['task_id'] for row in tasks if (row.get('metadata') or {}).get('adapter') == 'durable_process'
                  and not (row.get('metadata') or {}).get('research_task_id')}
        bindings = {}
        if not wanted:
            return bindings
        for path in (runtime_state_dir() / 'research_tasks').glob('*.json'):
            try:
                payload = read_task(task_id=path.stem)
            except (OSError, ValueError):
                continue
            for receipt in payload.get('tool_receipts', []):
                for task_id in wanted.intersection(receipt.get('background_task_ids', [])):
                    bindings.setdefault(task_id, set()).add(path.stem)
        return {key: sorted(values) for key, values in bindings.items()}

    def recover(self):
        interrupted = []
        recent = self.store.managed_tasks(adapter="durable_process")
        legacy = self._legacy_research_bindings(recent)
        for task in recent:
            meta = task.get("metadata") or {}
            research_ids = [meta["research_task_id"]] if meta.get("research_task_id") else legacy.get(task["task_id"], [])
            if meta.get("adapter") == "durable_process" and (task.get("result_path") or task['status'] in TERMINAL_STATUSES) and research_ids:
                self.record_completion(task["task_id"], research_task_ids=research_ids)
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
            self._settle_children(task['task_id'])
            if research_ids:
                self.record_completion(task['task_id'], research_task_ids=research_ids)
            interrupted.append(task["task_id"])
        return {"interrupted": interrupted, "automatic_reexecution": False}

    def retry(self, task_id, *, timeout_s=None):
        task = self.store.get_task(task_id) or {}
        continuation = (task.get('metadata') or {}).get('continuation_task_id')
        if continuation:
            return self.store.get_task(continuation) or task
        if task.get("status") not in {"failed", "cancelled", "interrupted", "waiting_for_user"}:
            return task
        if not task_id.startswith("durable-") or Path(task_id).name != task_id:
            raise ValueError("not a durable task")
        job = json.loads((self.root / task_id / "input.json").read_text(encoding="utf-8"))
        payload = job["payload"]
        if job["operation"] == "content_detail":
            payload.setdefault("options", {})["resume_checkpoint"] = True
        return self.submit(job["operation"], payload, timeout_s=timeout_s if timeout_s is not None else job["timeout_s"], scope=job["scope"], retry_of=task_id,
                           billing_scope_id=job.get("billing_scope_id") or task_id)

    def cancel(self, task_id):
        task = self.store.get_task(task_id) or {}
        continuation = (task.get("metadata") or {}).get("continuation_task_id")
        if continuation:
            return self.cancel(continuation)
        cancelled = self.store.cancel_task(task_id)
        if cancelled:
            self._cancel_platform_operation(task_id)
            self.record_completion(task_id)
        with self._enhancement_condition:
            self._enhancement_pending.pop(task_id, None)
            self._enhancement_condition.notify()
        return cancelled
