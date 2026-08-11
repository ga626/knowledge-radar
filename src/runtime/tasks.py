"""SQLite-backed runtime task registry for long-running understanding jobs."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Dict, Iterator, Optional

from runtime.paths import runtime_log_dir
from runtime.status_schema import normalize_error_code
from runtime.task_scope import SERVER_RUN_ID


TASK_STATUSES = ("queued", "running", "completed", "failed", "skipped", "cancelled")
TERMINAL_STATUSES = ("completed", "failed", "skipped", "cancelled")


def _default_runtime_dir() -> str:
    return str(runtime_log_dir())


def default_task_db_path() -> str:
    return os.environ.get("KR_TASK_DB_PATH") or os.path.join(_default_runtime_dir(), "knowledgeradar-tasks.sqlite3")


def now_ts() -> float:
    return time.time()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except Exception:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


DEFAULT_STALE_RUNNING_SECONDS = _env_int("KR_TASK_STALE_RUNNING_SECONDS", 1800)
DEFAULT_STALE_QUEUED_SECONDS = _env_int("KR_TASK_STALE_QUEUED_SECONDS", 900)
COMPACT_SCHEMA_VERSION = "knowledgeradar-runtime-task-ref/v1"


def _json_dumps(value: Optional[Dict]) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)


def _json_loads(value: str | None) -> Dict:
    if not value:
        return {}
    try:
        data = json.loads(value)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _safe_metadata_value(metadata: Dict[str, Any], *names: str) -> str:
    for name in names:
        value = metadata.get(name)
        if value is None:
            continue
        text = str(value)
        if text:
            return text
    return ""


def _result_ref(result_path: str) -> Dict[str, Any]:
    if not result_path:
        return {"available": False}
    path = Path(result_path)
    ref: Dict[str, Any] = {
        "available": path.is_file(),
        "name": path.name,
    }
    try:
        ref["size_bytes"] = path.stat().st_size if path.is_file() else 0
    except OSError:
        ref["size_bytes"] = 0
    return ref


def _detail_reread_ref(task: Dict[str, Any], metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Reference a logical result that is available by rereading source detail."""

    ref = _result_ref(str(task.get("result_path") or ""))
    if ref.get("available"):
        return ref
    status = str(task.get("status") or "")
    reread_tool = str(metadata.get("result_reread_tool") or "").strip()
    source_url = str(metadata.get("source_url") or "").strip()
    content_id = str(task.get("content_id") or metadata.get("content_id") or "").strip()
    if status == "completed" and reread_tool and (source_url or content_id):
        logical_ref: Dict[str, Any] = {
            "available": True,
            "kind": "detail_reread",
            "tool": reread_tool,
        }
        if source_url:
            logical_ref["source_url"] = source_url[:240]
        if content_id:
            logical_ref["content_id"] = content_id
        return logical_ref
    return ref


def _compact_timing(metadata: Dict[str, Any]) -> Dict[str, Any]:
    names = (
        "subtitle_probe_s",
        "download_s",
        "model_load_s",
        "transcribe_s",
        "total_s",
    )
    timing: Dict[str, Any] = {}
    for name in names:
        value = metadata.get(name)
        if value is None:
            continue
        try:
            timing[name] = round(float(value), 3)
        except Exception:
            continue
    return timing


def _compact_fanin(metadata: Dict[str, Any]) -> Dict[str, Any]:
    allowed = (
        "server_run_id",
        "work_scope_id",
        "task_scope_id",
        "scope_kind",
        "research_session_id_alias",
        "research_session_id",
        "blocks_final_report",
        "result_reread_tool",
        "source_url",
        "content_id",
        "approach",
        "cache_hit",
        "subtitle_hit",
        "result_path",
        "device",
        "compute_type",
        "model",
        "vad_enabled",
        "beam_size",
        "resource_kind",
        "limit",
    )
    fanin: Dict[str, Any] = {}
    for name in allowed:
        value = metadata.get(name)
        if value in (None, ""):
            continue
        if name == "source_url":
            fanin[name] = str(value)[:240]
        elif name == "result_path":
            fanin["result_ref"] = _result_ref(str(value))
        elif name == "research_session_id":
            fanin["legacy_research_session_id"] = value
        else:
            fanin[name] = value
    return fanin


