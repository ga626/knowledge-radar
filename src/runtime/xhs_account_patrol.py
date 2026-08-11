"""Offline Xiaohongshu account-pool patrol checks.

This module deliberately avoids launching browsers or touching the platform.
It only reads sanitized registry metadata and local Chromium cookie names.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
from typing import Any, Dict, List

from .platform_diagnostics import stable_hash
from .profile_registry import REPO_ROOT, profile_registry_internal
from .xhs_account_policy import switch_policy_decision


REQUIRED_COOKIE_NAMES = {"a1", "id_token"}
OPTIONAL_COOKIE_NAMES = {"web_session", "webId", "gid", "xsecappid"}
CHROME_EPOCH_DELTA_SECONDS = 11644473600


def xhs_account_patrol_summary(registry: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Return offline patrol readiness for the Xiaohongshu account pool."""
    registry = registry or profile_registry_internal()
    raw = registry.get("raw") if isinstance(registry, dict) else {}
    policy = dict((raw or {}).get("policy") or registry.get("policy") or {})
    raw_profiles = list((raw or {}).get("profiles") or [])
    sanitized_by_id = {
        str(row.get("profile_id") or ""): row
        for row in registry.get("profiles", [])
        if isinstance(row, dict)
    }

    profiles: List[Dict[str, Any]] = []
    for row in raw_profiles:
        if not isinstance(row, dict) or row.get("platform") != "xiaohongshu":
            continue
        profile_id = str(row.get("profile_id") or "")
        resolved = _resolve_profile_dir(str(row.get("profile_dir") or ""))
        cookie_status = _cookie_status(resolved)
        sanitized = sanitized_by_id.get(profile_id, {})
        status = _profile_patrol_status(bool(sanitized.get("profile_exists")), cookie_status)
        profiles.append(
            {
                "profile_id": profile_id,
                "account_slot": str(row.get("account_slot") or ""),
                "channel_id": str(row.get("channel_id") or ""),
                "role": str(row.get("role") or ""),
                "profile_dir_hash": stable_hash(resolved),
                "profile_exists": bool(sanitized.get("profile_exists")),
                "cookie_status": cookie_status,
                "patrol_status": status,
                "main_chain_allowed": False,
            }
        )

    policy_checks = {
        "search_safe_auto": switch_policy_decision(
            purpose="search",
            mode="safe_auto",
            reason_code="COOKIE_MISSING",
            risk_score=20,
            policy=policy,
            switches_used=0,
        ),
        "detail_safe_auto": switch_policy_decision(
            purpose="detail",
            mode="safe_auto",
            reason_code="COOKIE_MISSING",
            risk_score=20,
            policy=policy,
            switches_used=0,
        ),
        "diagnostic_default": switch_policy_decision(
            purpose="diagnostic",
            mode=str(policy.get("default_mode") or "disabled"),
            reason_code="COOKIE_MISSING",
            risk_score=20,
            policy=policy,
            switches_used=0,
        ),
        "patrol_default": switch_policy_decision(
            purpose="patrol",
            mode=str(policy.get("default_mode") or "disabled"),
            reason_code="COOKIE_MISSING",
            risk_score=20,
            policy=policy,
            switches_used=0,
        ),
    }
    counts = {
        "profiles": len(profiles),
        "ok": sum(1 for row in profiles if row.get("patrol_status") == "ok"),
        "degraded": sum(1 for row in profiles if row.get("patrol_status") == "degraded"),
        "unknown": sum(1 for row in profiles if row.get("patrol_status") == "unknown"),
        "missing": sum(1 for row in profiles if row.get("patrol_status") == "missing"),
    }
    status = "ok" if counts["profiles"] and counts["degraded"] == 0 and counts["missing"] == 0 else "degraded"
    return {
        "schema": "knowledgeradar-xhs-account-patrol/v1",
        "status": status,
        "checks": {
            "profile_cookie_readiness": profiles,
            "policy_guards": policy_checks,
        },
        "counts": counts,
        "notes": [
            "offline patrol only; no browser launch and no xiaohongshu request",
            "cookie values are never returned",
            "readonly search/detail safe_auto follows registry policy",
        ],
    }


