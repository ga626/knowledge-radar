"""Local usage tracking for model calls."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import sqlite3
import threading
from typing import Dict, Iterator, Optional

from runtime.paths import runtime_log_dir


def _runtime_dir() -> str:
    return str(runtime_log_dir())


def default_usage_db_path() -> str:
    return os.environ.get("KR_USAGE_DB_PATH") or os.path.join(_runtime_dir(), "knowledgeradar-usage.sqlite3")


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


@dataclass(frozen=True)
class UsageRecord:
    model: str
    capability: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    created_at: float
    metadata: Dict


class UsageTracker:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or default_usage_db_path()
        self._lock = threading.RLock()
        self._initialized = False

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
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
                    CREATE TABLE IF NOT EXISTS usage_records (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        model TEXT NOT NULL,
                        capability TEXT NOT NULL,
                        prompt_tokens INTEGER NOT NULL DEFAULT 0,
                        completion_tokens INTEGER NOT NULL DEFAULT 0,
                        total_tokens INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL,
                        metadata_json TEXT NOT NULL DEFAULT '{}'
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_records_created ON usage_records(created_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_records_model ON usage_records(model)")
            self._initialized = True

    def record(
        self,
        *,
        model: str,
        capability: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: Optional[int] = None,
        metadata: Optional[Dict] = None,
    ) -> Dict:
        self.initialize()
        prompt_tokens = int(prompt_tokens or 0)
        completion_tokens = int(completion_tokens or 0)
        total_tokens = int(total_tokens if total_tokens is not None else prompt_tokens + completion_tokens)
        payload = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        ts = _now_ts()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO usage_records (
                    model, capability, prompt_tokens, completion_tokens, total_tokens, created_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (model, capability, prompt_tokens, completion_tokens, total_tokens, ts, payload),
            )
        return {
            "model": model,
            "capability": capability,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "created_at": ts,
            "metadata": metadata or {},
        }

    def summary(self, recent_limit: int = 10) -> Dict:
        self.initialize()
        recent_limit = max(1, min(int(recent_limit or 10), 50))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    COUNT(*) AS count,
                    SUM(prompt_tokens) AS prompt_tokens,
                    SUM(completion_tokens) AS completion_tokens,
                    SUM(total_tokens) AS total_tokens,
                    model,
                    capability
                FROM usage_records
                GROUP BY model, capability
                ORDER BY total_tokens DESC
                """
            ).fetchall()
            recent_rows = conn.execute(
                "SELECT * FROM usage_records ORDER BY created_at DESC LIMIT ?",
                (recent_limit,),
            ).fetchall()
            totals = conn.execute(
                "SELECT COUNT(*) AS count, SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens, SUM(total_tokens) AS total_tokens FROM usage_records"
            ).fetchone()
        grouped = [
            {
                "model": row["model"],
                "capability": row["capability"],
                "count": int(row["count"] or 0),
                "prompt_tokens": int(row["prompt_tokens"] or 0),
                "completion_tokens": int(row["completion_tokens"] or 0),
                "total_tokens": int(row["total_tokens"] or 0),
            }
            for row in rows
        ]
        recent = [
            {
                "model": row["model"],
                "capability": row["capability"],
                "prompt_tokens": int(row["prompt_tokens"] or 0),
                "completion_tokens": int(row["completion_tokens"] or 0),
                "total_tokens": int(row["total_tokens"] or 0),
                "created_at": float(row["created_at"] or 0),
                "metadata": json.loads(row["metadata_json"] or "{}"),
            }
            for row in recent_rows
        ]
        return {
            "status": "ok",
            "detail": "usage tracker 可读写",
            "db_path": self.db_path,
            "total_calls": int(totals["count"] or 0),
            "prompt_tokens": int(totals["prompt_tokens"] or 0),
            "completion_tokens": int(totals["completion_tokens"] or 0),
            "total_tokens": int(totals["total_tokens"] or 0),
            "by_model": grouped,
            "recent": recent,
        }


_DEFAULT_TRACKER = UsageTracker()


def get_usage_tracker() -> UsageTracker:
    return _DEFAULT_TRACKER