def compact_task_ref(task: Optional[Dict[str, Any]], *, now: float | None = None) -> Dict[str, Any]:
    """Return the agent-facing task reference without large metadata payloads."""
    if not task:
        return {}
    ts = now if now is not None else now_ts()
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    created_at = float(task.get("created_at") or 0)
    updated_at = float(task.get("updated_at") or 0)
    started_at = task.get("started_at")
    finished_at = task.get("finished_at")
    status = str(task.get("status") or "")
    error = str(task.get("error") or "")
    reason = error or _safe_metadata_value(metadata, "cleanup_reason", "reason", "phase", "last_error")
    next_action = "poll_get_task_status" if status in {"queued", "running"} else "read_result_ref"
    if status in {"failed", "cancelled", "skipped"}:
        next_action = "inspect_error"
    compact = {
        "schema_version": COMPACT_SCHEMA_VERSION,
        "task_id": task.get("task_id"),
        "task_type": task.get("task_type"),
        "platform": task.get("platform"),
        "status": status,
        "target": task.get("target") or "",
        "content_id": task.get("content_id") or "",
        "created_at": created_at,
        "updated_at": updated_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "age_s": round(max(0.0, ts - created_at), 3) if created_at else 0,
        "updated_age_s": round(max(0.0, ts - updated_at), 3) if updated_at else 0,
        "attempts": int(task.get("attempts") or 0),
        "retry_count": int(task.get("retry_count") or 0),
        "max_attempts": int(task.get("max_attempts") or 1),
        "error_code": task.get("error_code") or "",
        "reason": reason[:200],
        "result": _detail_reread_ref(task, metadata),
        "poll_after_seconds": 3 if status in {"queued", "running"} else 0,
        "recommended_next_action": next_action,
    }
    timing = _compact_timing(metadata)
    if timing:
        compact["timing"] = timing
    fanin = _compact_fanin(metadata)
    if fanin:
        compact["fanin"] = fanin
    if compact["result"].get("available") and not compact.setdefault("fanin", {}).get("result_ref"):
        compact["fanin"]["result_ref"] = compact["result"]
    if task.get("content_id") and "content_id" not in compact.get("fanin", {}):
        compact.setdefault("fanin", {})["content_id"] = task.get("content_id")
    return compact


def compact_task_refs(tasks: list[Dict[str, Any]], *, limit: int | None = None) -> list[Dict[str, Any]]:
    selected = tasks[: max(0, int(limit))] if limit is not None else tasks
    ts = now_ts()
    return [compact_task_ref(task, now=ts) for task in selected if task]


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    task_type: str
    platform: str
    status: str
    target: str
    content_id: str
    created_at: float
    updated_at: float
    started_at: Optional[float]
    finished_at: Optional[float]
    attempts: int
    max_attempts: int
    result_path: str
    error: str
    error_code: str
    metadata: Dict

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TaskRecord":
        return cls(
            task_id=row["task_id"],
            task_type=row["task_type"],
            platform=row["platform"],
            status=row["status"],
            target=row["target"],
            content_id=row["content_id"] if "content_id" in row.keys() else "",
            created_at=float(row["created_at"] or 0),
            updated_at=float(row["updated_at"] or 0),
            started_at=float(row["started_at"]) if row["started_at"] is not None else None,
            finished_at=float(row["finished_at"]) if row["finished_at"] is not None else None,
            attempts=int(row["attempts"] or 0),
            max_attempts=int(row["max_attempts"] or 1),
            result_path=row["result_path"] or "",
            error=row["error"] or "",
            error_code=row["error_code"] if "error_code" in row.keys() else "",
            metadata=_json_loads(row["metadata_json"]),
        )

    def to_dict(self) -> Dict:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "platform": self.platform,
            "status": self.status,
            "target": self.target,
            "content_id": self.content_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "attempts": self.attempts,
            "retry_count": max(0, self.attempts - 1),
            "max_attempts": self.max_attempts,
            "result_path": self.result_path,
            "error": self.error,
            "error_code": self.error_code,
            "metadata": self.metadata,
            "server_run_id": self.metadata.get("server_run_id", ""),
            "work_scope_id": self.metadata.get("work_scope_id", ""),
            "task_scope_id": self.metadata.get("task_scope_id", ""),
            "source_url": self.metadata.get("source_url", ""),
        }


