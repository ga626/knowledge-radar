"""Shared platform risk, cooldown and resumable-interaction helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import os
import random
import time
from typing import Any, Dict, Mapping, Optional


MANUAL_REASON_TOKENS = {
    "auth",
    "captcha",
    "cookie",
    "login",
    "manual_action",
    "platform_verification",
    "qr",
    "scan",
    "security_verification",
    "verify",
    "verification",
    "人机",
    "扫码",
    "登录",
    "验证",
    "验证码",
    "风控",
}

RATE_LIMIT_REASON_TOKENS = {
    "429",
    "frequency",
    "frequent",
    "http_429",
    "operation_frequent",
    "rate_limit",
    "rate_limited",
    "retry_after",
    "search_frequent",
    "too_many",
    "访问频繁",
    "操作频繁",
    "频控",
    "频繁",
}

ACCOUNT_RISK_REASON_TOKENS = {
    "account_locked",
    "account_risk",
    "device_risk",
    "ip_or_device_risk",
    "ip_risk",
    "封禁",
    "账号风险",
    "设备风险",
}

NON_COOLDOWN_REASON_TOKENS = {
    "city_mismatch",
    "cdp_method_error",
    "cdp_runtime_error",
    "cdp_target_error",
    "cdp_version_error",
    "empty_results",
    "no_page_target",
    "no_results",
    "parse_failed",
    "selector_error",
    "zero_results",
}


@dataclass(frozen=True)
class PlatformRiskEvent:
    schema: str
    platform: str
    operation: str
    reason_code: str
    severity: str
    recoverability: str
    manual_action_required: bool
    retry_after_s: float
    scope_key: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def normalize_reason(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _has_any(text: str, tokens: set[str]) -> bool:
    return any(token in text for token in tokens)


def _scope_key(platform: str, operation: str, scope: Mapping[str, Any] | None) -> str:
    normalized = {
        "platform": normalize_reason(platform),
        "operation": normalize_reason(operation),
        "scope": {
            str(key): normalize_reason(str(value))
            for key, value in sorted(dict(scope or {}).items())
            if value not in (None, "")
        },
    }
    return hashlib.sha256(repr(normalized).encode("utf-8", errors="ignore")).hexdigest()[:16]


def normalize_platform_risk_event(
    *,
    platform: str,
    operation: str,
    reason_code: str,
    outcome: str = "",
    retry_after_s: float | int | None = None,
    scope: Mapping[str, Any] | None = None,
    manual_action_required: Optional[bool] = None,
) -> PlatformRiskEvent:
    """Normalize platform collector/gate observations into a shared risk event."""

    reason = normalize_reason(reason_code)
    outcome_norm = normalize_reason(outcome)
    text = f"{reason} {outcome_norm}"
    retry_after = _safe_float(retry_after_s)
    manual = bool(manual_action_required) if manual_action_required is not None else _has_any(text, MANUAL_REASON_TOKENS)

    if _has_any(text, ACCOUNT_RISK_REASON_TOKENS):
        severity = "critical"
        recoverability = "manual_or_long_cooldown"
        manual = True
    elif manual:
        severity = "high"
        recoverability = "manual_interaction"
    elif _has_any(text, RATE_LIMIT_REASON_TOKENS) or outcome_norm in {"blocked", "failed", "degraded"}:
        severity = "medium"
        recoverability = "cooldown"
    elif _has_any(text, NON_COOLDOWN_REASON_TOKENS):
        severity = "low"
        recoverability = "no_cooldown"
    else:
        severity = "low"
        recoverability = "none" if outcome_norm in {"ok", "pass", "success"} else "inspect"

    return PlatformRiskEvent(
        schema="knowledgeradar-platform-risk-event/v1",
        platform=str(platform or ""),
        operation=str(operation or ""),
        reason_code=str(reason_code or ""),
        severity=severity,
        recoverability=recoverability,
        manual_action_required=manual,
        retry_after_s=retry_after,
        scope_key=_scope_key(platform, operation, scope),
    )


def compute_platform_cooldown(
    event: PlatformRiskEvent | Mapping[str, Any],
    *,
    base_s: int,
    maximum_s: int,
    previous_cooldown_s: int = 0,
    failure_streak: int = 0,
    jitter_ratio: float | None = None,
    now: float | None = None,
) -> Dict[str, Any]:
    """Compute a conservative dynamic cooldown without sleeping.

    Manual/login/captcha events return a zero cooldown because the next action is
    user interaction, not waiting. Rate-limit events prefer server Retry-After.
    """

    data = event.to_dict() if isinstance(event, PlatformRiskEvent) else dict(event or {})
    now_ts = float(now if now is not None else time.time())
    base = max(0, int(base_s or 0))
    maximum = max(base, int(maximum_s or base))
    retry_after = _safe_float(data.get("retry_after_s"))
    manual = bool(data.get("manual_action_required"))
    recoverability = str(data.get("recoverability") or "")

    if manual or recoverability == "manual_interaction":
        seconds = 0
        source = "manual_interaction"
    elif retry_after > 0:
        seconds = min(maximum, int(round(retry_after)))
        source = "retry_after"
    elif recoverability in {"none", "no_cooldown"}:
        seconds = 0
        source = recoverability
    else:
        streak = max(0, int(failure_streak or 0))
        previous = max(0, int(previous_cooldown_s or 0))
        seed = max(base, previous * 2 if previous else base * (2**streak))
        seconds = min(maximum, seed)
        ratio = _default_jitter_ratio() if jitter_ratio is None else max(0.0, float(jitter_ratio))
        if seconds > 0 and ratio > 0:
            seconds = min(maximum, seconds + int(round(random.uniform(0, seconds * ratio))))
        source = "dynamic_backoff"

    return {
        "schema": "knowledgeradar-platform-cooldown/v1",
        "cooldown_seconds": int(seconds),
        "cooldown_until": now_ts + int(seconds) if seconds > 0 else 0,
        "next_retry_at": now_ts + int(seconds) if seconds > 0 else 0,
        "source": source,
        "event": data,
    }


def build_manual_interaction_envelope(
    *,
    platform: str,
    reason_code: str,
    original_tool: str,
    original_args: Mapping[str, Any] | None = None,
    manual_interaction: Mapping[str, Any] | None = None,
    retry_tool: str = "health_check",
    retry_mode: str = "",
    resume_policy: str = "retry_once_after_complete",
) -> Dict[str, Any]:
    """Return the shared resumable interruption envelope for host adapters."""

    args_hash = hashlib.sha256(
        repr(sorted((original_args or {}).items())).encode("utf-8", errors="ignore")
    ).hexdigest()[:16]
    mode = retry_mode or f"complete_browser_interaction:{platform}" if platform else "complete_browser_interaction"
    return {
        "schema_version": "knowledgeradar-resumable-manual-interaction/v1",
        "status": "NEEDS_INTERACTION",
        "manual_action_required": True,
        "platform": platform,
        "reason_code": reason_code,
        "manual_interaction": dict(manual_interaction or {}),
        "retry_tool": retry_tool,
        "retry_mode": mode,
        "original_tool": original_tool,
        "original_args_hash": args_hash,
        "resume_policy": resume_policy,
        "max_auto_retries": 1,
    }


def _safe_float(value: Any) -> float:
    try:
        return max(0.0, float(value or 0))
    except Exception:
        return 0.0


def _default_jitter_ratio() -> float:
    try:
        return max(0.0, float(os.environ.get("KR_PLATFORM_COOLDOWN_JITTER_RATIO", "0.2")))
    except Exception:
        return 0.2
