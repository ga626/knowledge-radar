"""Server-owned task scope helpers for fan-in and result reread."""

from __future__ import annotations

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
