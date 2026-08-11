"""Shared runtime status and error taxonomy helpers."""

from __future__ import annotations

from enum import Enum
from typing import Any


ERROR_TAXONOMY = {
    "network_timeout",
    "network_error",
    "provider_unavailable",
    "rate_limited",
    "login_required",
    "anti_bot_verification",
    "empty_detail",
    "unsupported_url",
    "parse_failed",
    "task_cancelled",
    "policy_denied",
    "manual_required",
    "unknown",
}


class ValidationStatus(str, Enum):
    PASS = "PASS"
    NEEDS_INTERACTION = "NEEDS_INTERACTION"
    EXPECTED_DEGRADED = "EXPECTED_DEGRADED"
    FAIL = "FAIL"


VALIDATION_STATUS_VALUES = [item.value for item in ValidationStatus]
PASSING_STATUS_CLASSES = {ValidationStatus.PASS.value, ValidationStatus.EXPECTED_DEGRADED.value}
BLOCKING_STATUS_CLASSES = {ValidationStatus.FAIL.value, ValidationStatus.NEEDS_INTERACTION.value}


STATUS_CLASS_SCHEMA = {
    ValidationStatus.PASS.value: {
        "meaning": "Required or optional check completed successfully.",
        "blocks_overall_pass": False,
    },
    ValidationStatus.NEEDS_INTERACTION.value: {
        "meaning": "Current environment requires user login, QR scan, captcha, credential setup, or manual account action.",
        "blocks_overall_pass": True,
    },
    ValidationStatus.EXPECTED_DEGRADED.value: {
        "meaning": "Known product boundary, optional provider state, quota exhaustion, or designed fallback path.",
        "blocks_overall_pass": False,
    },
    ValidationStatus.FAIL.value: {
        "meaning": "Unexpected crash, missing required tool, broken schema, or required main-chain capability failure.",
        "blocks_overall_pass": True,
    },
}


def validation_status_classes() -> dict[str, dict[str, Any]]:
    return {name: dict(value) for name, value in STATUS_CLASS_SCHEMA.items()}


def classify_validation_status(status: str, *, required: bool = True) -> dict[str, Any]:
    value = str(status or "").strip().upper()
    if value not in STATUS_CLASS_SCHEMA:
        value = ValidationStatus.FAIL.value
    blocks = bool(STATUS_CLASS_SCHEMA[value]["blocks_overall_pass"])
    return {
        "status": value,
        "required": bool(required),
        "blocks_overall_pass": blocks,
        "meaning": STATUS_CLASS_SCHEMA[value]["meaning"],
    }


def status_blocks_overall_pass(status: str) -> bool:
    return str(status or "").strip().upper() in BLOCKING_STATUS_CLASSES


def canonical_status_counts(items: Any) -> dict[str, int]:
    counts = {status: 0 for status in VALIDATION_STATUS_VALUES}
    for item in items or []:
        if isinstance(item, dict):
            raw = item.get("status_class") or item.get("validation_status") or item.get("classification") or item.get("status")
        else:
            raw = item
        value = normalize_validation_status(raw)
        counts[value] = counts.get(value, 0) + 1
    return counts


def aggregate_validation_status(items: Any) -> str:
    counts = canonical_status_counts(items)
    if counts.get(ValidationStatus.FAIL.value):
        return ValidationStatus.FAIL.value
    if counts.get(ValidationStatus.NEEDS_INTERACTION.value):
        return ValidationStatus.NEEDS_INTERACTION.value
    if counts.get(ValidationStatus.EXPECTED_DEGRADED.value):
        return ValidationStatus.EXPECTED_DEGRADED.value
    return ValidationStatus.PASS.value


def legacy_health_status(status_class: str) -> str:
    value = normalize_validation_status(status_class)
    if value == ValidationStatus.FAIL.value:
        return "down"
    if value in {ValidationStatus.EXPECTED_DEGRADED.value, ValidationStatus.NEEDS_INTERACTION.value}:
        return "degraded"
    return "ok"


