"""Server-owned task scope helpers for fan-in and result reread."""

from __future__ import annotations

from runtime.execution_context import execution_value
from dataclasses import asdict, dataclass
import hashlib
import os
import uuid
from typing import Any, Dict


SERVER_RUN_ID = os.environ.get("KR_SERVER_RUN_ID") or f"kr-run-{uuid.uuid4().hex[:12]}"
TASK_SCOPE_SCHEMA = "knowledgeradar-task-scope/v1"


def _short_hash(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:length]


@dataclass(frozen=True)
class TaskScope:
    """Stable server-owned grouping for background tasks.

    ``research_session_id`` is preserved only as a legacy alias. It is not a
    transport session and must not be used as a worker lifecycle key.
    """

    server_run_id: str
    work_scope_id: str
    task_scope_id: str
    scope_kind: str = "detail_request"
    source_url: str = ""
    content_id: str = ""
    research_session_id_alias: str = ""

    def to_metadata(self) -> Dict[str, Any]:
        data = asdict(self)
        data["task_scope_schema"] = TASK_SCOPE_SCHEMA
        if self.research_session_id_alias:
            data["research_session_id"] = self.research_session_id_alias
        return data

    def compact(self) -> Dict[str, Any]:
        return {
            "schema": TASK_SCOPE_SCHEMA,
            "server_run_id": self.server_run_id,
            "work_scope_id": self.work_scope_id,
            "task_scope_id": self.task_scope_id,
            "scope_kind": self.scope_kind,
            "source_url": self.source_url,
            "content_id": self.content_id,
            "legacy_alias": self.research_session_id_alias,
            "binding": "server_owned_scope",
        }


def make_task_scope(
    *,
    source_url: str = "",
    content_id: str = "",
    platform: str = "",
    scope_kind: str = "detail_request",
    work_scope_id: str = "",
    task_scope_id: str = "",
    research_session_id: str = "",
) -> TaskScope:
    source = str(source_url or "").strip()
    content = str(content_id or "").strip()
    platform_value = str(platform or "").strip()
    seed = "|".join([platform_value, source, content])
    if not seed.strip("|"):
        seed = uuid.uuid4().hex
    work = str(work_scope_id or "").strip() or f"kr-work-{_short_hash(seed)}"
    task_seed = "|".join([work, scope_kind, source, content])
    task = str(task_scope_id or "").strip() or f"kr-task-{_short_hash(task_seed)}"
    alias = str(research_session_id or "").strip() or work
    return TaskScope(
        server_run_id=SERVER_RUN_ID,
        work_scope_id=work,
        task_scope_id=task,
        scope_kind=scope_kind,
        source_url=source,
        content_id=content,
        research_session_id_alias=alias,
    )


def merge_scope_metadata(scope: TaskScope | Dict[str, Any] | None, metadata: Dict[str, Any] | None = None) -> Dict[str, Any]:
    result = dict(metadata or {})
    if isinstance(scope, TaskScope):
        scope_data = scope.to_metadata()
    elif isinstance(scope, dict):
        scope_data = dict(scope)
    else:
        scope_data = {}
    for key, value in scope_data.items():
        if value not in (None, ""):
            result.setdefault(key, value)
    return result


def attach_detail_task_scope(result: Dict, research_session_id: str, scope: Dict | None = None, *, store_factory=None) -> Dict:
    data = dict(result)
    scope_data = dict(scope or {})
    from runtime.tasks import get_task_store, compact_task_refs, TERMINAL_STATUSES
    data["server_run_id"] = scope_data.get("server_run_id") or SERVER_RUN_ID
    if scope_data:
        data["work_scope_id"] = scope_data.get("work_scope_id", "")
        data["task_scope_id"] = scope_data.get("task_scope_id", "")
        data["scope_binding"] = scope_data
    data["research_session_id"] = research_session_id
    if not (research_session_id or scope_data):
        return data
    try:
        store = (store_factory or get_task_store)()
        tasks = store.tasks_for_scope(
            task_scope_id=str(scope_data.get("task_scope_id") or ""),
            work_scope_id=str(scope_data.get("work_scope_id") or ""),
            blocking_only=False,
            include_terminal=False,
            limit=None,
        )
        if not tasks and research_session_id:
            tasks = store.tasks_for_session(research_session_id, blocking_only=False, include_terminal=False, limit=None)
        if execution_value("KR_DURABLE_WORKER") == "1":
            current_task_id = execution_value("KR_DURABLE_TASK_ID", "")
            tasks = [task for task in tasks if task.get("task_id") != current_task_id]
        pending = [task for task in tasks if str(task.get("status") or "") not in TERMINAL_STATUSES]
        blocking = [
            task
            for task in tasks
            if bool((task.get("metadata") or {}).get("blocks_final_report"))
            and str(task.get("status") or "") not in TERMINAL_STATUSES
        ]
        data["pending_tasks"] = compact_task_refs(pending, limit=20)
        data["blocking_tasks"] = compact_task_refs(blocking, limit=20)
    except Exception as exc:
        data["pending_tasks_error"] = str(exc)[:160]
    return data
