"""Shared platform health probe schema helpers.

This module is intentionally transport-agnostic: it does not call platform
collectors or browsers. Collectors use it to expose diagnostic state without
turning diagnostics into another acquisition path.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Any, Dict, Optional


SCHEMA_VERSION = "platform-health-probe/v1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_hash(value: Any, *, length: int = 12) -> str:
    text = str(value or "")
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:length]


def build_platform_health_probe(
    *,
    platform: str,
    tool: str,
    mode: str,
    status: str,
    reason_code: str = "OK",
    started_at: Optional[str] = None,
    ended_at: Optional[str] = None,
    elapsed_ms: Optional[float] = None,
    confidence: float = 0.0,
    account_id_hash: str = "",
    profile_id: str = "",
    profile_path: str = "",
    browser_base: str = "",
    risk_scope: str = "unknown",
    risk_level: str = "none",
    safe_to_retry: bool = False,
    safe_to_switch_account: bool = False,
    cooldown_seconds: int = 0,
    manual_action_required: bool = False,
    health: Optional[Dict[str, Any]] = None,
    evidence: Optional[Dict[str, Any]] = None,
    recommended_action: str = "",
) -> Dict[str, Any]:
    """Return a normalized diagnostic probe payload."""
    end = ended_at or _now_iso()
    start = started_at or end
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "reason_code": reason_code,
        "platform": platform,
        "tool": tool,
        "mode": mode,
        "started_at": start,
        "ended_at": end,
        "elapsed_ms": round(float(elapsed_ms), 2) if elapsed_ms is not None else None,
        "confidence": float(confidence),
        "scope": {
            "account_id_hash": account_id_hash,
            "profile_id": profile_id,
            "profile_path_hash": stable_hash(profile_path),
            "browser_base": browser_base,
        },
        "health": health or {},
        "risk": {
            "risk_scope": risk_scope,
            "risk_level": risk_level,
            "safe_to_retry": bool(safe_to_retry),
            "safe_to_switch_account": bool(safe_to_switch_account),
            "cooldown_seconds": int(cooldown_seconds or 0),
            "manual_action_required": bool(manual_action_required),
        },
        "evidence": evidence or {},
        "recommended_action": recommended_action,
    }


def probe_from_error(
    *,
    platform: str,
    tool: str,
    mode: str,
    error: Dict[str, Any],
    profile_id: str = "",
    browser_base: str = "",
    confidence: float = 0.7,
) -> Dict[str, Any]:
    reason = str(error.get("failure_type") or error.get("type") or "UNKNOWN").upper()
    platform_state = str(error.get("platform_state") or "")
    manual = bool(error.get("manual_action_required"))
    risk_level = "high" if manual or "verification" in platform_state else "medium"
    status = "manual_action" if manual else ("cooldown" if risk_level == "high" else "fail")
    return build_platform_health_probe(
        platform=platform,
        tool=tool,
        mode=mode,
        status=status,
        reason_code=reason,
        confidence=confidence,
        profile_id=profile_id,
        browser_base=browser_base,
        risk_scope="platform" if status == "cooldown" else "unknown",
        risk_level=risk_level,
        safe_to_retry=bool(error.get("retryable")) and status != "manual_action",
        safe_to_switch_account=False,
        manual_action_required=manual,
        evidence={
            "platform_state": platform_state,
            "failure_tags": error.get("failure_tags", []),
        },
        recommended_action=str(error.get("recommended_action") or ""),
    )
