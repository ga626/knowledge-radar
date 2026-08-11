"""Xiaohongshu route-matrix event contract and compact summary.

This module is a lightweight ledger, not a router. It records sanitized
observations for account/browser/API candidates so future route scoring can
reuse the same evidence without granting any automatic search/detail authority.
"""

from __future__ import annotations

from collections import Counter, deque
from datetime import datetime, timezone
import hashlib
import json
import os
import time
from typing import Any, Dict, Iterable, List


SRC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(SRC_ROOT)
DEFAULT_EVENT_FILENAME = "knowledgeradar-xhs-route-events.jsonl"

REQUIRED_EVENT_FIELDS = [
    "event_id",
    "observed_at",
    "actor",
    "platform",
    "account_slot",
    "profile_id",
    "browser_base",
    "channel_id",
    "capability",
    "action_type",
    "result",
    "reason_code",
    "latency_ms",
    "manual_action_required",
    "cooldown_until",
    "evidence_ref",
]

PERMISSION_LEVELS = [
    {
        "level": "L0",
        "name": "readonly_summary",
        "examples": ["health_check.summary", "capabilities.summary", "decision_log.summary"],
        "current_executor": ["codex", "openclaw"],
        "requires_user_confirm": False,
    },
    {
        "level": "L1",
        "name": "readonly_patrol",
        "examples": ["profile_exists", "login_state_readonly", "channel_state_readonly"],
        "current_executor": ["codex", "openclaw"],
        "requires_user_confirm": False,
    },
    {
        "level": "L2",
        "name": "state_record",
        "examples": ["route_event_record", "cooldown_mark", "risk_observation"],
        "current_executor": ["codex"],
        "future_executor": ["openclaw"],
        "requires_user_confirm": False,
    },
    {
        "level": "L3",
        "name": "explicit_probe",
        "examples": ["limit_1_search_canary", "single_url_detail_probe", "api_minimal_probe"],
        "current_executor": ["codex"],
        "requires_user_confirm": True,
    },
    {
        "level": "L4",
        "name": "policy_admission",
        "examples": ["main_chain_allowed", "search_safe_auto", "detail_safe_auto"],
        "current_executor": ["codex_after_user_confirm"],
        "requires_user_confirm": True,
    },
]

HUMAN_GATES = [
    "new_browser_base_first_login_scan",
    "api_key_trial_or_paid_request",
    "search_canary_authorization",
    "main_chain_admission",
    "expand_search_detail_main_chain_automation",
]

OPENCLAW_ALLOWED_NOW = [
    "health_check_summary",
    "get_capabilities_summary",
    "decision_log_summary",
    "api_documentation_research",
    "candidate_gap_report",
    "preflight_checklist_without_browser",
]

OPENCLAW_DENIED_NOW = [
    "qr_login",
    "profile_switch",
    "xiaohongshu_search",
    "search_canary",
    "profile_registry_write",
    "main_chain_admission",
    "mcp_tool_signature_change",
]

EXECUTION_ORDER = [
    "P0_contract_and_event_schema",
    "P1_browser_matrix_completion",
    "P2_api_candidate_completion",
    "P3_stability_for_admitted_candidates",
    "P4_dynamic_route_scoring",
    "P5_openclaw_low_risk_delegation",
    "P6_search_canary_and_main_chain_admission",
]

SENSITIVE_METADATA_KEYS = {
    "cookie",
    "cookies",
    "token",
    "id_token",
    "web_session",
    "phone",
    "password",
    "raw_account_id",
}


def route_event_path() -> str:
    configured = os.environ.get("KR_XHS_ROUTE_EVENT_PATH", "").strip()
    if configured:
        return configured
    return os.path.join(_runtime_dir(), DEFAULT_EVENT_FILENAME)


def record_xhs_route_event(
    *,
    actor: str,
    account_slot: str = "",
    profile_id: str = "",
    browser_base: str = "",
    channel_id: str = "",
    capability: str = "",
    action_type: str = "read_only",
    result: str = "skipped",
    reason_code: str = "",
    latency_ms: int | float = 0,
    manual_action_required: bool = False,
    cooldown_until: int | float = 0,
    evidence_ref: str = "",
    platform: str = "xiaohongshu",
    metadata: Dict[str, Any] | None = None,
    path: str | None = None,
) -> Dict[str, Any]:
    """Append one sanitized matrix event to the local JSONL ledger."""
    event = normalize_xhs_route_event(
        actor=actor,
        platform=platform,
        account_slot=account_slot,
        profile_id=profile_id,
        browser_base=browser_base,
        channel_id=channel_id,
        capability=capability,
        action_type=action_type,
        result=result,
        reason_code=reason_code,
        latency_ms=latency_ms,
        manual_action_required=manual_action_required,
        cooldown_until=cooldown_until,
        evidence_ref=evidence_ref,
        metadata=metadata,
    )
    path = path or route_event_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    return {"status": "ok", "event_id": event["event_id"], "event_path": path}


