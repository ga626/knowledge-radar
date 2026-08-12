"""Read-only profile/account registry for browser-backed platforms.

The first version is intentionally small: it does not move profiles, read
secrets, or switch accounts. It gives health/capabilities a single place to
describe which profile belongs to which browser channel and role.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
import tempfile
import time
from typing import Any, Dict, List, Optional

from .platform_diagnostics import stable_hash


SRC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(SRC_ROOT)
DEFAULT_REGISTRY_PATH = os.path.join(REPO_ROOT, "config", "profile_registry.json")
DEFAULT_PROFILE_STATE_FILENAME = "knowledgeradar-profile-state.json"
PLATFORM_ALIASES = {
    "xhs": "xiaohongshu",
    "red": "xiaohongshu",
    "小红书": "xiaohongshu",
    "boss直聘": "boss",
    "boss": "boss",
    "zhipin": "boss",
    "猎聘": "liepin",
    "liepin": "liepin",
    "脉脉": "maimai",
    "maimai": "maimai",
    "知乎": "zhihu",
    "zhihu": "zhihu",
    "b站": "bilibili",
    "哔哩哔哩": "bilibili",
    "bilibili": "bilibili",
    "cnki": "cnki",
    "知网": "cnki",
}


def registry_path() -> str:
    configured = os.environ.get("KR_PROFILE_REGISTRY_PATH", "").strip()
    if configured:
        return configured
    data_root = os.environ.get("KR_DATA_ROOT", "").strip()
    if data_root:
        return os.path.join(data_root, "config", "profile_registry.json")
    return DEFAULT_REGISTRY_PATH


def profile_state_path() -> str:
    configured = os.environ.get("KR_PROFILE_STATE_PATH", "").strip()
    if configured:
        return configured
    return os.path.join(_runtime_dir(), DEFAULT_PROFILE_STATE_FILENAME)


def normalize_platform_id(platform: str) -> str:
    value = str(platform or "").strip().lower()
    return PLATFORM_ALIASES.get(value, value)


def platform_matches(value: str, platform: str) -> bool:
    return normalize_platform_id(value) == normalize_platform_id(platform)


def raw_registry_for_platform(platform: str, path: str | None = None) -> Dict[str, Any]:
    """Return private registry rows scoped to one platform for local runtime use.

    This is deliberately an internal-data helper.  It must not be returned by
    an MCP tool, health endpoint, log record, or distributable artifact.
    """
    registry = profile_registry_internal(path)
    raw = registry.get("raw") or {}
    return {
        "schema": raw.get("schema", ""),
        "platform": normalize_platform_id(platform),
        "accounts": [
            row
            for row in raw.get("accounts", []) or []
            if isinstance(row, dict) and platform_matches(str(row.get("platform") or ""), platform)
        ],
        "profiles": [
            row
            for row in raw.get("profiles", []) or []
            if isinstance(row, dict) and platform_matches(str(row.get("platform") or ""), platform)
        ],
        "bindings": [
            row
            for row in raw.get("bindings", []) or []
            if isinstance(row, dict) and platform_matches(str(row.get("platform") or ""), platform)
        ],
        "policy": raw.get("policy", {}) if isinstance(raw, dict) else {},
        "summary_status": registry.get("status"),
        "runtime_state": registry.get("runtime_state") or {},
    }


def profiles_for_platform(platform: str, path: str | None = None) -> List[Dict[str, Any]]:
    summary = profile_registry_summary(path)
    return [
        row
        for row in summary.get("profiles", []) or []
        if isinstance(row, dict) and platform_matches(str(row.get("platform") or ""), platform)
    ]


def accounts_for_platform(platform: str, path: str | None = None) -> List[Dict[str, Any]]:
    return list(raw_registry_for_platform(platform, path).get("accounts") or [])


def bindings_for_platform(platform: str, path: str | None = None) -> List[Dict[str, Any]]:
    return list(raw_registry_for_platform(platform, path).get("bindings") or [])


def profile_registry_internal(path: str | None = None) -> Dict[str, Any]:
    """Load private registry data for local profile/account runtime code only."""
    path = path or registry_path()
    if not os.path.isfile(path):
        return {
            "schema": "knowledgeradar-profile-registry-summary/v1",
            "status": "not_configured",
            "registry_path": path,
            "profiles": [],
            "counts": {"total": 0, "available": 0, "missing": 0},
            "notes": ["profile registry file is optional in v1"],
        }

    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as exc:
        return {
            "schema": "knowledgeradar-profile-registry-summary/v1",
            "status": "degraded",
            "registry_path": path,
            "error": str(exc),
            "profiles": [],
            "counts": {"total": 0, "available": 0, "missing": 0},
        }

    profiles = data.get("profiles", []) if isinstance(data, dict) else []
    summarized: List[Dict[str, Any]] = []
    state_summary = profile_state_summary()
    profile_states = {
        str(row.get("profile_id") or ""): row
        for row in state_summary.get("profiles", [])
        if isinstance(row, dict)
    }
    available = 0
    missing = 0
    for row in profiles:
        if not isinstance(row, dict):
            continue
        profile_dir = _resolve_profile_dir(str(row.get("profile_dir") or ""))
        exists = bool(profile_dir and os.path.isdir(profile_dir))
        if exists:
            available += 1
        else:
            missing += 1
        profile_id = str(row.get("profile_id") or "")
        state = profile_states.get(profile_id, {})
        summarized.append(
            {
                "platform": str(row.get("platform") or ""),
                "profile_id": profile_id,
                "role": str(row.get("role") or ""),
                "browser_base": str(row.get("browser_base") or ""),
                "channel_id": str(row.get("channel_id") or ""),
                "status": str(row.get("status") or "unknown"),
                "runtime_state": state.get("state", "unknown") if state else "unobserved",
                "manual_action_required": bool(state.get("manual_action_required", False)) if state else False,
                "cooldown_active": bool(state.get("cooldown_active", False)) if state else False,
                "account_slot": str(row.get("account_slot") or ""),
                "account_id_hash": _account_hash(row),
                "profile_dir_hash": stable_hash(profile_dir),
                "profile_exists": exists,
                "launch_policy": str(row.get("launch_policy") or ""),
                "risk_level": str(row.get("risk_level") or ""),
                "auto_switch": str(row.get("auto_switch") or "disabled"),
                "account_pool_member": bool(row.get("account_pool_member", False)),
                "main_chain_allowed": bool(row.get("main_chain_allowed", False)),
            }
        )

    status = "ok" if summarized and missing == 0 else ("degraded" if summarized else "not_configured")
    return {
        "schema": "knowledgeradar-profile-registry-summary/v1",
        "status": status,
        "registry_path": path,
        "version": str(data.get("version") or "1") if isinstance(data, dict) else "1",
        "raw": {
            "schema": data.get("schema", "") if isinstance(data, dict) else "",
            "accounts": data.get("accounts", []) if isinstance(data, dict) else [],
            "profiles": data.get("profiles", []) if isinstance(data, dict) else [],
            "bindings": data.get("bindings", []) if isinstance(data, dict) else [],
            "policy": data.get("policy", {}) if isinstance(data, dict) else {},
        },
        "profiles": summarized,
        "counts": {
            "total": len(summarized),
            "available": available,
            "missing": missing,
        },
        "policy": data.get("policy", {}) if isinstance(data, dict) else {},
        "runtime_state": state_summary,
    }


def profile_registry_summary(path: str | None = None) -> Dict[str, Any]:
    """Return the public, sanitized profile-registry DTO for diagnostics/MCP.

    The local registry can contain account identifiers and absolute browser
    directories.  This projection intentionally omits raw records and file
    paths while preserving enough readiness information for product support.
    """
    internal = profile_registry_internal(path)
    runtime_state = internal.get("runtime_state") or {}
    public_runtime_state = dict(runtime_state)
    public_runtime_state.pop("state_path", None)
    public_runtime_state.pop("error", None)
    public_runtime_state["profiles"] = [
        {key: value for key, value in row.items() if key not in {"notes", "recent_events"}}
        for row in runtime_state.get("profiles", [])
        if isinstance(row, dict)
    ]
    return {
        "schema": internal.get("schema", "knowledgeradar-profile-registry-summary/v1"),
        "status": internal.get("status", "not_configured"),
        "version": internal.get("version", "1"),
        "profiles": list(internal.get("profiles") or []),
        "counts": dict(internal.get("counts") or {}),
        "policy": dict(internal.get("policy") or {}),
        "runtime_state": public_runtime_state,
        "notes": ["profile registry is optional; configure it locally when browser-backed features are needed"],
    }


def select_main_chain_profile(platform: str = "xiaohongshu", path: str | None = None) -> Dict[str, Any]:
    """Return the one explicitly admitted main-chain profile, if any.

    This does not switch accounts or launch a browser. It only resolves the
    registry's explicit admission state into a profile directory that runtime
    launchers may use.
    """
    summary = profile_registry_internal(path)
    if summary.get("status") not in {"ok", "degraded"}:
        return {
            "status": "not_configured",
            "platform": platform,
            "reason": "profile registry unavailable",
        }
    raw = raw_registry_for_platform(platform, path)
    raw_profiles = [
        row
        for row in raw.get("profiles", []) or []
        if isinstance(row, dict)
        and bool(row.get("main_chain_allowed", False))
    ]
    if not raw_profiles:
        return {
            "status": "not_configured",
            "platform": normalize_platform_id(platform),
            "reason": "no profile has main_chain_allowed=true",
        }
    if len(raw_profiles) > 1:
        order = list((raw.get("policy") or {}).get("readonly_route_order") or [])
        order_index = {str(slot): idx for idx, slot in enumerate(order)}
        raw_profiles.sort(
            key=lambda row: (
                0 if str(row.get("role") or "") == "primary" else 1,
                order_index.get(str(row.get("account_slot") or ""), 999),
                str(row.get("profile_id") or ""),
            )
        )

    runtime_by_profile = {
        str(row.get("profile_id") or ""): row
        for row in (summary.get("runtime_state") or {}).get("profiles", [])
        if isinstance(row, dict)
    }
    last_blocked: Dict[str, Any] = {}
    for raw_profile in raw_profiles:
        profile_id = str(raw_profile.get("profile_id") or "")
        runtime = runtime_by_profile.get(profile_id, {})
        if runtime.get("manual_action_required"):
            last_blocked = {
                "status": "blocked",
                "platform": normalize_platform_id(platform),
                "profile_id": profile_id,
                "reason": "manual_action_required",
            }
            continue
        if runtime.get("cooldown_active"):
            last_blocked = {
                "status": "blocked",
                "platform": normalize_platform_id(platform),
                "profile_id": profile_id,
                "reason": "cooldown_active",
            }
            continue
        if runtime.get("safe_to_switch_account") and str(runtime.get("state") or "") != "healthy":
            last_blocked = {
                "status": "blocked",
                "platform": normalize_platform_id(platform),
                "profile_id": profile_id,
                "reason": "safe_to_switch_account",
            }
            continue

        profile_dir = _resolve_profile_dir(str(raw_profile.get("profile_dir") or ""))
        if not profile_dir or not os.path.isdir(profile_dir):
            last_blocked = {
                "status": "degraded",
                "platform": platform,
                "profile_id": profile_id,
                "reason": "profile_dir_missing",
                "profile_dir_hash": stable_hash(profile_dir),
            }
            continue
        return {
            "status": "ok",
            "platform": normalize_platform_id(platform),
            "profile_id": profile_id,
            "account_slot": str(raw_profile.get("account_slot") or ""),
            "profile_dir": profile_dir,
            "profile_dir_hash": stable_hash(profile_dir),
            "channel_id": str(raw_profile.get("channel_id") or ""),
            "runtime_state": runtime.get("state", "unobserved") if runtime else "unobserved",
        }
    return last_blocked or {
        "status": "blocked",
        "platform": normalize_platform_id(platform),
        "reason": "no eligible main-chain profile",
    }


def profile_state_summary(path: str | None = None) -> Dict[str, Any]:
    """Return sanitized runtime state for profiles, without switching accounts."""
    path = path or profile_state_path()
    if not os.path.isfile(path):
        return {
            "schema": "knowledgeradar-profile-state-summary/v1",
            "status": "ok",
            "state_path": path,
            "profiles": [],
            "counts": {"total": 0, "healthy": 0, "cooldown": 0, "manual_action": 0, "blocked": 0},
            "notes": ["runtime profile state is optional until account-pool v2 records observations"],
        }
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as exc:
        return {
            "schema": "knowledgeradar-profile-state-summary/v1",
            "status": "degraded",
            "state_path": path,
            "error": str(exc),
            "profiles": [],
            "counts": {"total": 0, "healthy": 0, "cooldown": 0, "manual_action": 0, "blocked": 0},
        }

    rows = data.get("profiles", []) if isinstance(data, dict) else []
    now = time.time()
    summarized: List[Dict[str, Any]] = []
    counts = {"total": 0, "healthy": 0, "cooldown": 0, "manual_action": 0, "blocked": 0}
    for row in rows:
        if not isinstance(row, dict):
            continue
        cooldown_until = _float(row.get("cooldown_until"))
        cooldown_active = bool(cooldown_until and cooldown_until > now)
        manual = bool(row.get("manual_action_required", False))
        state = str(row.get("state") or "unknown")
        if cooldown_active:
            counts["cooldown"] += 1
        if manual:
            counts["manual_action"] += 1
        if state in {"blocked", "locked"}:
            counts["blocked"] += 1
        if state in {"healthy", "available"} and not cooldown_active and not manual:
            counts["healthy"] += 1
        counts["total"] += 1
        summarized.append(
            {
                "profile_id": str(row.get("profile_id") or ""),
                "platform": str(row.get("platform") or ""),
                "state": state,
                "reason_code": str(row.get("reason_code") or ""),
                "cooldown_active": cooldown_active,
                "cooldown_remaining_s": max(0, int(cooldown_until - now)) if cooldown_active else 0,
                "manual_action_required": manual,
                "safe_to_switch_account": bool(row.get("safe_to_switch_account", False)),
                "last_observed_at": row.get("last_observed_at") or "",
                "last_tool": str(row.get("last_tool") or ""),
                "notes": list(row.get("notes") or [])[:3],
                "recent_events": _summarize_recent_events(row.get("events")),
            }
        )

    return {
        "schema": "knowledgeradar-profile-state-summary/v1",
        "status": "ok",
        "state_path": path,
        "profiles": summarized,
        "counts": counts,
        "policy": {
            "auto_switch": "safe_auto",
            "manual_action_blocks_switch": False,
            "cooldown_blocks_retry": False,
        },
    }


def record_profile_state(
    profile_id: str,
    *,
    platform: str = "",
    state: str = "unknown",
    reason_code: str = "",
    cooldown_seconds: int = 0,
    manual_action_required: bool = False,
    safe_to_switch_account: bool = False,
    last_tool: str = "",
    notes: Optional[List[str]] = None,
    path: str | None = None,
) -> Dict[str, Any]:
    """Record sanitized runtime state for a profile.

    This is deliberately not an account switcher. It only stores enough state
    for health summaries and future admission gates.
    """
    profile_id = str(profile_id or "").strip()
    if not profile_id:
        return {"status": "degraded", "error": "profile_id is required"}
    path = path or profile_state_path()
    data = _load_profile_state_file(path)
    rows = data.setdefault("profiles", [])
    if not isinstance(rows, list):
        rows = []
        data["profiles"] = rows
    now = time.time()
    row = next((item for item in rows if isinstance(item, dict) and item.get("profile_id") == profile_id), None)
    if row is None:
        row = {"profile_id": profile_id}
        rows.append(row)
    row.update(
        {
            "profile_id": profile_id,
            "platform": str(platform or row.get("platform") or ""),
            "state": str(state or "unknown"),
            "reason_code": str(reason_code or ""),
            "cooldown_until": now + max(0, int(cooldown_seconds or 0)) if cooldown_seconds else 0,
            "manual_action_required": bool(manual_action_required),
            "safe_to_switch_account": bool(safe_to_switch_account),
            "last_observed_at": datetime.now(timezone.utc).isoformat(),
            "last_tool": str(last_tool or ""),
            "notes": list(notes or [])[:3],
        }
    )
    events = row.setdefault("events", [])
    if not isinstance(events, list):
        events = []
        row["events"] = events
    events.append(
        {
            "observed_at": row["last_observed_at"],
            "state": row["state"],
            "reason_code": row["reason_code"],
            "manual_action_required": row["manual_action_required"],
            "cooldown_seconds": max(0, int(cooldown_seconds or 0)),
            "safe_to_switch_account": row["safe_to_switch_account"],
            "last_tool": row["last_tool"],
        }
    )
    row["events"] = events[-50:]
    _atomic_write_json(path, data)
    return {"status": "ok", "profile_id": profile_id, "state_path": path, "state": row["state"]}


def account_pool_selection_summary(
    *,
    platform: str = "xiaohongshu",
    registry: Dict[str, Any] | None = None,
    state: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Rank available profiles without launching browsers or switching accounts."""
    registry = registry or profile_registry_internal()
    raw_policy = ((registry.get("raw") or {}).get("policy") or registry.get("policy") or {}) if isinstance(registry, dict) else {}
    auto_switch_mode = str(
        raw_policy.get("auto_switch_default")
        or raw_policy.get("automation_mode")
        or "disabled"
    )
    state = state or registry.get("runtime_state") or profile_state_summary()
    state_by_profile = {
        str(row.get("profile_id") or ""): row
        for row in state.get("profiles", [])
        if isinstance(row, dict)
    }
    candidates = []
    for row in registry.get("profiles", []) or []:
        if not isinstance(row, dict) or str(row.get("platform") or "") != platform:
            continue
        if not row.get("account_pool_member"):
            continue
        profile_id = str(row.get("profile_id") or "")
        runtime = state_by_profile.get(profile_id, {})
        eligible = _profile_is_eligible(row, runtime)
        candidates.append(
            {
                "profile_id": profile_id,
                "role": row.get("role", ""),
                "channel_id": row.get("channel_id", ""),
                "status": row.get("status", ""),
                "runtime_state": runtime.get("state", "unobserved") if runtime else "unobserved",
                "eligible": eligible,
                "blocked_reasons": _profile_blocked_reasons(row, runtime),
                "auto_switch_enabled": str(row.get("auto_switch") or "disabled") == "enabled",
                "priority": _profile_priority(row, runtime, eligible),
            }
        )
    candidates.sort(key=lambda item: item["priority"])
    recommended = next((item for item in candidates if item.get("eligible")), None)
    eligible_count = sum(1 for item in candidates if item.get("eligible"))
    return {
        "schema": "knowledgeradar-account-pool-selection/v1",
        "status": "ok",
        "platform": platform,
        "auto_switch": auto_switch_mode,
        "recommended_profile_id": recommended.get("profile_id") if recommended else "",
        "readiness": {
            "required_accounts": 3 if platform == "xiaohongshu" else 1,
            "registered_pool_members": len(candidates),
            "eligible_pool_members": eligible_count,
            "ready": eligible_count >= (3 if platform == "xiaohongshu" else 1),
        },
        "candidates": candidates,
        "notes": ["selection is policy-gated; callers may switch only for safe_auto read-only purposes"],
    }


