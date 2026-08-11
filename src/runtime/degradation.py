"""Unified degradation control plane for providers and models."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import random
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, Iterator, Optional, Sequence, Tuple, Type

from runtime.paths import runtime_log_dir


def _runtime_dir() -> str:
    return str(runtime_log_dir())


def default_degradation_db_path() -> str:
    return os.environ.get("KR_DEGRADATION_DB_PATH") or os.path.join(_runtime_dir(), "knowledgeradar-degradation.sqlite3")


def default_degradation_event_path() -> str:
    return os.environ.get("KR_DEGRADATION_EVENT_PATH") or os.path.join(_runtime_dir(), "knowledgeradar-degradation-events.jsonl")


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _json_dumps(value: Optional[Dict[str, Any]]) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)


def _json_loads(value: str | None) -> Dict[str, Any]:
    if not value:
        return {}
    try:
        data = json.loads(value)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@dataclass(frozen=True)
class DegradationEvent:
    scope: str
    key: str
    action: str
    reason: str
    metadata: Dict[str, Any]
    created_at: float


class DegradationPolicy:
    def __init__(self, db_path: str | None = None, event_path: str | None = None):
        self.db_path = db_path or default_degradation_db_path()
        self.event_path = event_path or default_degradation_event_path()
        self._lock = threading.RLock()
        self._initialized = False
        self.default_failure_threshold = int(os.environ.get("KR_DEGRADATION_FAILURE_THRESHOLD", "3"))
        self.default_cooldown_seconds = int(os.environ.get("KR_DEGRADATION_COOLDOWN_SECONDS", "300"))
        self.default_retry_attempts = int(os.environ.get("KR_DEGRADATION_RETRY_ATTEMPTS", "3"))
        self.default_retry_base_delay = float(os.environ.get("KR_DEGRADATION_RETRY_BASE_DELAY", "0.5"))
        self.default_retry_max_delay = float(os.environ.get("KR_DEGRADATION_RETRY_MAX_DELAY", "5.0"))
        self.default_retry_jitter = float(os.environ.get("KR_DEGRADATION_RETRY_JITTER", "0.35"))

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
                    CREATE TABLE IF NOT EXISTS degradation_breakers (
                        key TEXT PRIMARY KEY,
                        scope TEXT NOT NULL,
                        state TEXT NOT NULL,
                        consecutive_failures INTEGER NOT NULL DEFAULT 0,
                        opened_at REAL,
                        half_open_until REAL,
                        last_success_at REAL,
                        last_failure_at REAL,
                        last_reason TEXT NOT NULL DEFAULT '',
                        metadata_json TEXT NOT NULL DEFAULT '{}'
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS degradation_dead_letters (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        queue_name TEXT NOT NULL,
                        key TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        created_at REAL NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS degradation_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        scope TEXT NOT NULL,
                        key TEXT NOT NULL,
                        action TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        metadata_json TEXT NOT NULL DEFAULT '{}'
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_degradation_events_created ON degradation_events(created_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_dead_letters_created ON degradation_dead_letters(created_at)")
            self._initialized = True

    def _write_event_file(self, event: DegradationEvent) -> None:
        os.makedirs(os.path.dirname(self.event_path), exist_ok=True)
        line = json.dumps(
            {
                "scope": event.scope,
                "key": event.key,
                "action": event.action,
                "reason": event.reason,
                "metadata": event.metadata,
                "created_at": event.created_at,
            },
            ensure_ascii=False,
        )
        with open(self.event_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def record_event(self, scope: str, key: str, action: str, reason: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.initialize()
        event = DegradationEvent(scope=scope, key=key, action=action, reason=reason, metadata=metadata or {}, created_at=_now_ts())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO degradation_events (scope, key, action, reason, created_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (event.scope, event.key, event.action, event.reason, event.created_at, _json_dumps(event.metadata)),
            )
        self._write_event_file(event)
        return {
            "scope": scope,
            "key": key,
            "action": action,
            "reason": reason,
            "metadata": metadata or {},
            "created_at": event.created_at,
        }

    def _get_breaker_row(self, key: str) -> Optional[Dict[str, Any]]:
        self.initialize()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM degradation_breakers WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    def is_open(self, key: str) -> Dict[str, Any]:
        row = self._get_breaker_row(key)
        if not row:
            return {"open": False, "state": "closed"}
        state = row.get("state") or "closed"
        opened_at = float(row.get("opened_at") or 0)
        half_open_until = float(row.get("half_open_until") or 0)
        now = _now_ts()
        if state == "open" and half_open_until and now >= half_open_until:
            return {"open": False, "state": "half_open", "probe_allowed": True}
        return {
            "open": state == "open",
            "state": state,
            "consecutive_failures": int(row.get("consecutive_failures") or 0),
            "opened_at": opened_at or None,
            "half_open_until": half_open_until or None,
            "last_reason": row.get("last_reason") or "",
        }

    def allow(self, key: str) -> bool:
        return not bool(self.is_open(key).get("open"))

    def mark_success(self, key: str, scope: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.initialize()
        ts = _now_ts()
        current = self._get_breaker_row(key)
        merged = _json_loads(current["metadata_json"]) if current else {}
        if metadata:
            merged.update(metadata)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO degradation_breakers (key, scope, state, consecutive_failures, opened_at, half_open_until, last_success_at, last_failure_at, last_reason, metadata_json)
                VALUES (?, ?, 'closed', 0, NULL, NULL, ?, NULL, '', ?)
                ON CONFLICT(key) DO UPDATE SET
                    scope=excluded.scope,
                    state='closed',
                    consecutive_failures=0,
                    opened_at=NULL,
                    half_open_until=NULL,
                    last_success_at=excluded.last_success_at,
                    last_reason='',
                    metadata_json=excluded.metadata_json
                """,
                (key, scope, ts, _json_dumps(merged)),
            )
        return self.record_event(scope, key, "success", "call succeeded", metadata)

    def mark_failure(
        self,
        key: str,
        scope: str,
        reason: str,
        *,
        metadata: Optional[Dict[str, Any]] = None,
        failure_threshold: Optional[int] = None,
        cooldown_seconds: Optional[int] = None,
        retryable: bool = True,
    ) -> Dict[str, Any]:
        self.initialize()
        threshold = int(failure_threshold or self.default_failure_threshold)
        cooldown = int(cooldown_seconds or self.default_cooldown_seconds)
        ts = _now_ts()
        current = self._get_breaker_row(key) or {}
        failures = int(current.get("consecutive_failures") or 0) + 1
        state = "open" if failures >= threshold else "closed"
        half_open_until = ts + cooldown if state == "open" else None
        merged = _json_loads(current.get("metadata_json"))
        if metadata:
            merged.update(metadata)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO degradation_breakers (
                    key, scope, state, consecutive_failures, opened_at, half_open_until,
                    last_success_at, last_failure_at, last_reason, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    scope=excluded.scope,
                    state=excluded.state,
                    consecutive_failures=excluded.consecutive_failures,
                    opened_at=CASE WHEN excluded.state='open' THEN excluded.opened_at ELSE degradation_breakers.opened_at END,
                    half_open_until=excluded.half_open_until,
                    last_failure_at=excluded.last_failure_at,
                    last_reason=excluded.last_reason,
                    metadata_json=excluded.metadata_json
                """,
                (
                    key,
                    scope,
                    state,
                    failures,
                    ts if state == "open" else current.get("opened_at"),
                    half_open_until,
                    current.get("last_success_at"),
                    ts,
                    reason,
                    _json_dumps(merged),
                ),
            )
        self.record_event(scope, key, "failure", reason, {**(metadata or {}), "retryable": retryable, "state": state, "consecutive_failures": failures})
        return {
            "key": key,
            "scope": scope,
            "state": state,
            "consecutive_failures": failures,
            "opened": state == "open",
            "retryable": retryable,
        }

    def record_degradation(self, scope: str, key: str, reason: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.record_event(scope, key, "degraded", reason, metadata)

    def record_dead_letter(
        self,
        queue_name: str,
        key: str,
        reason: str,
        payload: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        self.initialize()
        ts = _now_ts()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO degradation_dead_letters (queue_name, key, reason, payload_json, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (queue_name, key, reason, _json_dumps(payload), _json_dumps(metadata), ts),
            )
        self.record_event(queue_name, key, "dead_letter", reason, {"payload": payload or {}, **(metadata or {})})
        return {"queue_name": queue_name, "key": key, "reason": reason, "created_at": ts}

    def retry_with_jitter(
        self,
        key: str,
        scope: str,
        func: Callable[[], Any],
        *,
        attempts: Optional[int] = None,
        base_delay: Optional[float] = None,
        max_delay: Optional[float] = None,
        jitter: Optional[float] = None,
        retryable_exceptions: Sequence[Type[BaseException]] = (),
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Any:
        attempts = int(attempts or self.default_retry_attempts)
        base_delay = float(base_delay or self.default_retry_base_delay)
        max_delay = float(max_delay or self.default_retry_max_delay)
        jitter = float(jitter or self.default_retry_jitter)
        last_error: Optional[BaseException] = None

        breaker = self.is_open(key)
        if breaker.get("open"):
            reason = f"circuit breaker open: {breaker.get('last_reason') or 'recent failures'}"
            self.record_degradation(scope, key, reason, {"breaker": breaker, **(metadata or {})})
            raise RuntimeError(reason)

        for index in range(max(1, attempts)):
            if index > 0:
                delay = min(max_delay, base_delay * (2 ** (index - 1)))
                delay = delay + random.uniform(0, jitter)
                time.sleep(delay)
            try:
                result = func()
                self.mark_success(key, scope, metadata)
                return result
            except Exception as exc:
                last_error = exc
                retryable = bool(not retryable_exceptions or isinstance(exc, tuple(retryable_exceptions)))
                self.mark_failure(
                    key,
                    scope,
                    str(exc),
                    metadata={**(metadata or {}), "attempt": index + 1},
                    retryable=retryable,
                )
                if index + 1 >= attempts or not retryable:
                    break
        raise RuntimeError(str(last_error) if last_error else "degradation retry failed")

    def summary(self, recent_limit: int = 20) -> Dict[str, Any]:
        self.initialize()
        recent_limit = max(1, min(int(recent_limit or 20), 100))
        with self._lock, self._connect() as conn:
            breakers = conn.execute(
                "SELECT * FROM degradation_breakers ORDER BY last_failure_at DESC, last_success_at DESC LIMIT ?",
                (recent_limit,),
            ).fetchall()
            dead_letters = conn.execute(
                "SELECT * FROM degradation_dead_letters ORDER BY created_at DESC LIMIT ?",
                (recent_limit,),
            ).fetchall()
            events = conn.execute(
                "SELECT * FROM degradation_events ORDER BY created_at DESC LIMIT ?",
                (recent_limit,),
            ).fetchall()
        open_breakers = []
        for row in breakers:
            item = dict(row)
            if item.get("state") == "open":
                open_breakers.append(
                    {
                        "key": item.get("key"),
                        "scope": item.get("scope"),
                        "reason": item.get("last_reason") or "",
                        "opened_at": item.get("opened_at"),
                        "half_open_until": item.get("half_open_until"),
                        "failures": item.get("consecutive_failures") or 0,
                    }
                )
        return {
            "status": "ok",
            "detail": "degradation control plane 可读写",
            "db_path": self.db_path,
            "event_path": self.event_path,
            "open_breakers": open_breakers,
            "open_breaker_count": len(open_breakers),
            "recent_dead_letters": [dict(row) for row in dead_letters],
            "recent_events": [dict(row) for row in events],
        }


_DEFAULT_POLICY = DegradationPolicy()


def get_degradation_policy() -> DegradationPolicy:
    return _DEFAULT_POLICY
