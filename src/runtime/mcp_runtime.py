"""Small, durable control plane for safe KnowledgeRadar MCP replacement.

The state is only touched when a code change needs a new server generation or a
real protocol call cannot reach the local service.  It deliberately has no
heartbeat, scheduler, or background retry loop.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Callable

from runtime.browser_sessions import browser_sessions_summary
from runtime.leases import default_owner, get_runtime_lease_coordinator
from runtime.paths import project_root
from runtime.tasks import get_task_store


SCHEMA = "knowledgeradar-mcp-runtime/v1"
STATE_FILE = "knowledgeradar-mcp-runtime.json"
SWITCH_KIND = "mcp_runtime_switch"
SWITCH_KEY = "shared_server"
COOLDOWN_S = 30


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def state_path(root: Path | None = None) -> Path:
    base = root or project_root()
    return Path(base) / "runtime" / "state" / STATE_FILE


def _read_state(root: Path | None = None) -> dict[str, Any]:
    path = state_path(root)
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_state(value: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": SCHEMA, "updated_at": _utc_now(), **value}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return payload


def source_fingerprint(root: Path | None = None) -> str:
    base = Path(root or project_root())
    digest = hashlib.sha256()
    for relative in ("src/server.py", "src/runtime/mcp_runtime.py"):
        path = base / relative
        digest.update(relative.encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"missing")
    return digest.hexdigest()[:16]


def runtime_activity(root: Path | None = None) -> dict[str, Any]:
    """Read the three durable signals that make replacement unsafe."""

    task_summary = get_task_store().summary(recent_limit=5)
    lease_summary = get_runtime_lease_coordinator().summary(limit=50)
    browser_summary = browser_sessions_summary(limit=20)
    active_leases = [
        lease
        for lease in lease_summary.get("active", [])
        if str(lease.get("resource_kind") or "") != SWITCH_KIND
    ]
    active_tasks = int(task_summary.get("active") or 0)
    pending_human = int(browser_summary.get("pending_human_action") or 0)
    reasons: list[str] = []
    if active_tasks:
        reasons.append("active_tasks")
    if active_leases:
        reasons.append("active_resource_leases")
    if pending_human:
        reasons.append("pending_browser_interaction")
    return {
        "safe": not reasons,
        "reasons": reasons,
        "active_tasks": active_tasks,
        "active_leases": active_leases,
        "pending_human_action": pending_human,
    }


def request_runtime_refresh(root: Path | None = None, *, reason: str, requested_by: str) -> dict[str, Any]:
    current = _read_state(root)
    return _write_state(
        {
            **current,
            "status": "restart_pending",
            "pending": True,
            "reason": reason,
            "requested_by": requested_by,
            "requested_at": _utc_now(),
            "retry_not_before": 0,
            "source_fingerprint": source_fingerprint(root),
        },
        root,
    )


def switch_in_progress(root: Path | None = None) -> bool:
    del root  # Lease storage follows the configured runtime path.
    for lease in get_runtime_lease_coordinator().active_leases(limit=20):
        if lease.get("resource_kind") == SWITCH_KIND and lease.get("resource_key") == SWITCH_KEY:
            return True
    return False


def wait_for_switch_release(timeout_s: float = 30.0) -> bool:
    """Rare local lock wait used only by work that begins during a replacement."""

    deadline = time.monotonic() + max(0.0, timeout_s)
    while switch_in_progress():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def try_apply_pending_runtime(
    root: Path | None = None,
    *,
    restart: Callable[[Path], dict[str, Any]],
    now: float | None = None,
) -> dict[str, Any]:
    """Apply one requested switch if the shared runtime is currently idle.

    Callers never wait for a safe window.  A busy runtime remains pending and a
    later real MCP call may make one new attempt.
    """

    base = Path(root or project_root())
    current = _read_state(base)
    if not current.get("pending"):
        return {"schema": SCHEMA, "status": "PASS", "action": "noop", "reason": "no_restart_pending"}
    timestamp = float(now if now is not None else time.time())
    retry_not_before = float(current.get("retry_not_before") or 0)
    if timestamp < retry_not_before:
        return {
            "schema": SCHEMA,
            "status": "WARN",
            "action": "deferred",
            "reason": "recovery_cooldown",
            "retry_after_s": round(retry_not_before - timestamp, 3),
        }

    before = runtime_activity(base)
    if not before["safe"]:
        state = _write_state({**current, "status": "restart_pending", "pending": True, "last_activity": before}, base)
        return {"schema": SCHEMA, "status": "PASS", "action": "deferred", "reason": "runtime_busy", "activity": before, "state": state}

    coordinator = get_runtime_lease_coordinator()
    lease = coordinator.acquire_exclusive(
        SWITCH_KIND,
        SWITCH_KEY,
        owner=default_owner("mcp_runtime_switch", project_root=str(base)),
        ttl_s=90,
        metadata={"reason": current.get("reason", ""), "requested_by": current.get("requested_by", "")},
    )
    if not lease.acquired:
        return {
            "schema": SCHEMA,
            "status": "PASS",
            "action": "deferred",
            "reason": "switch_already_in_progress",
            "retry_after_s": lease.retry_after_s,
        }
    try:
        after_lock = runtime_activity(base)
        if not after_lock["safe"]:
            state = _write_state({**current, "status": "restart_pending", "pending": True, "last_activity": after_lock}, base)
            return {"schema": SCHEMA, "status": "PASS", "action": "deferred", "reason": "runtime_became_busy", "activity": after_lock, "state": state}
        restart_result = restart(base)
        if int(restart_result.get("returncode", 1)) == 0:
            state = _write_state(
                {
                    **current,
                    "status": "applied",
                    "pending": False,
                    "applied_at": _utc_now(),
                    "retry_not_before": 0,
                    "restart": restart_result,
                    "source_fingerprint": source_fingerprint(base),
                },
                base,
            )
            return {"schema": SCHEMA, "status": "PASS", "action": "restarted", "restart": restart_result, "state": state}
        state = _write_state(
            {
                **current,
                "status": "recovery_failed",
                "pending": True,
                "retry_not_before": timestamp + COOLDOWN_S,
                "last_failure": restart_result,
            },
            base,
        )
        return {"schema": SCHEMA, "status": "WARN", "action": "failed", "reason": "restart_failed", "restart": restart_result, "state": state}
    finally:
        coordinator.release(lease.lease_id)
