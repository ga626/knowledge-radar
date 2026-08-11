"""Browser session state and event ledger for managed Chrome lifecycles.

This module is intentionally lightweight: it records observable browser
session state without launching browsers or knowing platform collection logic.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Dict, List, Optional

from runtime.paths import runtime_log_dir, runtime_state_dir


SCHEMA_VERSION = "knowledgeradar-browser-sessions/v1"
EVENT_SCHEMA_VERSION = "knowledgeradar-browser-session-event/v1"
MANUAL_ACTION_SCHEMA_VERSION = "knowledgeradar-manual-action/v1"

SESSION_STATES = {
    "UNINITIALIZED",
    "STARTING_SILENT",
    "READY_SILENT",
    "ACTIVE_AUTOMATION",
    "NEEDS_USER",
    "USER_INTERACTING",
    "USER_DONE_VERIFYING",
    "QUIESCING",
    "CLOSED",
    "FAILED",
}


def _runtime_root() -> Path:
    return runtime_state_dir()


def default_session_state_path() -> Path:
    return Path(os.environ.get("KR_BROWSER_SESSION_STATE_PATH") or (_runtime_root() / "browser-sessions.json"))


def default_session_event_path() -> Path:
    return Path(os.environ.get("KR_BROWSER_SESSION_EVENT_PATH") or (runtime_log_dir() / "browser-session-events.jsonl"))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_ts() -> float:
    return time.time()


def stable_hash(value: object) -> str:
    text = str(value or "")
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _session_id(platform: str, debug_port: str, profile_dir: str) -> str:
    raw = json.dumps(
        {
            "platform": platform,
            "debug_port": debug_port,
            "profile_dir": str(profile_dir or "").replace("\\", "/").lower(),
            "created_bucket": int(now_ts() // 3600),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return f"browser-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"


def _session_matches_scope(
    existing: "BrowserSession",
    *,
    platform: str,
    debug_port: str = "",
    profile_dir: str = "",
    profile_id: str = "",
    account_slot: str = "",
) -> bool:
    if existing.platform != platform or existing.state in {"CLOSED", "FAILED"}:
        return False
    requested_profile_hash = stable_hash(profile_dir)
    qualifiers = [
        bool(debug_port) and existing.debug_port == str(debug_port),
        bool(profile_id) and existing.profile_id == str(profile_id),
        bool(account_slot) and existing.account_slot == str(account_slot),
        bool(requested_profile_hash) and existing.profile_dir_hash == requested_profile_hash,
    ]
    if any(qualifiers):
        return True
    return not any([debug_port, profile_dir, profile_id, account_slot])


def _action_type_from_reason(reason: str) -> str:
    text = str(reason or "").lower()
    if "captcha" in text or "verify" in text or "verification" in text or "验证" in text or "风控" in text:
        return "security_verification"
    if "login" in text or "登录" in text or "扫码" in text:
        return "login"
    return "manual_check"


def _manual_action_status(state: str) -> str:
    if state == "NEEDS_USER":
        return "pending_user"
    if state == "USER_INTERACTING":
        return "waiting_for_user"
    if state == "USER_DONE_VERIFYING":
        return "verifying"
    if state == "READY_SILENT":
        return "resolved"
    if state == "FAILED":
        return "failed"
    return "observed"


def _human_reason(reason: str) -> str:
    text = str(reason or "").lower()
    if "captcha" in text or "verify" in text or "verification" in text or "验证" in text:
        return "安全验证"
    if "login" in text or "扫码" in text or "登录" in text:
        return "登录失效"
    return "需要人工确认"


def manual_action_request_from_session(
    session: Dict[str, Any] | "BrowserSession",
    *,
    reason_code: str = "",
    trigger_evidence: Optional[List[str]] = None,
) -> Dict[str, Any]:
    data = session.to_dict() if isinstance(session, BrowserSession) else dict(session or {})
    reason = reason_code or str(data.get("reason") or "manual_action_required")
    platform = str(data.get("platform") or "")
    identity = dict((data.get("metadata") or {}).get("account_identity") or {})
    display_label = str(identity.get("display_label") or data.get("account_slot") or data.get("profile_id") or platform)
    masked_hint = str(identity.get("masked_hint") or "")
    # XHS recovery sessions are profile-scoped (``xhs:<profile_id>``).  Keep
    # that internal resource key out of the human-facing prompt while still
    # preserving it in the interaction/session id.
    platform_label = "小红书" if platform.split(":", 1)[0] in {"xhs", "xiaohongshu"} else platform
    human_message = f"{platform_label}“{display_label}”需要扫码登录。原因：{_human_reason(reason)}。已打开对应窗口；其余不依赖该平台的工作会继续。"
    return {
        "schema_version": MANUAL_ACTION_SCHEMA_VERSION,
        "interaction_id": str(data.get("session_id") or ""),
        "platform": platform,
        "profile_id": str(data.get("profile_id") or ""),
        "account_slot": str(data.get("account_slot") or ""),
        "profile_dir_hash": str(data.get("profile_dir_hash") or ""),
        "display_label": display_label,
        "masked_hint": masked_hint,
        "debug_port": str(data.get("debug_port") or ""),
        "action_type": _action_type_from_reason(reason),
        "reason_code": reason,
        "status": _manual_action_status(str(data.get("state") or "")),
        "target_url": str(data.get("target_url") or ""),
        "browser": {
            "auto_opened": str(data.get("state") or "") in {"USER_INTERACTING", "USER_DONE_VERIFYING"},
            "desired_visibility": str(data.get("desired_visibility") or ""),
        },
        "trigger_evidence": list(trigger_evidence or []),
        "retry_tool": "health_check",
        "retry_mode": f"complete_browser_interaction:{platform}" if platform else "complete_browser_interaction",
        "original_tool": str((data.get("metadata") or {}).get("original_tool") or ""),
        "original_args_hash": str((data.get("metadata") or {}).get("original_args_hash") or ""),
        "resume_policy": str((data.get("metadata") or {}).get("resume_policy") or "retry_once_after_complete"),
        "blocks_only_platform": True,
        "notification_required": True,
        "max_auto_retries": 1,
        "human_message": human_message,
        "updated_at_iso": str(data.get("updated_at_iso") or ""),
    }


def _json_dumps(data: Dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2)


def _json_loads(text: str | None) -> Dict[str, Any]:
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _sanitize_metadata(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    data = dict(metadata or {})
    for key in list(data.keys()):
        lowered = key.lower()
        if any(marker in lowered for marker in ("cookie", "token", "key", "secret", "password", "xsec")):
            data[key] = "[REDACTED]"
    return data


@dataclass
class BrowserSession:
    session_id: str
    platform: str
    profile_id: str = ""
    profile_dir_hash: str = ""
    account_slot: str = ""
    debug_port: str = ""
    pid: Optional[int] = None
    target_url: str = ""
    desired_visibility: str = "silent"
    state: str = "UNINITIALIZED"
    reason: str = ""
    started_at: float = field(default_factory=now_ts)
    updated_at: float = field(default_factory=now_ts)
    last_activity_at: float = field(default_factory=now_ts)
    deadline_at: Optional[float] = None
    last_probe_result: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["started_at_iso"] = datetime.fromtimestamp(self.started_at, timezone.utc).isoformat()
        data["updated_at_iso"] = datetime.fromtimestamp(self.updated_at, timezone.utc).isoformat()
        return data


class BrowserSessionStore:
    def __init__(self, state_path: Optional[Path] = None, event_path: Optional[Path] = None) -> None:
        self.state_path = state_path or default_session_state_path()
        self.event_path = event_path or default_session_event_path()
        self._lock = threading.RLock()
        self._sessions: Dict[str, BrowserSession] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.state_path.is_file():
            return
        data = _json_loads(self.state_path.read_text(encoding="utf-8"))
        for item in data.get("sessions", []) or []:
            if not isinstance(item, dict):
                continue
            try:
                session = BrowserSession(
                    session_id=str(item.get("session_id") or ""),
                    platform=str(item.get("platform") or ""),
                    profile_id=str(item.get("profile_id") or ""),
                    profile_dir_hash=str(item.get("profile_dir_hash") or ""),
                    account_slot=str(item.get("account_slot") or ""),
                    debug_port=str(item.get("debug_port") or ""),
                    pid=int(item["pid"]) if item.get("pid") is not None else None,
                    target_url=str(item.get("target_url") or ""),
                    desired_visibility=str(item.get("desired_visibility") or "silent"),
                    state=str(item.get("state") or "UNINITIALIZED"),
                    reason=str(item.get("reason") or ""),
                    started_at=float(item.get("started_at") or now_ts()),
                    updated_at=float(item.get("updated_at") or now_ts()),
                    last_activity_at=float(item.get("last_activity_at") or now_ts()),
                    deadline_at=float(item["deadline_at"]) if item.get("deadline_at") is not None else None,
                    last_probe_result=item.get("last_probe_result") if isinstance(item.get("last_probe_result"), dict) else {},
                    metadata=item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
                )
                if session.session_id:
                    self._sessions[session.session_id] = session
            except Exception:
                continue

    def _persist(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": utc_now_iso(),
            "sessions": [session.to_dict() for session in self._sessions.values()],
        }
        self.state_path.write_text(_json_dumps(payload), encoding="utf-8")

    def _record_event(self, session: BrowserSession, event: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.event_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "timestamp": utc_now_iso(),
            "session_id": session.session_id,
            "platform": session.platform,
            "event": event,
            "state": session.state,
            "desired_visibility": session.desired_visibility,
            "debug_port": session.debug_port,
            "pid": session.pid,
            "reason": session.reason,
            "profile_id": session.profile_id,
            "account_slot": session.account_slot,
            "profile_dir_hash": session.profile_dir_hash,
            "metadata": _sanitize_metadata(metadata),
        }
        with self.event_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        return payload

    def upsert(
        self,
        *,
        platform: str,
        debug_port: str = "",
        profile_dir: str = "",
        state: str = "UNINITIALIZED",
        desired_visibility: str = "silent",
        reason: str = "",
        pid: Optional[int] = None,
        target_url: str = "",
        profile_id: str = "",
        account_slot: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        event: str = "session_upserted",
    ) -> Dict[str, Any]:
        with self._lock:
            self._ensure_loaded()
            sid = ""
            for existing in self._sessions.values():
                if _session_matches_scope(
                    existing,
                    platform=platform,
                    debug_port=debug_port,
                    profile_dir=profile_dir,
                    profile_id=profile_id,
                    account_slot=account_slot,
                ):
                    sid = existing.session_id
                    break
            if not sid:
                sid = _session_id(platform, debug_port, profile_dir)
            session = self._sessions.get(sid) or BrowserSession(session_id=sid, platform=platform)
            session.debug_port = str(debug_port or session.debug_port or "")
            session.profile_dir_hash = stable_hash(profile_dir) or session.profile_dir_hash
            session.profile_id = profile_id or session.profile_id
            session.account_slot = account_slot or session.account_slot
            session.pid = pid if pid is not None else session.pid
            session.target_url = target_url or session.target_url
            session.desired_visibility = desired_visibility or session.desired_visibility
            session.state = state if state in SESSION_STATES else session.state
            session.reason = reason or session.reason
            session.updated_at = now_ts()
            session.last_activity_at = session.updated_at
            if metadata:
                session.metadata.update(_sanitize_metadata(metadata))
            self._sessions[sid] = session
            self._persist()
            self._record_event(session, event, metadata)
            return session.to_dict()

    def transition(
        self,
        platform: str,
        state: str,
        *,
        profile_id: str = "",
        account_slot: str = "",
        profile_dir: str = "",
        desired_visibility: Optional[str] = None,
        reason: str = "",
        pid: Optional[int] = None,
        last_probe_result: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        event: str = "session_transitioned",
    ) -> Dict[str, Any]:
        with self._lock:
            self._ensure_loaded()
            session = next(
                (
                    item
                    for item in sorted(self._sessions.values(), key=lambda candidate: candidate.updated_at, reverse=True)
                    if _session_matches_scope(
                        item,
                        platform=platform,
                        profile_id=profile_id,
                        account_slot=account_slot,
                        profile_dir=profile_dir,
                    )
                ),
                None,
            )
            if session is None and not any([profile_id, account_slot, profile_dir]):
                session = self.latest_for_platform(platform)
            if session is None:
                session = BrowserSession(
                    session_id=_session_id(platform, "", profile_dir),
                    platform=platform,
                    profile_id=str(profile_id or ""),
                    account_slot=str(account_slot or ""),
                    profile_dir_hash=stable_hash(profile_dir),
                )
            if state in SESSION_STATES:
                session.state = state
            if desired_visibility:
                session.desired_visibility = desired_visibility
            if reason:
                session.reason = reason
            if pid is not None:
                session.pid = pid
            if last_probe_result is not None:
                session.last_probe_result = _sanitize_metadata(last_probe_result)
            if metadata:
                session.metadata.update(_sanitize_metadata(metadata))
            session.updated_at = now_ts()
            session.last_activity_at = session.updated_at
            self._sessions[session.session_id] = session
            self._persist()
            self._record_event(session, event, metadata)
            return session.to_dict()

    def set_deadline(
        self,
        platform: str,
        deadline_at: Optional[float],
        *,
        profile_id: str = "",
        account_slot: str = "",
        profile_dir: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        event: str = "session_deadline_updated",
    ) -> Dict[str, Any]:
        """Persist an idle deadline without treating it as browser activity."""
        with self._lock:
            self._ensure_loaded()
            session = next(
                (
                    item
                    for item in sorted(self._sessions.values(), key=lambda candidate: candidate.updated_at, reverse=True)
                    if _session_matches_scope(
                        item,
                        platform=platform,
                        profile_id=profile_id,
                        account_slot=account_slot,
                        profile_dir=profile_dir,
                    )
                ),
                None,
            )
            if session is None:
                return {}
            session.deadline_at = float(deadline_at) if deadline_at is not None else None
            if metadata:
                session.metadata.update(_sanitize_metadata(metadata))
            self._sessions[session.session_id] = session
            self._persist()
            self._record_event(session, event, metadata)
            return session.to_dict()

    def latest_for_platform(self, platform: str) -> Optional[BrowserSession]:
        self._ensure_loaded()
        candidates = [session for session in self._sessions.values() if session.platform == platform]
        if not candidates:
            return None
        candidates.sort(key=lambda item: item.updated_at, reverse=True)
        return candidates[0]

    def record_event(self, platform: str, event: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self._lock:
            self._ensure_loaded()
            session = self.latest_for_platform(platform)
            if session is None:
                session = BrowserSession(session_id=_session_id(platform, "", ""), platform=platform)
                self._sessions[session.session_id] = session
                self._persist()
            return self._record_event(session, event, metadata)

    def recent_events(self, limit: int = 20) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit or 20), 200))
        if not self.event_path.is_file():
            return []
        lines = self.event_path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
        events: List[Dict[str, Any]] = []
        for line in lines:
            try:
                item = json.loads(line)
            except Exception:
                continue
            if isinstance(item, dict):
                events.append(item)
        return events

    def compact_terminal_sessions(self, *, retain_closed: int = 100, retain_failed: int = 20, dry_run: bool = False) -> Dict[str, Any]:
        """Archive old terminal records without touching active user interactions."""
        with self._lock:
            self._ensure_loaded()
            retained: Dict[str, BrowserSession] = {}
            removed: list[BrowserSession] = []
            buckets = {"CLOSED": max(0, int(retain_closed)), "FAILED": max(0, int(retain_failed))}
            for state, limit in buckets.items():
                rows = sorted((item for item in self._sessions.values() if item.state == state), key=lambda item: item.updated_at, reverse=True)
                for item in rows[:limit]:
                    retained[item.session_id] = item
                removed.extend(rows[limit:])
            for item in self._sessions.values():
                if item.state not in buckets:
                    retained[item.session_id] = item
            if not dry_run:
                self._sessions = retained
                self._persist()
                for item in removed:
                    self._record_event(item, "session_compacted", {"previous_state": item.state})
            return {
                "schema_version": SCHEMA_VERSION,
                "status": "ok",
                "removed_count": len(removed),
                "retained_closed": sum(1 for item in retained.values() if item.state == "CLOSED"),
                "retained_failed": sum(1 for item in retained.values() if item.state == "FAILED"),
                "protected_active_count": sum(1 for item in retained.values() if item.state not in {"CLOSED", "FAILED"}),
                "action": "dry_run" if dry_run else "compacted",
            }

    def summary(self, recent_events_limit: int = 20) -> Dict[str, Any]:
        with self._lock:
            self._ensure_loaded()
            sessions = [session.to_dict() for session in self._sessions.values()]
        counts: Dict[str, int] = {}
        for session in sessions:
            counts[session.get("state") or "unknown"] = counts.get(session.get("state") or "unknown", 0) + 1
        pending = [
            session
            for session in sessions
            if session.get("state") in {"NEEDS_USER", "USER_INTERACTING", "USER_DONE_VERIFYING"}
        ]
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "ok",
            "state_path": str(self.state_path),
            "event_path": str(self.event_path),
            "total": len(sessions),
            "counts": counts,
            "pending_human_action": len(pending),
            "pending_interactions": [manual_action_request_from_session(session) for session in pending],
            "sessions": sorted(sessions, key=lambda item: item.get("updated_at") or 0, reverse=True)[:20],
            "recent_events": self.recent_events(recent_events_limit),
        }


_STORE = BrowserSessionStore()


def get_browser_session_store() -> BrowserSessionStore:
    return _STORE


def upsert_browser_session(**kwargs: Any) -> Dict[str, Any]:
    return _STORE.upsert(**kwargs)


def transition_browser_session(platform: str, state: str, **kwargs: Any) -> Dict[str, Any]:
    return _STORE.transition(platform, state, **kwargs)


def set_browser_session_deadline(platform: str, deadline_at: Optional[float], **kwargs: Any) -> Dict[str, Any]:
    return _STORE.set_deadline(platform, deadline_at, **kwargs)


def record_browser_event(platform: str, event: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return _STORE.record_event(platform, event, metadata)


def browser_sessions_summary(limit: int = 20) -> Dict[str, Any]:
    return _STORE.summary(recent_events_limit=limit)


def compact_terminal_browser_sessions(**kwargs: Any) -> Dict[str, Any]:
    return _STORE.compact_terminal_sessions(**kwargs)
