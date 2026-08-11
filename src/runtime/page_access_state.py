"""Shared lightweight page access-state classifier.

This module keeps platform collectors from turning marker words such as
"captcha" or "login" into final conclusions before checking whether useful
content is already readable.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Sequence


def classify_page_access_state(
    *,
    platform: str,
    operation: str,
    blocked_marker: bool = False,
    login_marker: bool = False,
    rate_limit_marker: bool = False,
    captcha_element_count: int | None = None,
    result_item_count: int | None = None,
    card_count: int | None = None,
    link_count: int | None = None,
    content_chars: int | None = None,
    content_readable: bool | None = None,
    http_status: int | None = None,
    url_signal: str = "",
    empty_marker: bool = False,
    structured_list_expected: bool = False,
    query_reflected: bool | None = None,
    parse_failed: bool = False,
    security_evidence_strength: str | None = None,
    login_evidence_strength: str | None = None,
    blocking_modal_count: int | None = None,
    login_modal_count: int | None = None,
    extra_signals: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Classify a page from already-sanitized collector signals.

    Marker terms are only signals. A readable result page with a security/login
    prompt is returned as ok plus warning; only unreadable pages with hard
    evidence require manual interaction.
    """

    platform = str(platform or "")
    operation = str(operation or "")
    captcha_count = _safe_int(captcha_element_count, 0)
    item_count = _safe_int(result_item_count, 0)
    cards = _safe_int(card_count, 0)
    links = _safe_int(link_count, 0)
    chars = _safe_int(content_chars, 0)
    status_code = _safe_int(http_status, 0)
    blocking_modals = _safe_int(blocking_modal_count, 0)
    login_modals = _safe_int(login_modal_count, 0)
    url_signal = str(url_signal or "")
    rate_limited = bool(rate_limit_marker or status_code == 429)
    query_state = "unknown" if query_reflected is None else ("reflected" if query_reflected else "not_reflected")
    security_strength = _normalize_evidence_strength(security_evidence_strength)
    login_strength = _normalize_evidence_strength(login_evidence_strength)
    hard_security_signal = bool(captcha_count > 0 or blocking_modals > 0 or status_code == 403 or url_signal == "verification_redirect")
    hard_login_signal = bool(login_modals > 0 or status_code == 401 or url_signal == "login_redirect")
    if security_strength == "strong":
        hard_security_signal = True
    elif security_strength in {"", "unknown"}:
        hard_security_signal = bool(hard_security_signal or blocked_marker)
    if login_strength == "strong":
        hard_login_signal = True
    elif login_strength in {"", "unknown"}:
        hard_login_signal = bool(hard_login_signal or login_marker)
    hard_security = hard_security_signal
    login_required = hard_login_signal
    suspected_security = bool(blocked_marker and not hard_security)
    suspected_login = bool(login_marker and not login_required)
    readable = _is_readable(
        item_count=item_count,
        card_count=cards,
        link_count=links,
        content_chars=chars,
        content_readable=content_readable,
        operation=operation,
    )

    signals = {
        "blocked_marker": bool(blocked_marker),
        "login_marker": bool(login_marker),
        "rate_limit_marker": bool(rate_limit_marker),
        "captcha_element_count": captcha_count,
        "result_item_count": item_count,
        "card_count": cards,
        "link_count": links,
        "content_chars": chars,
        "http_status": status_code or None,
        "url_signal": url_signal,
        "empty_marker": bool(empty_marker),
        "structured_list_expected": bool(structured_list_expected),
        "query_reflected": query_state,
        "security_evidence_strength": security_strength or None,
        "login_evidence_strength": login_strength or None,
        "blocking_modal_count": blocking_modals,
        "login_modal_count": login_modals,
    }
    if extra_signals:
        signals.update(extra_signals)

    page_state = {
        "schema": "page-access-state/v1",
        "platform": platform,
        "operation": operation,
        "result_readability": "readable" if readable else "not_readable",
        "signals": signals,
        **signals,
    }

    if readable:
        if hard_security or blocked_marker:
            platform_state = "soft_security_prompt_with_results"
            warning_type = platform_state
        elif login_required or login_marker:
            platform_state = "soft_login_prompt_with_results"
            warning_type = platform_state
        else:
            platform_state = f"{operation}_ok" if operation else "ok"
            warning_type = ""
        return {
            "status": "ok",
            "failure_type": "",
            "platform_state": platform_state,
            "warning_type": warning_type,
            "manual_action_required": False,
            "manual_confidence": "none",
            "safe_to_retry": False,
            "safe_to_switch_account": False,
            "page_state": page_state,
        }

    if parse_failed:
        return {
            "status": "failed",
            "failure_type": "parse_failed",
            "platform_state": "parse_failed",
            "manual_action_required": False,
            "manual_confidence": "none",
            "safe_to_retry": True,
            "safe_to_switch_account": False,
            "page_state": {**page_state, "platform_state": "parse_failed"},
        }

    if rate_limited:
        return {
            "status": "retry_later",
            "failure_type": "rate_limited",
            "platform_state": "rate_limited",
            "manual_action_required": False,
            "manual_confidence": "none",
            "safe_to_retry": True,
            "safe_to_switch_account": False,
            "page_state": {**page_state, "platform_state": "rate_limited"},
        }

    if hard_security:
        return {
            "status": "needs_interaction",
            "failure_type": "platform_verification_required",
            "platform_state": "hard_security_block",
            "manual_action_required": True,
            "manual_confidence": "confirmed",
            "safe_to_retry": False,
            "safe_to_switch_account": True,
            "page_state": {**page_state, "platform_state": "hard_security_block"},
        }

    if login_required:
        return {
            "status": "needs_interaction",
            "failure_type": "login_required",
            "platform_state": "login_required",
            "manual_action_required": True,
            "manual_confidence": "confirmed",
            "safe_to_retry": False,
            "safe_to_switch_account": True,
            "page_state": {**page_state, "platform_state": "login_required"},
        }

    if suspected_security or suspected_login:
        return {
            "status": "ambiguous",
            "failure_type": "ambiguous_page_state",
            "platform_state": "suspected_manual_gate_not_confirmed",
            "suspected_manual_kind": "security" if suspected_security else "login",
            "manual_action_required": False,
            "manual_confidence": "suspected",
            "safe_to_retry": True,
            "safe_to_switch_account": False,
            "page_state": {
                **page_state,
                "platform_state": "suspected_manual_gate_not_confirmed",
                "suspected_manual_kind": "security" if suspected_security else "login",
            },
        }

    if operation == "search" and structured_list_expected and not empty_marker:
        state = "search_route_query_not_reflected" if query_reflected is False else "search_route_unreadable"
        return {
            "status": "failed",
            "failure_type": "tool_failure_needs_repair",
            "platform_state": state,
            "manual_action_required": False,
            "manual_confidence": "none",
            "safe_to_retry": True,
            "safe_to_switch_account": False,
            "page_state": {**page_state, "platform_state": state},
        }

    return {
        "status": "empty",
        "failure_type": "empty_results" if operation == "search" else "empty_detail",
        "platform_state": "empty_results" if operation == "search" else "empty_detail",
        "manual_action_required": False,
        "manual_confidence": "none",
        "safe_to_retry": True,
        "safe_to_switch_account": False,
        "page_state": {**page_state, "platform_state": "empty_results" if operation == "search" else "empty_detail"},
    }


