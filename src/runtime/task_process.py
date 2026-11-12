"""持久任务的库内执行；原子写artifact后CAS提交成功状态。"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid

from runtime.tasks import TaskStore, TERMINAL_STATUSES
from runtime.process_identity import process_identity


def execute_job(input_path, *, execution_input=None, stage="collection", execution_id=None):
    folder = input_path.resolve().parent
    job = json.loads(input_path.read_text(encoding="utf-8"))
    job['payload'] = json.loads(execution_input) if execution_input is not None else job['payload']
    collected = None
    if stage == 'enhancement':
        collected = job['payload']['collected']
        job['payload'] = job['payload']['request']
    store = TaskStore(job["db_path"])
    task_id = job["task_id"]
    writer = {"attempt_id": job["attempt_id"], "execution_id": execution_id or uuid.uuid4().hex}
    if not store.claim_queued(task_id, {**writer, "worker_pid": os.getpid(), "worker_identity": process_identity(), "phase": "worker_start", "stage": stage}):
        return
    stop = threading.Event()
    def phase(name):
        with (folder / "spans.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": time.time(), "phase": name, "attempt_id": job["attempt_id"],
                                "origin_request_id": job.get("origin_request_id", "")}) + "\n")
        store.heartbeat(task_id, {**writer, "phase": name, "worker_pid": os.getpid()})
    def heartbeat():
        while not stop.wait(1):
            store.heartbeat(task_id, {**writer, "worker_pid": os.getpid()})
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    from runtime.execution_context import execution_scope, check_execution
    context = execution_scope({'store': store, **writer,
        'deadline_at': job.get('submitted_at', time.time()) + job.get('timeout_s', 600),
        'KR_DURABLE_WORKER': '1', 'KR_DURABLE_TASK_ID': task_id,
        'KR_TASK_DB_PATH': store.db_path, 'KR_PAID_TASK_SCOPE': job.get('billing_scope_id') or task_id})
    context.__enter__()
    from runtime.application import request_scope
    origin = request_scope(job.get('origin_request_id', ''))
    origin.__enter__()
    from runtime.research_queries import CURRENT_RESEARCH_ACTION
    action=(job.get('scope') or {}).get('research_action') or {}
    research_token = CURRENT_RESEARCH_ACTION.set({**action,'_task_id':(job.get('scope') or {}).get('research_task_id','')} if action else {})
    try:
        check_execution()
        phase("worker_imports_begin")
        operation = job["operation"]
        payload = job["payload"]
        from runtime.tool_trace import set_current_tool_trace
        previous_trace = __import__("runtime.tool_trace", fromlist=["current_tool_trace"]).current_tool_trace()
        set_current_tool_trace({"trace_id": job["attempt_id"], "origin_request_id": job.get('origin_request_id', ''),
                               "research_task_id": job.get("scope", {}).get("research_task_id", "")})
        if operation == "content_detail":
            from runtime.platform_execution import worker_detail
            from kr_core import DetailRequest
            phase("worker_imports_end")
            phase("detail_execute_begin")
            from runtime.platform_execution import ensure_bindings
            from kr_core.registry import registry
            ensure_bindings()
            if stage == "collection" and registry.supports_staged_detail(payload.get("platform", "")):
                payload.setdefault("options", {})["_detail_stage"] = "collection"
            result = worker_detail(DetailRequest(**payload), collected=collected)
        elif operation == "research_query":
            from runtime.composition import execute_research_query
            phase("research_query_begin")
            result = execute_research_query(payload)
        elif operation == 'platform_discovery':
            from runtime.composition import execute_platform_discovery
            phase('platform_discovery_begin')
            result = (payload['initial_result'] if not (store.get_task(task_id).get('metadata') or {}).get('cooldown_attempts')
                      else execute_platform_discovery(payload))
        elif operation == 'academic_discovery':
            from use_cases.academic_discovery import execute
            phase('academic_discovery_begin')
            result = execute(payload)
        elif operation == 'academic_fulltext':
            from use_cases.academic_fulltext import execute
            phase('academic_fulltext_begin')
            result = execute(payload)
        elif operation == "optional_media":
            from runtime.optional_media import run_optional
            phase("inprocess_optional_begin")
            result = run_optional(payload)
        elif operation in {"fixture_echo", "fixture_sleep", "fixture_staged"} and os.environ.get("KR_TASK_TEST_MODE") == "1":
            phase("fixture_begin")
            from contextlib import nullcontext
            from runtime.resource_concurrency import acquire_resource
            resource = payload.get("enhancement_resource") if stage == "enhancement" else ""
            with acquire_resource(resource) if resource else nullcontext():
                until = time.monotonic() + float(payload.get("enhancement_sleep" if stage == "enhancement" else "sleep", 0))
                while time.monotonic() < until:
                    check_execution()
                    time.sleep(min(.02, max(0, until - time.monotonic())))
            result = {"value": payload.get("value", "fixture"), "completed": True}
            if operation == "fixture_staged":
                result.update(_enhancement_pending=stage == "collection", enhancement_status="queued" if stage == "collection" else "ok")
                if stage == "enhancement" and payload.get("fail_enhancement"):
                    raise ValueError("fixture enhancement failure")
            if payload.get("required_token_sha256"):
                from urllib.parse import parse_qs, urlsplit
                token = parse_qs(urlsplit(payload["value"]["url"]).query).get("xsec_token", [""])[0]
                result["token_received"] = hashlib.sha256(token.encode()).hexdigest() == payload["required_token_sha256"]
        else:
            raise ValueError("unsupported worker operation")
        check_execution()
        phase("detail_execute_end")
        if isinstance(result, dict):
            # scope快照在本任务运行期间生成；自身不是自己的依赖。
            result = dict(result)
            for key in ("pending_tasks", "blocking_tasks"):
                if isinstance(result.get(key), list):
                    result[key] = [ref for ref in result[key] if ref.get("task_id") != task_id]
        if (store.get_task(task_id) or {}).get("status") in TERMINAL_STATUSES:
            return
        from kr_core.source_urls import public_result_payload
        artifact = {"schema": "knowledgeradar-task-artifact/v1", "task_id": task_id, **writer,
                    "origin_request_id": job.get('origin_request_id', ''),
                    "identity": job["identity"], "result": public_result_payload(result)}
        raw = json.dumps(artifact, ensure_ascii=False, sort_keys=True).encode("utf-8")
        pending_enhancement = stage == "collection" and isinstance(result, dict) and result.get("_enhancement_pending") is True
        name = ("collection-" if pending_enhancement else "result-") + writer["execution_id"]
        temporary = folder / (name + ".pending")
        with temporary.open("xb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        destination = folder / (name + ".json")
        temporary.replace(destination)
        phase("artifact_atomic_commit")
        metadata = {**writer, "artifact_sha256": hashlib.sha256(raw).hexdigest(),
                    "artifact_execution_id": writer['execution_id']}
        from runtime.task_cooldown import cooldown_plan
        previous=store.get_task(task_id) or {}
        cooldown=cooldown_plan(result,operation=operation,attempts=int((previous.get('metadata') or {}).get('cooldown_attempts',0))) if stage=='collection' else None
        if pending_enhancement:
            store.queue_enhancement(task_id, job["attempt_id"], result_path=str(destination), artifact_sha256=metadata["artifact_sha256"], execution_id=writer["execution_id"])
        elif cooldown:
            if cooldown['cooldown_attempts']>=cooldown['cooldown_limit']:
                store.mark_failed(task_id,'bounded cooldown probes exhausted',result_path=str(destination),error_code='cooldown_probe_exhausted',metadata={**metadata,**cooldown,'phase':'cooldown_exhausted'})
            else:
                store.mark_waiting_cooldown(task_id,{**metadata,**cooldown,'phase':'waiting_cooldown'},result_path=str(destination))
        elif isinstance(result, dict) and result.get("resume_policy") != "stop_no_auto_resume" and (result.get("manual_action_required") or result.get("status") == "needs_interaction"):
            store.mark_waiting_for_user(task_id, {**metadata, "phase": "waiting_for_user", "resume_policy": "explicit_retry"}, result_path=str(destination))
        elif isinstance(result, dict) and (result.get("error") or result.get("failure_type") or result.get("status") in {"failed", "error", "empty"} or result.get("blocking_tasks")):
            failure_code = result.get('failure_type') or ('dependency_pending' if result.get('blocking_tasks') else 'detail_failed')
            store.mark_failed(task_id, "detail returned structured error", result_path=str(destination),
                              error_code=str(failure_code),
                              metadata={**metadata, "phase": "failed"})
        elif isinstance(result, dict) and result.get("evidence_storage_gaps"):
            store.mark_failed(task_id, "evidence archive incomplete", result_path=str(destination),
                              error_code="evidence_archive_incomplete", metadata={**metadata, "phase": "archive_incomplete"})
        elif isinstance(result, dict) and result.get("enhancement_status") == "partial":
            failure_code = "enhancement_incomplete"
            for key in ("ocr", "transcript_result"):
                enhancement = result.get(key) or {}
                for row in [enhancement, *enhancement.get("results", [])]:
                    reason = str(row.get("reason") or row.get("error_code") or "")
                    if reason.startswith("paid_"):
                        failure_code = reason
                        break
                if failure_code.startswith("paid_"):
                    break
            store.mark_failed(task_id, "required enhancement incomplete", result_path=str(destination),
                              error_code=failure_code, metadata={**metadata, "phase": "enhancement_incomplete"})
        elif isinstance(result, dict) and (result.get('content_coverage') or {}).get('required_gaps'):
            store.mark_failed(task_id, 'required evidence not acquired', result_path=str(destination),
                              error_code='required_evidence_incomplete', metadata={**metadata, 'phase': 'coverage_incomplete'})
        else:
            store.mark_completed(task_id, result_path=str(destination), metadata={**metadata, "phase": "completed", "stage": "complete"})
    except Exception as exc:
        from runtime.resource_concurrency import ResourceDeferred
        if isinstance(exc, ResourceDeferred):
            store.queue_resource_wait(task_id, job["attempt_id"], exc.resource_kind, execution_id=writer["execution_id"])
            return
        code = getattr(exc, "error_code", "worker_execution_error")
        message = str(exc) if code in {'unsupported_language', 'asr_language_detection_failed'} else type(exc).__name__
        diagnostic = {"failure_type": code, "next_action": exc.next_action, "ocr_attempted": False} if code in {
            'document_password_required', 'document_password_incorrect', 'document_corrupt'} else {}
        store.mark_failed(task_id, message, error_code=code, metadata={**writer, "phase": "failed", **diagnostic})
    finally:
        CURRENT_RESEARCH_ACTION.reset(research_token)
        stop.set()
        thread.join(timeout=2)
        if "previous_trace" in locals():
            set_current_tool_trace(previous_trace)
        origin.__exit__(None, None, None)
        context.__exit__(None, None, None)
