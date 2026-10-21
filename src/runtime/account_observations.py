"""账号事实的唯一写入口；旧浏览器状态只是窗口生命周期兼容投影。"""
from __future__ import annotations

from contextlib import closing, contextmanager
from contextvars import ContextVar
import json
from pathlib import Path
import sqlite3
import time
import uuid

from .paths import runtime_state_dir

STATES = {"unknown", "needs_login", "needs_verification", "cooldown", "auth_verified", "business_verified", "degraded"}
TTL = {"unknown": 0, "needs_login": 86400, "needs_verification": 7200, "cooldown": 1800,
       "auth_verified": 900, "business_verified": 1800, "degraded": 300}
_ATTEMPT = ContextVar("account_observation_attempt", default=None)


class AccountObservationStore:
    def __init__(self, path=None):
        self.path = Path(path or runtime_state_dir() / "account-observations.sqlite3")

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE IF NOT EXISTS observations(profile_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS deliveries(event_id TEXT PRIMARY KEY, status TEXT, updated_at REAL, receipt TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS notification_outbox(event_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, status TEXT NOT NULL, updated_at REAL NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS recovery_sessions(profile_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def observe(self, profile_id, platform, state, *, source, reason="", ttl_s=None, now=None, expected_revision=None):
        if not profile_id or state not in STATES:
            raise ValueError("invalid account observation")
        now = time.time() if now is None else now
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._observe(conn, profile_id, platform, state, source, reason, ttl_s, now, expected_revision)

    def _observe(self, conn, profile_id, platform, state, source, reason, ttl_s, now, expected_revision=None):
        row = conn.execute("SELECT payload FROM observations WHERE profile_id=?", (profile_id,)).fetchone()
        old = json.loads(row[0]) if row else {}
        revision = int(old.get("state_revision", 0))
        if now < old.get("observed_at", 0) or (expected_revision is not None and expected_revision != revision):
            return {**self.project(old, now), "accepted": False, "write_rejected": "stale_observation"}
        unchanged = old.get("state") == state and old.get("reason") == reason and old.get("expires_at", 0) > now
        value = {"schema": "knowledgeradar-account-observation/v1", "profile_id": profile_id, "platform": platform,
                 "state": state, "source": source, "reason": reason, "observed_at": now,
                 "expires_at": now + (TTL[state] if ttl_s is None else max(0, ttl_s)),
                 "state_revision": revision + 1,
                 "last_success_at": now if state == "business_verified" else old.get("last_success_at"),
                 "event_id": old["event_id"] if unchanged else uuid.uuid4().hex}
        conn.execute("INSERT OR REPLACE INTO observations VALUES(?,?)", (profile_id, json.dumps(value)))
        if state in {"needs_login", "needs_verification"}:
            conn.execute("UPDATE notification_outbox SET status='superseded',updated_at=? WHERE profile_id=? AND event_id!=? AND status IN ('pending','dispatched','shown','activated')", (now, profile_id, value["event_id"]))
            conn.execute("INSERT OR IGNORE INTO notification_outbox VALUES(?,?,?,?)", (value["event_id"], profile_id, "pending", now))
        else:
            conn.execute("UPDATE notification_outbox SET status='resolved',updated_at=? WHERE profile_id=? AND status IN ('pending','dispatched','shown','activated')", (now, profile_id))
        return {**self.project(value, now), "accepted": True}

    def begin_recovery(self, profile_id, *, owner_generation, lease_s=120, now=None):
        if not profile_id or not owner_generation:
            raise ValueError("recovery identity required")
        now = time.time() if now is None else now
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT payload FROM recovery_sessions WHERE profile_id=?", (profile_id,)).fetchone()
            previous = json.loads(row[0]) if row else {}
            if previous.get("status") == "active" and previous.get("lease_until", 0) > now:
                if previous.get("owner_generation") != owner_generation:
                    return {"status": "busy", "profile_id": profile_id}
                session = {**previous, "lease_until": now + lease_s}
            else:
                observation = conn.execute("SELECT payload FROM observations WHERE profile_id=?", (profile_id,)).fetchone()
                observation = json.loads(observation[0]) if observation else {}
                session = {"status": "active", "profile_id": profile_id, "session_id": uuid.uuid4().hex,
                           "owner_generation": owner_generation, "lease_until": now + lease_s,
                           "state_revision": observation.get("state_revision", 0)}
            conn.execute("INSERT OR REPLACE INTO recovery_sessions VALUES(?,?)", (profile_id, json.dumps(session)))
        return session

    def complete_recovery(self, session, platform, *, now=None):
        now = time.time() if now is None else now
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT payload FROM recovery_sessions WHERE profile_id=?", (session.get("profile_id"),)).fetchone()
            current = json.loads(row[0]) if row else {}
            if not current or current.get("status") != "active" or current.get("lease_until", 0) <= now or any(current.get(k) != session.get(k) for k in ("session_id", "owner_generation")):
                return {"accepted": False, "write_rejected": "stale_recovery_owner"}
            result = self._observe(conn, current["profile_id"], platform, "auth_verified", "recovery_probe", "auth_probe_passed", None, now, current["state_revision"])
            conn.execute("UPDATE recovery_sessions SET payload=? WHERE profile_id=?", (json.dumps({**current, "status": "completed" if result["accepted"] else "superseded"}), current["profile_id"]))
            return result

    def release_recovery(self, session):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT payload FROM recovery_sessions WHERE profile_id=?", (session.get("profile_id"),)).fetchone()
            current = json.loads(row[0]) if row else {}
            if current.get("status") == "active" and all(current.get(k) == session.get(k) for k in ("session_id", "owner_generation")):
                conn.execute("UPDATE recovery_sessions SET payload=? WHERE profile_id=?", (json.dumps({**current, "status": "released"}), session["profile_id"]))

    @staticmethod
    def project(value, now=None):
        now = time.time() if now is None else now
        fresh = bool(value and value.get("expires_at", 0) > now)
        state = value.get("state", "unknown") if fresh else "unknown"
        return {**value, "state": state, "fresh": fresh, "needs_action": state in {"needs_login", "needs_verification"},
                "verified": state == "business_verified", "business_readiness": "ready" if state == "business_verified" else "unknown"}

    def get(self, profile_id, now=None):
        if not self.path.is_file():
            return self.project({"profile_id": profile_id}, now)
        with closing(sqlite3.connect(self.path.as_uri()+"?mode=ro",uri=True,timeout=5)) as conn:
            row = conn.execute("SELECT payload FROM observations WHERE profile_id=?", (profile_id,)).fetchone()
        return self.project(json.loads(row[0]) if row else {"profile_id": profile_id}, now)

    def all(self):
        if not self.path.is_file():
            return []
        with closing(sqlite3.connect(self.path.as_uri()+"?mode=ro",uri=True,timeout=5)) as conn:
            rows = conn.execute("SELECT payload FROM observations").fetchall()
        return [self.project(json.loads(row[0])) for row in rows]


    def claim_notification(self, event_id):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            outbox = conn.execute("SELECT status FROM notification_outbox WHERE event_id=?", (event_id,)).fetchone()
            if outbox and outbox[0] not in {"pending", "dispatched"}:
                return False
            # shown/activated不重弹；启动后无显示回执的helper超过90秒可重放。
            conn.execute("DELETE FROM deliveries WHERE event_id=? AND status IN ('failed','claimed') AND updated_at<?", (event_id, time.time()-60))
            conn.execute("DELETE FROM deliveries WHERE event_id=? AND status='dispatched' AND updated_at<?", (event_id, time.time()-90))
            cursor = conn.execute("INSERT OR IGNORE INTO deliveries VALUES(?,?,?,?)", (event_id, "claimed", time.time(), "{}"))
        return cursor.rowcount == 1

    def delivery(self, event_id, status, receipt=None):
        if status not in {"dispatched", "failed", "shown", "activated", "acknowledged"}:
            raise ValueError("invalid notification status")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status FROM deliveries WHERE event_id=?", (event_id,)).fetchone()
            rank = {"claimed": 0, "failed": 1, "dispatched": 1, "shown": 2, "activated": 3, "acknowledged": 4}
            if row and rank.get(row[0], 0) > rank[status]:
                return
            conn.execute("INSERT OR REPLACE INTO deliveries VALUES(?,?,?,?)", (event_id, status, time.time(), json.dumps(receipt or {})))
            if status in {"dispatched", "shown", "activated", "acknowledged"}:
                conn.execute("UPDATE notification_outbox SET status=?,updated_at=? WHERE event_id=? AND status IN ('pending','dispatched','shown','activated')", (status, time.time(), event_id))
            elif status == "failed":
                conn.execute("UPDATE notification_outbox SET status='pending',updated_at=? WHERE event_id=? AND status='dispatched'", (time.time(), event_id))

    def pending_notifications(self, limit=20):
        if not self.path.is_file():
            return []
        with self.connect() as conn:
            rows = conn.execute("SELECT o.payload,n.event_id FROM notification_outbox n JOIN observations o ON o.profile_id=n.profile_id WHERE n.status='pending' OR (n.status='dispatched' AND n.updated_at<?) ORDER BY n.updated_at LIMIT ?", (time.time()-90, limit)).fetchall()
        return [value for row in rows if (value := self.project(json.loads(row[0])))["needs_action"] and value["event_id"] == row[1]]

    def notification_is_resolved(self, event_id):
        with self.connect() as conn:
            row = conn.execute("SELECT status FROM notification_outbox WHERE event_id=?", (event_id,)).fetchone()
        return bool(row and row[0] in {"resolved", "superseded", "acknowledged"})


def legacy_projection(observation):
    state = observation["state"]
    return {"profile_id": observation["profile_id"], "platform": observation.get("platform", ""),
            "state": "healthy" if state == "business_verified" else "available" if state == "auth_verified" else "degraded" if state.startswith("needs_") else state,
            "reason_code": observation.get("reason", "") if observation["fresh"] else "",
            "manual_action_required": observation["needs_action"], "cooldown_active": state == "cooldown",
            "cooldown_remaining_s": max(0, int(observation.get("expires_at", 0)-time.time())) if state=="cooldown" else 0,
            "safe_to_switch_account": state in {"degraded", "needs_login"}, "recent_events": [],
            "source": "AccountObservation", "last_observed_at": observation.get("observed_at", "")}


@contextmanager
def account_observation_attempt(profile_id, *, revision=None):
    store = AccountObservationStore()
    marker = {"profile_id": profile_id, "revision": store.get(profile_id).get("state_revision", 0) if revision is None else revision}
    token = _ATTEMPT.set(marker)
    try:
        yield marker
    finally:
        _ATTEMPT.reset(token)


def observe_account(profile_id, platform, state, **kwargs):
    marker = _ATTEMPT.get()
    if marker and marker["profile_id"] == profile_id:
        kwargs.setdefault("expected_revision", marker["revision"])
    result = AccountObservationStore().observe(profile_id, platform, state, **kwargs)
    if marker and marker["profile_id"] == profile_id and result.get("accepted"):
        marker["revision"] = result["state_revision"]
    return result