def _resolve_profile_dir(profile_dir: str) -> str:
    if not profile_dir:
        return ""
    expanded = os.path.expandvars(os.path.expanduser(profile_dir))
    if os.path.isabs(expanded):
        return os.path.abspath(expanded)
    data_root = os.environ.get("KR_DATA_ROOT", "").strip()
    return os.path.abspath(os.path.join(data_root or REPO_ROOT, expanded))


def _account_hash(row: Dict[str, Any]) -> str:
    explicit = str(row.get("account_id_hash") or "").strip()
    if explicit:
        return explicit[:24]
    seed = str(row.get("account_slot") or row.get("profile_id") or row.get("profile_dir") or "")
    return stable_hash(seed, length=12)


def _runtime_dir() -> str:
    from runtime.paths import runtime_log_dir

    return str(runtime_log_dir())


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _load_profile_state_file(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return {"version": "1", "profiles": []}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {"version": "1", "profiles": []}
    except Exception:
        return {"version": "1", "profiles": []}


def _summarize_recent_events(events: Any) -> List[Dict[str, Any]]:
    if not isinstance(events, list):
        return []
    summarized: List[Dict[str, Any]] = []
    for event in events[-10:]:
        if not isinstance(event, dict):
            continue
        summarized.append(
            {
                "observed_at": str(event.get("observed_at") or ""),
                "state": str(event.get("state") or ""),
                "reason_code": str(event.get("reason_code") or ""),
                "manual_action_required": bool(event.get("manual_action_required", False)),
                "cooldown_seconds": int(event.get("cooldown_seconds") or 0),
                "safe_to_switch_account": bool(event.get("safe_to_switch_account", False)),
                "last_tool": str(event.get("last_tool") or ""),
            }
        )
    return summarized


def _atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    directory = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(prefix=".profile-state-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def _profile_blocked_reasons(registry_row: Dict[str, Any], runtime_row: Dict[str, Any]) -> List[str]:
    reasons: List[str] = []
    if not registry_row.get("profile_exists"):
        reasons.append("PROFILE_MISSING")
    if runtime_row.get("cooldown_active"):
        reasons.append("COOLDOWN_ACTIVE")
    if runtime_row.get("manual_action_required"):
        reasons.append("MANUAL_ACTION_REQUIRED")
    if runtime_row.get("state") in {"blocked", "locked"}:
        reasons.append("PROFILE_BLOCKED")
    if runtime_row.get("safe_to_switch_account") and str(runtime_row.get("state") or "") != "healthy":
        reasons.append("SWITCHABLE_DEGRADED")
    return reasons


def _profile_is_eligible(registry_row: Dict[str, Any], runtime_row: Dict[str, Any]) -> bool:
    return not _profile_blocked_reasons(registry_row, runtime_row)


def _profile_priority(registry_row: Dict[str, Any], runtime_row: Dict[str, Any], eligible: bool) -> int:
    base = {
        "primary": 10,
        "primary_candidate": 15,
        "isolation_probe": 30,
        "isolated_backup_candidate": 40,
        "backup_candidate": 50,
    }.get(str(registry_row.get("role") or ""), 90)
    if not eligible:
        base += 100
    if runtime_row.get("state") in {"healthy", "available"}:
        base -= 5
    return base
