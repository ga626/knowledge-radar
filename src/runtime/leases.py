"""SQLite-backed runtime leases for cross-client resource coordination."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from runtime.paths import runtime_log_dir


SCHEMA_VERSION = "knowledgeradar-runtime-leases/v1"
LEASE_STATES = {"held", "released", "expired"}
DEFAULT_TTL_S = 300
ACQUIRE_RETRY_ATTEMPTS = 3


def default_lease_db_path() -> str:
    return os.environ.get("KR_RUNTIME_LEASE_DB_PATH") or str(runtime_log_dir() / "knowledgeradar-runtime-leases.sqlite3")


def now_ts() -> float:
    return time.time()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _clean_key(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _lease_id(resource_kind: str, resource_key: str, owner_client_id: str, owner_request_id: str = "") -> str:
    raw = json.dumps(
        {
            "kind": _clean_key(resource_kind),
            "key": _clean_key(resource_key),
            "owner": str(owner_client_id or ""),
            "request": str(owner_request_id or ""),
            "bucket": int(now_ts() * 1000),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "lease-" + hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:20]


def default_owner(tool: str = "", *, project_root: str = "", thread_id: str = "", request_id: str = "") -> Dict[str, Any]:
    host = os.environ.get("KR_HOST_PLATFORM", "codex")
    project = project_root or os.environ.get("KR_PROJECT_ROOT", "")
    thread = thread_id or os.environ.get("KR_CODEX_THREAD_ID", "") or os.environ.get("KR_THREAD_ID", "")
    owner_seed = json.dumps({"host": host, "project": project, "thread": thread}, sort_keys=True)
    owner_hash = hashlib.sha256(owner_seed.encode("utf-8", errors="ignore")).hexdigest()[:12]
    return {
        "client_id": f"{host}:{owner_hash}",
        "host_platform": host,
        "project_root": project,
        "thread_id": thread,
        "request_id": request_id,
        "tool": tool,
    }


@dataclass(frozen=True)
class LeaseResult:
    acquired: bool
    lease_id: str = ""
    resource_kind: str = ""
    resource_key: str = ""
    state: str = ""
    reason: str = ""
    retry_after_s: float = 0.0
    owner: Dict[str, Any] | None = None
    holder: Dict[str, Any] | None = None
    metadata: Dict[str, Any] | None = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["schema"] = "knowledgeradar-runtime-lease-result/v1"
        return data


class RuntimeLeaseCoordinator:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = str(db_path or default_lease_db_path())
        self._lock = threading.RLock()
        self._initialized = False

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
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
                    CREATE TABLE IF NOT EXISTS runtime_leases (
                        lease_id TEXT PRIMARY KEY,
                        resource_kind TEXT NOT NULL,
                        resource_key TEXT NOT NULL,
                        slot_key TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        state TEXT NOT NULL,
                        owner_client_id TEXT NOT NULL,
                        owner_project_root TEXT NOT NULL DEFAULT '',
                        owner_thread_id TEXT NOT NULL DEFAULT '',
                        owner_request_id TEXT NOT NULL DEFAULT '',
                        owner_tool TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        heartbeat_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        released_at REAL,
                        metadata_json TEXT NOT NULL DEFAULT '{}'
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_leases_active_slot
                    ON runtime_leases(resource_kind, resource_key, slot_key)
                    WHERE state='held'
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_runtime_leases_state ON runtime_leases(state, expires_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_runtime_leases_owner ON runtime_leases(owner_client_id)")
            self._initialized = True

    def expire_stale(self, *, now: float | None = None) -> int:
        self.initialize()
        ts = float(now if now is not None else now_ts())
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE runtime_leases
                SET state='expired', updated_at=?, released_at=?
                WHERE state='held' AND expires_at <= ?
                """,
                (ts, ts, ts),
            )
            return int(cur.rowcount or 0)

    def acquire_exclusive(
        self,
        resource_kind: str,
        resource_key: str,
        *,
        owner: Dict[str, Any] | None = None,
        ttl_s: int = DEFAULT_TTL_S,
        metadata: Dict[str, Any] | None = None,
        now: float | None = None,
    ) -> LeaseResult:
        return self.acquire_bounded(
            resource_kind,
            resource_key,
            limit=1,
            owner=owner,
            ttl_s=ttl_s,
            metadata=metadata,
            now=now,
        )

    def acquire_bounded(
        self,
        resource_kind: str,
        resource_key: str,
        *,
        limit: int,
        owner: Dict[str, Any] | None = None,
        ttl_s: int = DEFAULT_TTL_S,
        metadata: Dict[str, Any] | None = None,
        now: float | None = None,
    ) -> LeaseResult:
        last_busy: LeaseResult | None = None
        for attempt in range(ACQUIRE_RETRY_ATTEMPTS):
            result = self._acquire_bounded_once(
                resource_kind,
                resource_key,
                limit=limit,
                owner=owner,
                ttl_s=ttl_s,
                metadata=metadata,
                now=now,
            )
            if result.acquired or result.reason != "lease_insert_conflict":
                return result
            last_busy = result
            time.sleep(0.02 * (attempt + 1))
        return last_busy or LeaseResult(acquired=False, resource_kind=_clean_key(resource_kind), resource_key=_clean_key(resource_key), state="busy", reason="lease_insert_conflict")

    def _acquire_bounded_once(
        self,
        resource_kind: str,
        resource_key: str,
        *,
        limit: int,
        owner: Dict[str, Any] | None = None,
        ttl_s: int = DEFAULT_TTL_S,
        metadata: Dict[str, Any] | None = None,
        now: float | None = None,
    ) -> LeaseResult:
        self.initialize()
        ts = float(now if now is not None else now_ts())
        normalized_kind = _clean_key(resource_kind)
        normalized_key = _clean_key(resource_key)
        selected_owner = dict(owner or default_owner())
        ttl = max(1, int(ttl_s or DEFAULT_TTL_S))
        max_slots = max(1, int(limit or 1))
        self.expire_stale(now=ts)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM runtime_leases
                WHERE resource_kind=? AND resource_key=? AND state='held'
                ORDER BY created_at ASC
                """,
                (normalized_kind, normalized_key),
            ).fetchall()
            used = {str(row["slot_key"]) for row in rows}
            slot = ""
            for index in range(max_slots):
                candidate = str(index)
                if candidate not in used:
                    slot = candidate
                    break
            if not slot:
                holder = dict(rows[0]) if rows else {}
                retry_after = max(1.0, float(holder.get("expires_at") or ts) - ts) if holder else float(ttl)
                return LeaseResult(
                    acquired=False,
                    resource_kind=normalized_kind,
                    resource_key=normalized_key,
                    state="busy",
                    reason="lease_unavailable",
                    retry_after_s=round(retry_after, 3),
                    owner=selected_owner,
                    holder=_row_summary(holder),
                    metadata={"limit": max_slots, "active": len(rows)},
                )
            lease_id = _lease_id(normalized_kind, normalized_key, str(selected_owner.get("client_id") or ""), str(selected_owner.get("request_id") or ""))
            try:
                conn.execute(
                    """
                    INSERT INTO runtime_leases (
                        lease_id, resource_kind, resource_key, slot_key, mode, state,
                        owner_client_id, owner_project_root, owner_thread_id, owner_request_id, owner_tool,
                        created_at, updated_at, heartbeat_at, expires_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, 'held', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lease_id,
                        normalized_kind,
                        normalized_key,
                        slot,
                        "exclusive" if max_slots == 1 else "bounded",
                        str(selected_owner.get("client_id") or "unknown"),
                        str(selected_owner.get("project_root") or ""),
                        str(selected_owner.get("thread_id") or ""),
                        str(selected_owner.get("request_id") or ""),
                        str(selected_owner.get("tool") or ""),
                        ts,
                        ts,
                        ts,
                        ts + ttl,
                        _json_dumps({"slot": slot, "limit": max_slots, **dict(metadata or {})}),
                    ),
                )
            except sqlite3.IntegrityError:
                return LeaseResult(
                    acquired=False,
                    resource_kind=normalized_kind,
                    resource_key=normalized_key,
                    state="busy",
                    reason="lease_insert_conflict",
                    retry_after_s=0.05,
                    owner=selected_owner,
                    metadata={"limit": max_slots, "slot": slot},
                )
            return LeaseResult(
                acquired=True,
                lease_id=lease_id,
                resource_kind=normalized_kind,
                resource_key=normalized_key,
                state="held",
                owner=selected_owner,
                metadata={"slot": slot, "limit": max_slots, "ttl_s": ttl},
            )

    def release(self, lease_id: str, *, now: float | None = None) -> bool:
        if not lease_id:
            return False
        self.initialize()
        ts = float(now if now is not None else now_ts())
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE runtime_leases
                SET state='released', updated_at=?, released_at=?
                WHERE lease_id=? AND state='held'
                """,
                (ts, ts, lease_id),
            )
            return bool(cur.rowcount)

    def heartbeat(self, lease_id: str, *, ttl_s: int = DEFAULT_TTL_S, now: float | None = None) -> bool:
        if not lease_id:
            return False
        self.initialize()
        ts = float(now if now is not None else now_ts())
        ttl = max(1, int(ttl_s or DEFAULT_TTL_S))
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE runtime_leases
                SET updated_at=?, heartbeat_at=?, expires_at=?
                WHERE lease_id=? AND state='held'
                """,
                (ts, ts, ts + ttl, lease_id),
            )
            return bool(cur.rowcount)

    def active_leases(self, *, limit: int = 50, now: float | None = None) -> list[Dict[str, Any]]:
        self.initialize()
        self.expire_stale(now=now)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM runtime_leases
                WHERE state='held'
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (max(1, min(int(limit or 50), 200)),),
            ).fetchall()
        return [_row_summary(dict(row)) for row in rows]

    def summary(self, *, limit: int = 20, now: float | None = None) -> Dict[str, Any]:
        self.initialize()
        expired = self.expire_stale(now=now)
        with self._lock, self._connect() as conn:
            counts = {
                row["state"]: int(row["count"])
                for row in conn.execute("SELECT state, COUNT(*) AS count FROM runtime_leases GROUP BY state").fetchall()
            }
            active = [
                _row_summary(dict(row))
                for row in conn.execute(
                    """
                    SELECT * FROM runtime_leases
                    WHERE state='held'
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (max(1, min(int(limit or 20), 100)),),
                ).fetchall()
            ]
        return {
            "schema": SCHEMA_VERSION,
            "db_path": self.db_path,
            "status": "ok",
            "counts": counts,
            "expired_on_read": expired,
            "active": active,
            "generated_at": utc_now_iso(),
        }

    @contextmanager
    def hold_exclusive(self, resource_kind: str, resource_key: str, **kwargs: Any) -> Iterator[LeaseResult]:
        result = self.acquire_exclusive(resource_kind, resource_key, **kwargs)
        try:
            yield result
        finally:
            if result.acquired:
                self.release(result.lease_id)

    @contextmanager
    def hold_bounded(self, resource_kind: str, resource_key: str, **kwargs: Any) -> Iterator[LeaseResult]:
        result = self.acquire_bounded(resource_kind, resource_key, **kwargs)
        try:
            yield result
        finally:
            if result.acquired:
                self.release(result.lease_id)


def _row_summary(row: Dict[str, Any]) -> Dict[str, Any]:
    if not row:
        return {}
    metadata = _json_loads(str(row.get("metadata_json") or ""))
    now = now_ts()
    return {
        "lease_id": str(row.get("lease_id") or ""),
        "resource_kind": str(row.get("resource_kind") or ""),
        "resource_key": str(row.get("resource_key") or ""),
        "slot_key": str(row.get("slot_key") or ""),
        "mode": str(row.get("mode") or ""),
        "state": str(row.get("state") or ""),
        "owner_client_id": str(row.get("owner_client_id") or ""),
        "owner_project_root": str(row.get("owner_project_root") or ""),
        "owner_thread_id": str(row.get("owner_thread_id") or ""),
        "owner_request_id": str(row.get("owner_request_id") or ""),
        "owner_tool": str(row.get("owner_tool") or ""),
        "age_s": round(max(0.0, now - float(row.get("created_at") or now)), 3),
        "expires_in_s": round(max(0.0, float(row.get("expires_at") or now) - now), 3),
        "metadata": metadata,
    }


_COORDINATOR: RuntimeLeaseCoordinator | None = None
_COORDINATOR_LOCK = threading.RLock()


def get_runtime_lease_coordinator() -> RuntimeLeaseCoordinator:
    global _COORDINATOR
    with _COORDINATOR_LOCK:
        if _COORDINATOR is None:
            _COORDINATOR = RuntimeLeaseCoordinator()
        return _COORDINATOR


def runtime_lease_summary(*, limit: int = 20) -> Dict[str, Any]:
    return get_runtime_lease_coordinator().summary(limit=limit)