def merge_page_state(data: Dict[str, Any], classified: Dict[str, Any]) -> Dict[str, Any]:
    """Merge classifier output into a collector result without losing raw data."""

    merged = dict(data)
    page_state = dict(data.get("page_state") or {})
    page_state.update(dict(classified.get("page_state") or {}))
    merged["page_state"] = page_state
    for key in (
        "status",
        "failure_type",
        "platform_state",
        "warning_type",
        "manual_action_required",
        "manual_confidence",
        "suspected_manual_kind",
        "safe_to_retry",
        "safe_to_switch_account",
    ):
        value = classified.get(key)
        if value not in (None, "") or key in {"warning_type", "failure_type"}:
            merged[key] = value
    return merged


def adapt_xhs_page_state(page_state: Dict[str, Any]) -> Dict[str, Any]:
    """Map the richer XHS classifier vocabulary to the shared state contract."""

    state = str((page_state or {}).get("platform_state") or "")
    status = "ok"
    failure_type = ""
    shared_state = "detail_ok" if state == "ok" else state
    manual = bool((page_state or {}).get("manual_action_required"))

    if manual:
        status = "needs_interaction"
        if state == "login_required":
            failure_type = "login_required"
        elif state == "app_scan_required":
            failure_type = "app_scan_required"
        else:
            failure_type = "platform_verification_required"
    elif state in {"not_found", "empty_detail"}:
        status = "empty"
        failure_type = state
    elif state == "platform_verification_required" and str((page_state or {}).get("failure_subtype") or "") == "http_429":
        status = "retry_later"
        failure_type = "rate_limited"
        shared_state = "rate_limited"
    elif state and state != "ok":
        status = "failed"
        failure_type = str((page_state or {}).get("failure_subtype") or state)

    return {
        "status": status,
        "failure_type": failure_type,
        "platform_state": shared_state,
        "manual_action_required": manual,
        "manual_confidence": "confirmed" if manual else "none",
        "safe_to_retry": bool((page_state or {}).get("safe_to_retry")),
        "safe_to_switch_account": bool((page_state or {}).get("safe_to_switch_account")),
        "page_state": {
            "schema": "page-access-state-adapter/v1",
            "source_schema": str((page_state or {}).get("schema") or "xhs-page-state/v1"),
            "platform": "xhs",
            "operation": "detail_or_search",
            "source_platform_state": state,
            "result_readability": "readable" if state == "ok" else "not_readable",
            **dict(page_state or {}),
        },
    }


def count_items(items: Iterable[Any] | None) -> int:
    if items is None:
        return 0
    if isinstance(items, Sequence) and not isinstance(items, (str, bytes, bytearray)):
        return len(items)
    return sum(1 for _ in items)


def _is_readable(
    *,
    item_count: int,
    card_count: int,
    link_count: int,
    content_chars: int,
    content_readable: bool | None,
    operation: str,
) -> bool:
    if content_readable is not None:
        return bool(content_readable)
    if operation == "detail":
        return content_chars >= 80
    return bool(item_count or card_count or link_count)


def _safe_int(value: Any, default: int) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_evidence_strength(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"strong", "confirmed", "hard"}:
        return "strong"
    if normalized in {"weak", "suspected", "soft", "text"}:
        return "weak"
    if normalized in {"none", "no", "false"}:
        return "none"
    return normalized
