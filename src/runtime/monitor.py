"""Periodic runtime monitoring snapshots for KnowledgeRadar."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import sqlite3
import threading
import time
from typing import Callable, Dict, Iterator, Optional

from runtime.paths import runtime_log_dir


def _runtime_dir() -> str:
    return str(runtime_log_dir())


def default_monitor_db_path() -> str:
    return os.environ.get("KR_MONITOR_DB_PATH") or os.path.join(_runtime_dir(), "knowledgeradar-monitor.sqlite3")


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


@dataclass(frozen=True)
class MonitorSample:
    scope: str
    name: str
    success: bool
    latency_ms: int
    login_health: str
    fallback_count: int
    metadata: Dict
    created_at: float


class MonitorTracker:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or default_monitor_db_path()
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
                    CREATE TABLE IF NOT EXISTS monitor_samples (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        scope TEXT NOT NULL,
                        name TEXT NOT NULL,
                        success INTEGER NOT NULL DEFAULT 0,
                        latency_ms INTEGER NOT NULL DEFAULT 0,
                        login_health TEXT NOT NULL DEFAULT '',
                        fallback_count INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL,
                        metadata_json TEXT NOT NULL DEFAULT '{}'
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_monitor_samples_created ON monitor_samples(created_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_monitor_samples_scope ON monitor_samples(scope, name)")
            self._initialized = True

    def record(
        self,
        *,
        scope: str,
        name: str,
        success: bool,
        latency_ms: int = 0,
        login_health: str = "",
        fallback_count: int = 0,
        metadata: Optional[Dict] = None,
    ) -> Dict:
        self.initialize()
        ts = _now_ts()
        payload = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO monitor_samples (
                    scope, name, success, latency_ms, login_health, fallback_count, created_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (scope, name, 1 if success else 0, int(latency_ms or 0), login_health, int(fallback_count or 0), ts, payload),
            )
        return {
            "scope": scope,
            "name": name,
            "success": bool(success),
            "latency_ms": int(latency_ms or 0),
            "login_health": login_health,
            "fallback_count": int(fallback_count or 0),
            "created_at": ts,
            "metadata": metadata or {},
        }

    def summary(self, recent_limit: int = 20) -> Dict:
        self.initialize()
        recent_limit = max(1, min(int(recent_limit or 20), 100))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    scope, name,
                    COUNT(*) AS count,
                    SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS success_count,
                    AVG(latency_ms) AS avg_latency_ms,
                    SUM(fallback_count) AS fallback_count
                FROM monitor_samples
                GROUP BY scope, name
                ORDER BY scope, name
                """
            ).fetchall()
            recent_rows = conn.execute(
                "SELECT * FROM monitor_samples ORDER BY created_at DESC LIMIT ?",
                (recent_limit,),
            ).fetchall()
        groups = []
        for row in rows:
            count = int(row["count"] or 0)
            success_count = int(row["success_count"] or 0)
            groups.append(
                {
                    "scope": row["scope"],
                    "name": row["name"],
                    "count": count,
                    "success_rate": round(success_count / count, 3) if count else 0.0,
                    "avg_latency_ms": round(float(row["avg_latency_ms"] or 0), 1),
                    "fallback_count": int(row["fallback_count"] or 0),
                }
            )
        recent = [
            {
                "scope": row["scope"],
                "name": row["name"],
                "success": bool(row["success"]),
                "latency_ms": int(row["latency_ms"] or 0),
                "login_health": row["login_health"] or "",
                "fallback_count": int(row["fallback_count"] or 0),
                "created_at": float(row["created_at"] or 0),
                "metadata": json.loads(row["metadata_json"] or "{}"),
            }
            for row in recent_rows
        ]
        totals = {
            "status": "ok",
            "detail": "monitor tracker 可读写",
            "db_path": self.db_path,
            "groups": groups,
            "recent": recent,
        }
        return totals


def sample_runtime_snapshot(*, scope: str, run_health_check: Callable[[], Dict], fallback_count: int = 0) -> Dict:
    tracker = get_monitor_tracker()
    started = _now_ts()
    try:
        health = run_health_check()
        elapsed_ms = int((_now_ts() - started) * 1000)
        checks = health.get("checks", {}) if isinstance(health, dict) else {}
        login_health = ",".join(
            [
                name
                for name in ("zhihu", "xiaohongshu")
                if checks.get(name, {}).get("login_state") == "authenticated"
            ]
        )
        tracker.record(
            scope=scope,
            name="health_check",
            success=str(health.get("status")) == "ok",
            latency_ms=elapsed_ms,
            login_health=login_health,
            fallback_count=fallback_count,
            metadata={"status": health.get("status"), "checks": {k: v.get("status") for k, v in checks.items()}},
        )
        return {
            "status": "ok",
            "elapsed_ms": elapsed_ms,
            "health_status": health.get("status"),
        }
    except Exception as exc:
        elapsed_ms = int((_now_ts() - started) * 1000)
        tracker.record(
            scope=scope,
            name="health_check",
            success=False,
            latency_ms=elapsed_ms,
            fallback_count=fallback_count,
            metadata={"error": str(exc)},
        )
        return {"status": "degraded", "error": str(exc), "elapsed_ms": elapsed_ms}


_DEFAULT_MONITOR = MonitorTracker()


def get_monitor_tracker() -> MonitorTracker:
    return _DEFAULT_MONITOR