def normalize_validation_status(status: Any, *, unknown: str = ValidationStatus.FAIL.value) -> str:
    value = str(status or "").strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "OK": ValidationStatus.PASS.value,
        "READY": ValidationStatus.PASS.value,
        "HEALTHY": ValidationStatus.PASS.value,
        "SUCCESS": ValidationStatus.PASS.value,
        "AVAILABLE": ValidationStatus.PASS.value,
        "IMPLEMENTED": ValidationStatus.PASS.value,
        "DESIGN_READY": ValidationStatus.PASS.value,
        "READY_FOR_DESIGN": ValidationStatus.PASS.value,
        "READY_FOR_READONLY_OBSERVATION": ValidationStatus.PASS.value,
        "RECOMMENDATION_ONLY": ValidationStatus.PASS.value,
        "NOT_EXECUTED": ValidationStatus.PASS.value,
        "NOT_APPLICABLE": ValidationStatus.PASS.value,
        "SKIPPED": ValidationStatus.PASS.value,
        "EXPECTED_DEGRADED": ValidationStatus.EXPECTED_DEGRADED.value,
        "DEGRADED": ValidationStatus.EXPECTED_DEGRADED.value,
        "PARTIAL": ValidationStatus.EXPECTED_DEGRADED.value,
        "WARNING": ValidationStatus.EXPECTED_DEGRADED.value,
        "WARN": ValidationStatus.EXPECTED_DEGRADED.value,
        "TIMEOUT": ValidationStatus.EXPECTED_DEGRADED.value,
        "QUOTA_EXHAUSTED": ValidationStatus.EXPECTED_DEGRADED.value,
        "RATE_LIMITED": ValidationStatus.EXPECTED_DEGRADED.value,
        "NOT_CONFIGURED": ValidationStatus.EXPECTED_DEGRADED.value,
        "UNAVAILABLE": ValidationStatus.EXPECTED_DEGRADED.value,
        "NEEDS_INTERACTION": ValidationStatus.NEEDS_INTERACTION.value,
        "MANUAL": ValidationStatus.NEEDS_INTERACTION.value,
        "MANUAL_ACTION": ValidationStatus.NEEDS_INTERACTION.value,
        "LOGIN_REQUIRED": ValidationStatus.NEEDS_INTERACTION.value,
        "CAPTCHA_REQUIRED": ValidationStatus.NEEDS_INTERACTION.value,
        "AUTH_REQUIRED": ValidationStatus.NEEDS_INTERACTION.value,
        "FAIL": ValidationStatus.FAIL.value,
        "FAILED": ValidationStatus.FAIL.value,
        "ERROR": ValidationStatus.FAIL.value,
        "DOWN": ValidationStatus.FAIL.value,
        "UNHEALTHY": ValidationStatus.FAIL.value,
    }
    if value in STATUS_CLASS_SCHEMA:
        return value
    return aliases.get(value, unknown)


def classify_runtime_payload(
    payload: Any,
    *,
    required: bool = True,
    main_chain: bool | None = None,
    configured: bool | None = None,
    has_declared_reason: bool | None = None,
    optional: bool = False,
) -> dict[str, Any]:
    item = payload if isinstance(payload, dict) else {}
    explicit_status = item.get("validation_status") or item.get("status_class") or item.get("classification")
    raw_status = str(explicit_status or item.get("status") or item.get("state") or "").strip()
    configured_value = bool(item.get("configured", True) if configured is None else configured)
    main_chain_value = bool(item.get("main_chain", False) if main_chain is None else main_chain)
    item["main_chain"] = main_chain_value
    item["_required_context"] = bool(required)
    reason = _declared_reason(item)
    has_reason = bool(reason) if has_declared_reason is None else bool(has_declared_reason)
    error_type = _payload_error_type(item)
    lower = raw_status.lower().replace("-", "_").replace(" ", "_")

    if explicit_status:
        status = normalize_validation_status(explicit_status)
    elif _looks_needs_interaction(item, lower, error_type):
        status = ValidationStatus.NEEDS_INTERACTION.value
    elif lower in {"fail", "failed", "error", "down", "unhealthy"}:
        status = ValidationStatus.FAIL.value if main_chain_value else ValidationStatus.EXPECTED_DEGRADED.value
    elif normalize_validation_status(raw_status, unknown="") == ValidationStatus.PASS.value:
        status = ValidationStatus.PASS.value
    elif lower in {"timeout", "quota_exhausted", "rate_limited", "degraded", "expected_degraded", "partial", "warning", "warn", "not_configured", "unavailable"}:
        status = ValidationStatus.EXPECTED_DEGRADED.value if (has_reason or optional or not main_chain_value or not configured_value) else ValidationStatus.FAIL.value
    elif item.get("error") and main_chain_value and not has_reason:
        status = ValidationStatus.FAIL.value
    elif not configured_value or optional:
        status = ValidationStatus.EXPECTED_DEGRADED.value
    elif raw_status:
        status = ValidationStatus.FAIL.value if main_chain_value else ValidationStatus.EXPECTED_DEGRADED.value
    else:
        status = ValidationStatus.PASS.value if not item.get("error") else ValidationStatus.FAIL.value

    classified = classify_validation_status(status, required=required)
    return {
        "schema": "knowledgeradar-validation-classification/v1",
        "classification": classified["status"],
        "status_class": classified["status"],
        "validation_status": classified["status"],
        "raw_status": raw_status.lower(),
        "configured": configured_value,
        "required": bool(required),
        "main_chain": main_chain_value,
        "has_declared_reason": has_reason,
        "reason": reason,
        "error_type": error_type,
        "blocks_overall_pass": classified["blocks_overall_pass"],
    }