def _resolve_profile_dir(profile_dir: str) -> str:
    if not profile_dir:
        return ""
    expanded = os.path.expandvars(os.path.expanduser(profile_dir))
    if os.path.isabs(expanded):
        return os.path.abspath(expanded)
    return os.path.abspath(os.path.join(REPO_ROOT, expanded))


def _profile_patrol_status(profile_exists: bool, cookie_status: Dict[str, Any]) -> str:
    if not profile_exists:
        return "missing"
    if cookie_status.get("status") == "ok" and not cookie_status.get("missing_required"):
        return "ok"
    if cookie_status.get("status") in {"locked", "protected", "unreadable"}:
        return "unknown"
    return "degraded"


def _cookie_status(profile_dir: str) -> Dict[str, Any]:
    cookie_db = _cookie_db_path(profile_dir)
    if not cookie_db:
        return {
            "status": "missing",
            "cookie_db": "missing",
            "required_present": [],
            "missing_required": sorted(REQUIRED_COOKIE_NAMES),
            "optional_present": [],
            "missing_optional": sorted(OPTIONAL_COOKIE_NAMES),
        }
    temp_path = ""
    try:
        fd, temp_path = tempfile.mkstemp(prefix="kr_xhs_cookie_", suffix=".sqlite")
        os.close(fd)
        shutil.copy2(cookie_db, temp_path)
        rows = _read_cookie_rows(temp_path)
    except sqlite3.OperationalError as exc:
        return _cookie_error("locked" if "locked" in str(exc).lower() else "unreadable", exc)
    except PermissionError as exc:
        return _cookie_error("protected", exc)
    except Exception as exc:
        return _cookie_error("unreadable", exc)
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass

    now_chrome_us = int((time.time() + CHROME_EPOCH_DELTA_SECONDS) * 1_000_000)
    required_present = set()
    optional_present = set()
    expired_required = set()
    expired_optional = set()
    for name, expires_utc in rows:
        if name not in REQUIRED_COOKIE_NAMES and name not in OPTIONAL_COOKIE_NAMES:
            continue
        expires = int(expires_utc or 0)
        valid = expires <= 0 or expires > now_chrome_us
        if name in REQUIRED_COOKIE_NAMES:
            if valid:
                required_present.add(name)
            else:
                expired_required.add(name)
        elif name in OPTIONAL_COOKIE_NAMES:
            if valid:
                optional_present.add(name)
            else:
                expired_optional.add(name)
    missing = REQUIRED_COOKIE_NAMES - required_present
    return {
        "status": "ok" if not missing else "degraded",
        "cookie_db": "present",
        "required_present": sorted(required_present),
        "missing_required": sorted(missing),
        "expired_required": sorted(expired_required),
        "optional_present": sorted(optional_present),
        "missing_optional": sorted(OPTIONAL_COOKIE_NAMES - optional_present),
        "expired_optional": sorted(expired_optional),
    }


def _cookie_db_path(profile_dir: str) -> str:
    if not profile_dir or not os.path.isdir(profile_dir):
        return ""
    candidates = [
        os.path.join(profile_dir, "Default", "Network", "Cookies"),
        os.path.join(profile_dir, "Network", "Cookies"),
        os.path.join(profile_dir, "Default", "Cookies"),
    ]
    return next((path for path in candidates if os.path.isfile(path)), "")


def _read_cookie_rows(path: str) -> List[tuple[str, int]]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0) as conn:
        cursor = conn.execute("select name, expires_utc from cookies where host_key like '%xiaohongshu.com%'")
        return [(str(name or ""), int(expires_utc or 0)) for name, expires_utc in cursor.fetchall()]


def _cookie_error(status: str, exc: Exception) -> Dict[str, Any]:
    return {
        "status": status,
        "cookie_db": "present",
        "required_present": [],
        "missing_required": [],
        "required_state": "unknown",
        "error_type": exc.__class__.__name__,
    }