def normalize_xhs_route_event(**kwargs: Any) -> Dict[str, Any]:
    observed_at = str(kwargs.get("observed_at") or datetime.now(timezone.utc).isoformat())
    event = {
        "schema": "knowledgeradar-xhs-route-event/v1",
        "event_id": _event_id(observed_at, kwargs),
        "observed_at": observed_at,
        "actor": _clean_slug(kwargs.get("actor"), default="unknown"),
        "platform": _clean_slug(kwargs.get("platform"), default="xiaohongshu"),
        "account_slot": _clean_slug(kwargs.get("account_slot")),
        "profile_id": _clean_text(kwargs.get("profile_id"), limit=120),
        "browser_base": _clean_slug(kwargs.get("browser_base")),
        "channel_id": _clean_slug(kwargs.get("channel_id")),
        "capability": _clean_slug(kwargs.get("capability")),
        "action_type": _clean_slug(kwargs.get("action_type"), default="read_only"),
        "result": _clean_slug(kwargs.get("result"), default="skipped"),
        "reason_code": _clean_slug(kwargs.get("reason_code")),
        "latency_ms": _number(kwargs.get("latency_ms")),
        "manual_action_required": bool(kwargs.get("manual_action_required", False)),
        "cooldown_until": _number(kwargs.get("cooldown_until")),
        "evidence_ref": _clean_text(kwargs.get("evidence_ref"), limit=240),
        "metadata": _sanitize_metadata(kwargs.get("metadata") or {}),
    }
    return event


def xhs_route_event_summary(path: str | None = None, recent_limit: int = 12) -> Dict[str, Any]:
    """Return compact route-matrix contract and recent event summary."""
    path = path or route_event_path()
    recent = _read_recent_events(path, recent_limit=recent_limit)
    counts = _event_counts(recent)
    status = "ok" if os.path.isfile(path) else "not_started"
    return {
        "schema": "knowledgeradar-xhs-route-matrix-contract/v1",
        "status": status,
        "event_path": path,
        "event_schema": {
            "required_fields": REQUIRED_EVENT_FIELDS,
            "sensitive_fields_forbidden": sorted(SENSITIVE_METADATA_KEYS),
        },
        "permission_contract": {
            "levels": PERMISSION_LEVELS,
            "human_gates": HUMAN_GATES,
            "openclaw_allowed_now": OPENCLAW_ALLOWED_NOW,
            "openclaw_denied_now": OPENCLAW_DENIED_NOW,
        },
        "execution_order": EXECUTION_ORDER,
        "route_scoring": {
            "status": "not_enabled",
            "reason": "candidate matrix and API candidates must be completed before scoring",
        },
        "counts": counts,
        "recent_events": [_compact_event(event) for event in recent],
        "notes": [
            "contract only; no browser launch",
            "recording events does not admit search/detail/main_chain automation",
            "OpenClaw may use L0/L1 summaries now; L3/L4 stay Codex/user-gated",
        ],
    }


def _read_recent_events(path: str, *, recent_limit: int) -> List[Dict[str, Any]]:
    if not os.path.isfile(path):
        return []
    rows: deque[Dict[str, Any]] = deque(maxlen=max(1, int(recent_limit or 1)))
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
    except Exception:
        return []
    return list(rows)


def _event_counts(events: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    result_counts: Counter[str] = Counter()
    capability_counts: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()
    manual_count = 0
    cooldown_count = 0
    for event in events:
        result_counts[str(event.get("result") or "unknown")] += 1
        capability_counts[str(event.get("capability") or "unknown")] += 1
        action_counts[str(event.get("action_type") or "unknown")] += 1
        if event.get("manual_action_required"):
            manual_count += 1
        if _number(event.get("cooldown_until")) > time.time():
            cooldown_count += 1
    return {
        "recent_total": sum(result_counts.values()),
        "by_result": dict(result_counts),
        "by_capability": dict(capability_counts),
        "by_action_type": dict(action_counts),
        "manual_action": manual_count,
        "cooldown_active": cooldown_count,
    }


def _compact_event(event: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "event_id": str(event.get("event_id") or ""),
        "observed_at": str(event.get("observed_at") or ""),
        "actor": str(event.get("actor") or ""),
        "account_slot": str(event.get("account_slot") or ""),
        "profile_id": str(event.get("profile_id") or ""),
        "browser_base": str(event.get("browser_base") or ""),
        "channel_id": str(event.get("channel_id") or ""),
        "capability": str(event.get("capability") or ""),
        "action_type": str(event.get("action_type") or ""),
        "result": str(event.get("result") or ""),
        "reason_code": str(event.get("reason_code") or ""),
        "latency_ms": _number(event.get("latency_ms")),
        "manual_action_required": bool(event.get("manual_action_required", False)),
        "cooldown_until": _number(event.get("cooldown_until")),
        "evidence_ref": str(event.get("evidence_ref") or "")[:160],
        "metadata": _sanitize_metadata(event.get("metadata") or {}),
    }


def _runtime_dir() -> str:
    from runtime.paths import runtime_log_dir

    return str(runtime_log_dir())


def _event_id(observed_at: str, data: Dict[str, Any]) -> str:
    seed = "|".join(
        [
            observed_at,
            str(data.get("actor") or ""),
            str(data.get("account_slot") or ""),
            str(data.get("profile_id") or ""),
            str(data.get("channel_id") or ""),
            str(data.get("capability") or ""),
            str(data.get("result") or ""),
        ]
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    return f"xhs-route-{digest}"


def _clean_slug(value: Any, *, default: str = "") -> str:
    text = _clean_text(value, limit=80).lower()
    return "".join(ch if ch.isalnum() or ch in {"_", "-", "."} else "_" for ch in text) or default


def _clean_text(value: Any, *, limit: int = 120) -> str:
    text = str(value or "").strip()
    return text[: max(0, int(limit))]


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _sanitize_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    sanitized: Dict[str, Any] = {}
    for key, value in metadata.items():
        clean_key = _clean_slug(key)
        if not clean_key or clean_key in SENSITIVE_METADATA_KEYS:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            sanitized[clean_key] = _clean_text(value, limit=240) if isinstance(value, str) else value
        elif isinstance(value, list):
            sanitized[clean_key] = [_clean_text(item, limit=120) for item in value[:10]]
        else:
            sanitized[clean_key] = _clean_text(value, limit=240)
    return sanitized