def classify_provider_status(name: str, payload: dict[str, Any], *, main_chain: bool = False) -> dict[str, Any]:
    item = dict(payload or {})
    reason = _declared_reason(item)
    optional = bool(
        item.get("degraded_ok")
        or item.get("optional")
        or item.get("requires_api_key")
        or item.get("requires_login")
        or item.get("auto_enabled") is False
        or not item.get("configured", True)
        or not main_chain
    )
    classification = classify_runtime_payload(
        {
            **item,
            "status": item.get("status") or ("available" if item.get("available") else "degraded"),
            "reason": reason,
        },
        required=main_chain,
        main_chain=main_chain,
        configured=bool(item.get("configured", True)),
        has_declared_reason=bool(reason) or optional,
        optional=optional,
    )
    return {
        **classification,
        "provider": name,
        "validation_reason": reason or _default_provider_reason(name, item, classification["status_class"]),
    }


def _declared_reason(item: dict[str, Any]) -> str:
    for key in (
        "validation_reason",
        "reason",
        "degraded_reason",
        "detail",
        "role",
        "strategy",
        "manual_action",
        "error_type",
        "failure_category",
    ):
        value = item.get(key)
        if value:
            return str(value)
    error = item.get("error")
    if isinstance(error, dict):
        for key in ("message", "error", "type", "reason"):
            if error.get(key):
                return str(error.get(key))
    if error:
        return str(error)
    return ""


def _payload_error_type(item: dict[str, Any]) -> str:
    for key in ("error_type", "failure_type", "failure_category", "platform_state"):
        if item.get(key):
            return str(item.get(key))
    error = item.get("error")
    if isinstance(error, dict):
        for key in ("type", "error_type", "failure_type", "platform_state"):
            if error.get(key):
                return str(error.get(key))
    if error:
        return normalize_error_code(error)
    return normalize_error_code(_declared_reason(item), fallback="")


def _looks_needs_interaction(item: dict[str, Any], raw_status: str, error_type: str) -> bool:
    if raw_status in {"needs_interaction", "manual", "manual_action"}:
        return True
    if normalize_validation_status(raw_status, unknown="") == ValidationStatus.PASS.value and item.get("manual_action_required") is not True:
        return False
    if item.get("manual_action") and (item.get("main_chain") or item.get("manual_action_required") is True):
        return True
    if item.get("manual_action_required") is True:
        return True
    if item.get("requires_login") and item.get("main_chain"):
        return True
    text = f"{raw_status} {error_type} {_declared_reason(item)}".lower()
    if any(token in text for token in ("captcha_required", "manual_required", "anti_bot_verification")):
        return True
    if "captcha" in text and any(token in text for token in ("manual", "verification", "anti_bot")):
        return True
    if any(token in text for token in ("login_required", "auth_required")):
        return bool(item.get("main_chain") or item.get("_required_context") or item.get("manual_action_required") is True)
    if raw_status in {"login_required", "auth_required"}:
        return bool(item.get("main_chain") or item.get("_required_context") or item.get("manual_action_required") is True)
    if raw_status in {"captcha_required"}:
        return True
    return False


def _default_provider_reason(name: str, item: dict[str, Any], status: str) -> str:
    if status == ValidationStatus.PASS.value:
        return "provider available"
    if not item.get("configured", True):
        return "provider not configured"
    if item.get("requires_login"):
        return "provider requires explicit user-authorized login workflow"
    if item.get("requires_api_key"):
        return "provider requires API key or entitlement"
    if item.get("daily_exhausted") or item.get("monthly_exhausted"):
        return "provider quota exhausted"
    return f"{name} is a declared optional or degraded provider"


def normalize_error_code(error: Any = "", *, fallback: str = "unknown") -> str:
    value = str(error or "").strip().lower()
    if not value:
        return fallback
    if "timeout" in value or "timed out" in value or "超时" in value:
        return "network_timeout"
    if "rate" in value and "limit" in value:
        return "rate_limited"
    if "429" in value:
        return "rate_limited"
    if "login" in value or "cookie" in value or "unauthorized" in value or "登录" in value:
        return "login_required"
    if "captcha" in value or "verify" in value or "verification" in value or "风控" in value or "验证" in value:
        return "anti_bot_verification"
    if "provider" in value or "not configured" in value or "api key" in value:
        return "provider_unavailable"
    if "empty" in value or "no content" in value or "空" in value:
        return "empty_detail"
    if "unsupported" in value or "not support" in value:
        return "unsupported_url"
    if "parse" in value or "json" in value or "解析" in value:
        return "parse_failed"
    if "cancel" in value:
        return "task_cancelled"
    if "policy" in value or "denied" in value or "blocked" in value:
        return "policy_denied"
    if "manual" in value:
        return "manual_required"
    if "connect" in value or "connection" in value or "network" in value or "ssl" in value or "网络" in value:
        return "network_error"
    return fallback
