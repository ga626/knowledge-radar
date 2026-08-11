"""Worker-mode helpers for long-running KnowledgeRadar tasks."""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Any, Dict

from runtime.task_scope import SERVER_RUN_ID
from runtime.tasks import DEFAULT_STALE_QUEUED_SECONDS, DEFAULT_STALE_RUNNING_SECONDS, TaskStore, get_task_store


def worker_mode() -> str:
    return (os.environ.get("KR_TASK_WORKER_MODE") or "local_thread_sqlite").strip() or "local_thread_sqlite"


def cleanup_stale_worker_tasks(
    store: TaskStore | None = None,
    *,
    running_seconds: int | None = None,
    queued_seconds: int | None = None,
) -> Dict[str, Any]:
    task_store = store or get_task_store()
    return task_store.cleanup_stale_tasks(
        running_seconds=running_seconds,
        queued_seconds=queued_seconds,
        reason="worker_startup_stale_task_cleanup",
    )


def task_worker_summary(store: TaskStore | None = None) -> Dict[str, Any]:
    task_store = store or get_task_store()
    try:
        summary = task_store.summary(recent_limit=5)
    except Exception as exc:
        return {
            "schema": "knowledgeradar-task-worker/v1",
            "status": "degraded",
            "mode": worker_mode(),
            "error": str(exc),
        }
    mode = worker_mode()
    os_name = platform.system()
    return {
        "schema": "knowledgeradar-task-worker/v1",
        "status": "ok" if os_name == "Windows" else "degraded",
        "mode": mode,
        "server_run_id": SERVER_RUN_ID,
        "adapter": "runtime.task_adapter.LocalTaskAdapter",
        "store": {
            "type": "sqlite",
            "path_role": "KR_TASK_DB_PATH",
            "name": Path(str(getattr(task_store, "db_path", ""))).name,
        },
        "protocol": {
            "task_scope_fanin": True,
            "legacy_research_session_id_alias": True,
            "compact_ref": True,
            "final_fanin_wait": True,
            "result_reread": True,
            "heartbeat": True,
            "cancel": True,
            "retry": True,
            "stale_cleanup": True,
        },
        "stale_policy": {
            "running_seconds": DEFAULT_STALE_RUNNING_SECONDS,
            "queued_seconds": DEFAULT_STALE_QUEUED_SECONDS,
            "startup_action": "cancel stale tasks; caller may resubmit from source detail path",
        },
        "platform_policy": {
            "primary": "Windows first",
            "current_os": os_name,
            "non_windows": "degraded; future support only",
        },
        "summary": summary,
    }
