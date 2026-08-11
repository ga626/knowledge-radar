"""Local async task adapter for long-running understanding jobs."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from .failure_tags import detect_failure_tags
from .resource_concurrency import acquire_resource, infer_task_resource
from .tasks import TaskStore, get_task_store


@dataclass(frozen=True)
class LocalTaskSpec:
    task_id: str
    task_type: str
    platform: str
    target: str = ""
    content_id: str = ""
    result_path: str = ""
    resource_kind: str = ""
    max_attempts: int = 2
    retry_interval_s: float = 3.0
    timeout_s: float = 600.0
    metadata: Optional[Dict[str, Any]] = None


class LocalTaskAdapter:
    """Thin adapter boundary; replace with Celery/Temporal later if needed."""

    def __init__(self, store: TaskStore | None = None) -> None:
        self.store = store or get_task_store()

    def submit(self, spec: LocalTaskSpec, fn: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
        existing = self.store.get_task(spec.task_id)
        if existing and existing.get("status") in {"queued", "running"}:
            return existing
        if existing and existing.get("status") == "completed":
            return existing

        task = self.store.upsert_task(
            task_id=spec.task_id,
            task_type=spec.task_type,
            platform=spec.platform,
            target=spec.target,
            content_id=spec.content_id,
            status="queued",
            max_attempts=spec.max_attempts,
            result_path=spec.result_path,
            metadata={
                **(spec.metadata or {}),
                "adapter": "local_thread",
                "resource_kind": infer_task_resource(spec.task_type, {"resource_kind": spec.resource_kind, **(spec.metadata or {})}),
                "timeout_s": spec.timeout_s,
            },
        )
        thread = threading.Thread(target=self._run, args=(spec, fn), daemon=True)
        thread.start()
        return task

    def submit_lifecycle(self, spec: LocalTaskSpec, fn: Callable[[], None]) -> Dict[str, Any]:
        """Start a task whose callable already marks running/completed/failed.

        This lets legacy platform paths move behind the adapter boundary without
        duplicating lifecycle transitions during the migration window.
        """
        existing = self.store.get_task(spec.task_id)
        if existing and existing.get("status") in {"running", "completed"}:
            return existing
        if (
            existing
            and existing.get("status") == "queued"
            and (existing.get("metadata") or {}).get("adapter") == "local_thread_lifecycle"
        ):
            return existing
        task = self.store.upsert_task(
            task_id=spec.task_id,
            task_type=spec.task_type,
            platform=spec.platform,
            target=spec.target,
            content_id=spec.content_id,
            status="queued",
            max_attempts=spec.max_attempts,
            result_path=spec.result_path,
            metadata={
                **(spec.metadata or {}),
                "adapter": "local_thread_lifecycle",
                "resource_kind": infer_task_resource(spec.task_type, {"resource_kind": spec.resource_kind, **(spec.metadata or {})}),
                "timeout_s": spec.timeout_s,
            },
        )
        thread = threading.Thread(target=self._run_lifecycle, args=(spec, fn), daemon=True)
        thread.start()
        return task

    def _run_lifecycle(self, spec: LocalTaskSpec, fn: Callable[[], None]) -> None:
        resource_kind = infer_task_resource(spec.task_type, {"resource_kind": spec.resource_kind, **(spec.metadata or {})})
        with acquire_resource(resource_kind) as resource_meta:
            self.store.heartbeat(spec.task_id, metadata={"phase": "resource_acquired", **resource_meta})
            fn()

    def _run(self, spec: LocalTaskSpec, fn: Callable[[], Dict[str, Any]]) -> None:
        started = time.time()
        last_error = ""
        resource_kind = infer_task_resource(spec.task_type, {"resource_kind": spec.resource_kind, **(spec.metadata or {})})
        for attempt in range(1, max(1, spec.max_attempts) + 1):
            if self._is_cancelled(spec.task_id):
                return
            if time.time() - started > spec.timeout_s:
                self.store.mark_failed(
                    spec.task_id,
                    "task timed out before next attempt",
                    result_path=spec.result_path,
                    error_code="timeout",
                    metadata={"attempt": attempt, "failure_tags": ["network_timeout"]},
                )
                return
            try:
                with acquire_resource(resource_kind) as resource_meta:
                    self.store.mark_running(
                        spec.task_id,
                        metadata={"attempt": attempt, "phase": "running", **resource_meta},
                    )
                    result = fn()
                if self._is_cancelled(spec.task_id):
                    return
                self.store.mark_completed(
                    spec.task_id,
                    result_path=spec.result_path,
                    metadata={
                        "attempt": attempt,
                        "elapsed_s": round(time.time() - started, 3),
                        **(result or {}),
                    },
                )
                return
            except Exception as exc:
                last_error = str(exc)
                tags = detect_failure_tags(type(exc).__name__, last_error)
                if attempt >= max(1, spec.max_attempts):
                    self.store.mark_failed(
                        spec.task_id,
                        last_error,
                        result_path=spec.result_path,
                        error_code=type(exc).__name__,
                        metadata={"attempt": attempt, "failure_tags": tags},
                    )
                    return
                self.store.heartbeat(
                    spec.task_id,
                    metadata={
                        "attempt": attempt,
                        "phase": "retry_wait",
                        "last_error": last_error,
                        "failure_tags": tags,
                    },
                )
                time.sleep(max(0.0, spec.retry_interval_s))

    def _is_cancelled(self, task_id: str) -> bool:
        task = self.store.get_task(task_id) or {}
        return task.get("status") == "cancelled"