class TaskStore:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or default_task_db_path()
        self._lock = threading.RLock()
        self._initialized = False
        self._wal_enabled = _env_bool("KR_TASK_DB_WAL", True)
        self._journal_mode = ""

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        if self._wal_enabled:
            try:
                row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
                self._journal_mode = str(row[0] if row else "wal").lower()
            except Exception:
                self._journal_mode = "unavailable"
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS runtime_tasks (
                        task_id TEXT PRIMARY KEY,
                        task_type TEXT NOT NULL,
                        platform TEXT NOT NULL,
                        status TEXT NOT NULL,
                        target TEXT NOT NULL DEFAULT '',
                        content_id TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        started_at REAL,
                        finished_at REAL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        max_attempts INTEGER NOT NULL DEFAULT 1,
                        result_path TEXT NOT NULL DEFAULT '',
                        error TEXT NOT NULL DEFAULT '',
                        error_code TEXT NOT NULL DEFAULT '',
                        metadata_json TEXT NOT NULL DEFAULT '{}'
                    )
                    """
                )
                self._ensure_column(conn, "content_id", "TEXT NOT NULL DEFAULT ''")
                self._ensure_column(conn, "error_code", "TEXT NOT NULL DEFAULT ''")
                self._ensure_column(conn, "server_run_id", "TEXT NOT NULL DEFAULT ''")
                self._ensure_column(conn, "work_scope_id", "TEXT NOT NULL DEFAULT ''")
                self._ensure_column(conn, "task_scope_id", "TEXT NOT NULL DEFAULT ''")
                self._ensure_column(conn, "source_url", "TEXT NOT NULL DEFAULT ''")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_runtime_tasks_status ON runtime_tasks(status)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_runtime_tasks_updated ON runtime_tasks(updated_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_runtime_tasks_content_id ON runtime_tasks(content_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_runtime_tasks_server_run ON runtime_tasks(server_run_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_runtime_tasks_work_scope ON runtime_tasks(work_scope_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_runtime_tasks_task_scope ON runtime_tasks(task_scope_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_runtime_tasks_source_url ON runtime_tasks(source_url)")
            self._initialized = True

    def _ensure_column(self, conn: sqlite3.Connection, name: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(runtime_tasks)").fetchall()}
        if name not in columns:
            conn.execute(f"ALTER TABLE runtime_tasks ADD COLUMN {name} {definition}")

    def upsert_task(
        self,
        task_id: str,
        task_type: str,
        platform: str,
        target: str = "",
        content_id: str = "",
        status: str = "queued",
        max_attempts: int = 1,
        result_path: str = "",
        metadata: Optional[Dict] = None,
    ) -> Dict:
        self.initialize()
        ts = now_ts()
        metadata = dict(metadata or {})
        server_run_id = str(metadata.get("server_run_id") or SERVER_RUN_ID or "")
        work_scope_id = str(metadata.get("work_scope_id") or "")
        task_scope_id = str(metadata.get("task_scope_id") or "")
        source_url = str(metadata.get("source_url") or target or "")
        if server_run_id:
            metadata.setdefault("server_run_id", server_run_id)
        if work_scope_id:
            metadata.setdefault("work_scope_id", work_scope_id)
        if task_scope_id:
            metadata.setdefault("task_scope_id", task_scope_id)
        if source_url:
            metadata.setdefault("source_url", source_url)
        if content_id:
            metadata.setdefault("content_id", content_id)
        metadata_json = _json_dumps(metadata)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runtime_tasks (
                    task_id, task_type, platform, status, target, content_id, created_at, updated_at,
                    attempts, max_attempts, result_path, error, error_code, server_run_id, work_scope_id,
                    task_scope_id, source_url, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, '', '', ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    task_type=excluded.task_type,
                    platform=excluded.platform,
                    status=CASE
                        WHEN runtime_tasks.status IN ('failed', 'cancelled', 'skipped') AND excluded.status = 'queued'
                            THEN excluded.status
                        ELSE runtime_tasks.status
                    END,
                    target=excluded.target,
                    content_id=CASE WHEN excluded.content_id != '' THEN excluded.content_id ELSE runtime_tasks.content_id END,
                    updated_at=excluded.updated_at,
                    started_at=CASE
                        WHEN runtime_tasks.status IN ('failed', 'cancelled', 'skipped') AND excluded.status = 'queued'
                            THEN NULL
                        ELSE runtime_tasks.started_at
                    END,
                    finished_at=CASE
                        WHEN runtime_tasks.status IN ('failed', 'cancelled', 'skipped') AND excluded.status = 'queued'
                            THEN NULL
                        ELSE runtime_tasks.finished_at
                    END,
                    attempts=CASE
                        WHEN runtime_tasks.status IN ('failed', 'cancelled', 'skipped') AND excluded.status = 'queued'
                            THEN 0
                        ELSE runtime_tasks.attempts
                    END,
                    max_attempts=excluded.max_attempts,
                    result_path=CASE WHEN excluded.result_path != '' THEN excluded.result_path ELSE runtime_tasks.result_path END,
                    error=CASE
                        WHEN runtime_tasks.status IN ('failed', 'cancelled', 'skipped') AND excluded.status = 'queued'
                            THEN ''
                        ELSE runtime_tasks.error
                    END,
                    error_code=CASE
                        WHEN runtime_tasks.status IN ('failed', 'cancelled', 'skipped') AND excluded.status = 'queued'
                            THEN ''
                        ELSE runtime_tasks.error_code
                    END,
                    server_run_id=CASE WHEN excluded.server_run_id != '' THEN excluded.server_run_id ELSE runtime_tasks.server_run_id END,
                    work_scope_id=CASE WHEN excluded.work_scope_id != '' THEN excluded.work_scope_id ELSE runtime_tasks.work_scope_id END,
                    task_scope_id=CASE WHEN excluded.task_scope_id != '' THEN excluded.task_scope_id ELSE runtime_tasks.task_scope_id END,
                    source_url=CASE WHEN excluded.source_url != '' THEN excluded.source_url ELSE runtime_tasks.source_url END,
                    metadata_json=excluded.metadata_json
                """,
                (
                    task_id,
                    task_type,
                    platform,
                    status,
                    target,
                    content_id,
                    ts,
                    ts,
                    max_attempts,
                    result_path,
                    server_run_id,
                    work_scope_id,
                    task_scope_id,
                    source_url,
                    metadata_json,
                ),
            )
        return self.get_task(task_id) or {}

    def mark_running(self, task_id: str, metadata: Optional[Dict] = None) -> Dict:
        return self._mark(task_id, "running", started=True, increment_attempt=True, metadata=metadata)

    def mark_completed(self, task_id: str, result_path: str = "", metadata: Optional[Dict] = None) -> Dict:
        return self._mark(task_id, "completed", finished=True, result_path=result_path, metadata=metadata)

    def mark_failed(self, task_id: str, error: str, result_path: str = "", metadata: Optional[Dict] = None, error_code: str = "") -> Dict:
        error_code = error_code or normalize_error_code(error)
        return self._mark(task_id, "failed", finished=True, error=error, error_code=error_code, result_path=result_path, metadata=metadata)

    def mark_skipped(self, task_id: str, reason: str, metadata: Optional[Dict] = None) -> Dict:
        return self._mark(task_id, "skipped", finished=True, error=reason, metadata=metadata)

    def mark_cancelled(self, task_id: str, reason: str, metadata: Optional[Dict] = None) -> Dict:
        return self._mark(task_id, "cancelled", finished=True, error=reason, error_code="cancelled", metadata=metadata)

    def _mark(
        self,
        task_id: str,
        status: str,
        started: bool = False,
        finished: bool = False,
        increment_attempt: bool = False,
        error: str = "",
        error_code: str = "",
        result_path: str = "",
        metadata: Optional[Dict] = None,
    ) -> Dict:
        self.initialize()
        ts = now_ts()
        current = self.get_task(task_id) or {}
        merged_metadata = dict(current.get("metadata") or {})
        if metadata:
            merged_metadata.update(metadata)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE runtime_tasks SET
                    status=?,
                    updated_at=?,
                    started_at=CASE WHEN ? THEN COALESCE(started_at, ?) ELSE started_at END,
                    finished_at=CASE WHEN ? THEN ? ELSE finished_at END,
                    attempts=attempts + ?,
                    result_path=CASE WHEN ? != '' THEN ? ELSE result_path END,
                    error=?,
                    error_code=CASE WHEN ? != '' THEN ? ELSE error_code END,
                    metadata_json=?
                WHERE task_id=?
                """,
                (
                    status,
                    ts,
                    1 if started else 0,
                    ts,
                    1 if finished else 0,
                    ts,
                    1 if increment_attempt else 0,
                    result_path,
                    result_path,
                    error,
                    error_code,
                    error_code,
                    _json_dumps(merged_metadata),
                    task_id,
                ),
            )
        return self.get_task(task_id) or {}

    def cancel_task(self, task_id: str, reason: str = "cancelled_by_request") -> Dict:
        return self.mark_cancelled(task_id, reason)

    def delete_task(self, task_id: str) -> bool:
        self.initialize()
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM runtime_tasks WHERE task_id=?", (task_id,))
            return cur.rowcount > 0

    def get_task(self, task_id: str) -> Optional[Dict]:
        self.initialize()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM runtime_tasks WHERE task_id=?", (task_id,)).fetchone()
        return TaskRecord.from_row(row).to_dict() if row else None

    def recent_tasks(self, limit: int = 20) -> list[Dict]:
        self.initialize()
        limit = max(1, min(int(limit or 20), 200))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runtime_tasks ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [TaskRecord.from_row(row).to_dict() for row in rows]

    def _filter_task_list(
        self,
        tasks: list[Dict],
        *,
        blocking_only: bool = False,
        include_terminal: bool = True,
    ) -> list[Dict]:
        if blocking_only:
            tasks = [
                task
                for task in tasks
                if bool((task.get("metadata") or {}).get("blocks_final_report"))
            ]
        if not include_terminal:
            tasks = [
                task
                for task in tasks
                if str(task.get("status") or "") not in TERMINAL_STATUSES
            ]
        return tasks

    def tasks_for_scope(
        self,
        *,
        task_scope_id: str = "",
        work_scope_id: str = "",
        server_run_id: str = "",
        blocking_only: bool = False,
        include_terminal: bool = True,
        limit: int = 100,
    ) -> list[Dict]:
        self.initialize()
        task_scope = str(task_scope_id or "").strip()
        work_scope = str(work_scope_id or "").strip()
        server_run = str(server_run_id or "").strip()
        if not (task_scope or work_scope or server_run):
            return []
        limit = max(1, min(int(limit or 100), 500))
        clauses = []
        params: list[Any] = []
        if task_scope:
            clauses.append("(task_scope_id = ? OR json_extract(metadata_json, '$.task_scope_id') = ?)")
            params.extend([task_scope, task_scope])
        if work_scope:
            clauses.append("(work_scope_id = ? OR json_extract(metadata_json, '$.work_scope_id') = ?)")
            params.extend([work_scope, work_scope])
        if server_run:
            clauses.append("(server_run_id = ? OR json_extract(metadata_json, '$.server_run_id') = ?)")
            params.extend([server_run, server_run])
        sql = "SELECT * FROM runtime_tasks WHERE " + " AND ".join(clauses) + " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return self._filter_task_list(
            [TaskRecord.from_row(row).to_dict() for row in rows],
            blocking_only=blocking_only,
            include_terminal=include_terminal,
        )

    def tasks_for_source(
        self,
        *,
        source_url: str = "",
        content_id: str = "",
        blocking_only: bool = False,
        include_terminal: bool = True,
        limit: int = 100,
    ) -> list[Dict]:
        self.initialize()
        source = str(source_url or "").strip()
        content = str(content_id or "").strip()
        if not (source or content):
            return []
        limit = max(1, min(int(limit or 100), 500))
        clauses = []
        params: list[Any] = []
        if source:
            clauses.append("(source_url = ? OR json_extract(metadata_json, '$.source_url') = ?)")
            params.extend([source, source])
        if content:
            clauses.append("(content_id = ? OR json_extract(metadata_json, '$.content_id') = ?)")
            params.extend([content, content])
        sql = "SELECT * FROM runtime_tasks WHERE " + " AND ".join(clauses) + " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return self._filter_task_list(
            [TaskRecord.from_row(row).to_dict() for row in rows],
            blocking_only=blocking_only,
            include_terminal=include_terminal,
        )

    def wait_for_scope(
        self,
        *,
        task_scope_id: str = "",
        work_scope_id: str = "",
        source_url: str = "",
        content_id: str = "",
        task_id: str = "",
        max_wait_s: float = 60.0,
        poll_s: float = 1.0,
        blocking_only: bool = True,
    ) -> Dict[str, Any]:
        started = now_ts()
        max_wait = max(0.0, float(max_wait_s or 0.0))
        poll = max(0.1, min(float(poll_s or 1.0), 10.0))

        def _selected_tasks() -> list[Dict]:
            if task_id:
                task = self.get_task(task_id)
                tasks = [task] if task else []
                return self._filter_task_list(tasks, blocking_only=blocking_only, include_terminal=True)
            if task_scope_id or work_scope_id:
                return self.tasks_for_scope(
                    task_scope_id=task_scope_id,
                    work_scope_id=work_scope_id,
                    blocking_only=blocking_only,
                    include_terminal=True,
                )
            return self.tasks_for_source(
                source_url=source_url,
                content_id=content_id,
                blocking_only=blocking_only,
                include_terminal=True,
            )

        while True:
            tasks = _selected_tasks()
            pending = [
                task
                for task in tasks
                if str(task.get("status") or "") in {"queued", "running"}
            ]
            elapsed = now_ts() - started
            if not pending or elapsed >= max_wait:
                terminal = [
                    task
                    for task in tasks
                    if str(task.get("status") or "") in TERMINAL_STATUSES
                ]
                failure_terminal = [
                    task for task in terminal if str(task.get("status") or "") in {"failed", "cancelled", "skipped"}
                ]
                return {
                    "schema_version": "knowledgeradar-task-scope-wait/v1",
                    "status": "completed" if not pending else "timeout",
                    "outcome": "completed_with_failures" if not pending and failure_terminal else ("pending_timeout" if pending else "completed"),
                    "task_scope_id": task_scope_id,
                    "work_scope_id": work_scope_id,
                    "source_url": source_url,
                    "content_id": content_id,
                    "task_id": task_id,
                    "blocking_only": blocking_only,
                    "waited_s": round(elapsed, 3),
                    "pending": pending,
                    "terminal": terminal,
                    "terminal_failure_count": len(failure_terminal),
                    "tasks": tasks,
                }
            time.sleep(min(poll, max(0.0, max_wait - elapsed)))

    def tasks_for_session(
        self,
        research_session_id: str,
        *,
        blocking_only: bool = False,
        include_terminal: bool = True,
        limit: int = 100,
    ) -> list[Dict]:
        self.initialize()
        session_id = str(research_session_id or "").strip()
        if not session_id:
            return []
        limit = max(1, min(int(limit or 100), 500))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM runtime_tasks
                WHERE json_extract(metadata_json, '$.research_session_id') = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        tasks = [TaskRecord.from_row(row).to_dict() for row in rows]
        return self._filter_task_list(tasks, blocking_only=blocking_only, include_terminal=include_terminal)

    def wait_for_session(
        self,
        research_session_id: str,
        *,
        max_wait_s: float = 60.0,
        poll_s: float = 1.0,
        blocking_only: bool = True,
    ) -> Dict[str, Any]:
        started = now_ts()
        max_wait = max(0.0, float(max_wait_s or 0.0))
        poll = max(0.1, min(float(poll_s or 1.0), 10.0))
        while True:
            tasks = self.tasks_for_session(
                research_session_id,
                blocking_only=blocking_only,
                include_terminal=True,
            )
            pending = [
                task
                for task in tasks
                if str(task.get("status") or "") in {"queued", "running"}
            ]
            elapsed = now_ts() - started
            if not pending or elapsed >= max_wait:
                terminal = [
                    task
                    for task in tasks
                    if str(task.get("status") or "") in TERMINAL_STATUSES
                ]
                failure_terminal = [
                    task for task in terminal if str(task.get("status") or "") in {"failed", "cancelled", "skipped"}
                ]
                return {
                    "schema_version": "knowledgeradar-task-session-wait/v1",
                    "status": "completed" if not pending else "timeout",
                    "outcome": "completed_with_failures" if not pending and failure_terminal else ("pending_timeout" if pending else "completed"),
                    "research_session_id": research_session_id,
                    "blocking_only": blocking_only,
                    "waited_s": round(elapsed, 3),
                    "pending": pending,
                    "terminal": terminal,
                    "terminal_failure_count": len(failure_terminal),
                    "tasks": tasks,
                }
            time.sleep(min(poll, max(0.0, max_wait - elapsed)))

    def stale_tasks(
        self,
        *,
        running_seconds: int | None = None,
        queued_seconds: int | None = None,
        limit: int = 50,
    ) -> list[Dict]:
        self.initialize()
        running_cutoff = now_ts() - float(running_seconds or DEFAULT_STALE_RUNNING_SECONDS)
        queued_cutoff = now_ts() - float(queued_seconds or DEFAULT_STALE_QUEUED_SECONDS)
        limit = max(1, min(int(limit or 50), 200))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM runtime_tasks
                WHERE (status = 'running' AND updated_at < ?)
                   OR (status = 'queued' AND created_at < ?)
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                (running_cutoff, queued_cutoff, limit),
            ).fetchall()
        return [TaskRecord.from_row(row).to_dict() for row in rows]

    def cleanup_stale_tasks(
        self,
        *,
        running_seconds: int | None = None,
        queued_seconds: int | None = None,
        limit: int = 200,
        reason: str = "stale_runtime_task_cleanup",
    ) -> Dict:
        stale = self.stale_tasks(
            running_seconds=running_seconds,
            queued_seconds=queued_seconds,
            limit=limit,
        )
        cleaned = []
        for task in stale:
            task_id = str(task.get("task_id") or "")
            if not task_id:
                continue
            cleaned.append(
                self.mark_cancelled(
                    task_id,
                    reason,
                    metadata={
                        "cleanup_reason": reason,
                        "previous_status": task.get("status", ""),
                    },
                )
            )
        return {
            "status": "ok",
            "cleaned_count": len(cleaned),
            "cleaned": cleaned,
            "reason": reason,
        }

    def heartbeat(self, task_id: str, metadata: Optional[Dict] = None) -> Dict:
        self.initialize()
        ts = now_ts()
        current = self.get_task(task_id) or {}
        merged_metadata = dict(current.get("metadata") or {})
        if metadata:
            merged_metadata.update(metadata)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE runtime_tasks SET
                    updated_at=?,
                    metadata_json=?
                WHERE task_id=?
                """,
                (ts, _json_dumps(merged_metadata), task_id),
            )
        return self.get_task(task_id) or {}

    def summary(self, recent_limit: int = 10) -> Dict:
        self.initialize()
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS count FROM runtime_tasks GROUP BY status").fetchall()
            platform_rows = conn.execute("SELECT platform, COUNT(*) AS count FROM runtime_tasks GROUP BY platform ORDER BY count DESC").fetchall()
            type_rows = conn.execute("SELECT task_type, COUNT(*) AS count FROM runtime_tasks GROUP BY task_type ORDER BY count DESC").fetchall()
            error_rows = conn.execute(
                """
                SELECT COALESCE(NULLIF(error_code, ''), 'unknown') AS error_code, COUNT(*) AS count
                FROM runtime_tasks
                WHERE status = 'failed'
                GROUP BY COALESCE(NULLIF(error_code, ''), 'unknown')
                ORDER BY count DESC
                LIMIT 10
                """
            ).fetchall()
            active_oldest = conn.execute(
                """
                SELECT MIN(COALESCE(started_at, created_at)) AS started
                FROM runtime_tasks
                WHERE status IN ('queued', 'running')
                """
            ).fetchone()["started"]
            failed_rows = conn.execute(
                """
                SELECT * FROM runtime_tasks
                WHERE status = 'failed'
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (max(1, min(int(recent_limit or 10), 20)),),
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) AS count FROM runtime_tasks").fetchone()["count"]
            recent_rows = conn.execute(
                "SELECT * FROM runtime_tasks ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(int(recent_limit or 10), 50)),),
            ).fetchall()
        counts = {status: 0 for status in TASK_STATUSES}
        for row in rows:
            counts[row["status"]] = int(row["count"] or 0)
        stale = self.stale_tasks(limit=recent_limit)
        active_oldest_age_s = round(now_ts() - float(active_oldest), 3) if active_oldest else 0
        by_error_code = [{"error_code": row["error_code"] or "unknown", "count": int(row["count"] or 0)} for row in error_rows]
        return {
            "status": "ok",
            "detail": "SQLite task store 可读写",
            "db_path": self.db_path,
            "sqlite_policy": {
                "schema": "knowledgeradar-runtime-sqlite-policy/v1",
                "wal_requested": self._wal_enabled,
                "journal_mode": self._journal_mode or "default",
                "timeout_s": 10,
            },
            "total": int(total or 0),
            "counts": counts,
            "by_platform": [{"platform": row["platform"] or "unknown", "count": int(row["count"] or 0)} for row in platform_rows],
            "by_task_type": [{"task_type": row["task_type"] or "unknown", "count": int(row["count"] or 0)} for row in type_rows],
            "by_error_code": by_error_code,
            "unknown_error_count": sum(item["count"] for item in by_error_code if item["error_code"] == "unknown"),
            "active": counts.get("queued", 0) + counts.get("running", 0),
            "active_oldest_age_s": active_oldest_age_s,
            "stale": stale,
            "stale_count": len(stale),
            "stale_running_seconds": DEFAULT_STALE_RUNNING_SECONDS,
            "stale_queued_seconds": DEFAULT_STALE_QUEUED_SECONDS,
            "recent_failed": [TaskRecord.from_row(row).to_dict() for row in failed_rows],
            "recent": [TaskRecord.from_row(row).to_dict() for row in recent_rows],
        }


_DEFAULT_STORE = TaskStore()


def get_task_store() -> TaskStore:
    return _DEFAULT_STORE
