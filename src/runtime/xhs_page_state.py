"""Shared Xiaohongshu page-state classifier.

Keep this module dependency-free so collectors, health probes, and future
browser candidates can share the same login/risk/detail-state vocabulary.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List


LOGIN_PROMPT_PATTERN = re.compile(
    r"登录后查看搜索结果|请先登录|扫码登录|手机号登录|微信登录|"
    r"获取验证码|登录后推荐|验证后可见|登录后可见|登录后浏览",
    re.I,
)

RISK_PATTERNS = {
    "security_verification": re.compile(r"安全验证|请完成验证|完成验证|拖动滑块|滑块验证", re.I),
    "frequency_limit": re.compile(r"访问频繁|操作频繁|请求频繁|搜索频繁|稍后再试", re.I),
    "environment_risk": re.compile(r"环境异常|网络环境存在风险|当前环境异常|访问环境异常", re.I),
    "account_risk": re.compile(r"账号异常|帐号异常|登录异常|存在风险|风险提示", re.I),
}

DETAIL_BLOCK_PATTERNS = {
    "app_scan_required": re.compile(
        r"APP扫码查看|扫码查看|打开小红书APP|打开小红书App|小红书App扫码|"
        r"小红书APP扫码|请使用小红书|请用小红书|当前笔记暂时无法浏览",
        re.I,
    ),
    "not_found": re.compile(r"页面不见了|你访问的页面不见了|笔记不存在|内容无法浏览|页面不可访问|页面丢失", re.I),
    "empty_detail": re.compile(r"详情为空|空详情", re.I),
}


def classify_xhs_page_state(
    text: str,
    *,
    title: str = "",
    url: str = "",
    final_url: str = "",
    initial_url: str = "",
    http_status: int | None = None,
    selector_hit_count: int | None = None,
    body_text_len: int | None = None,
    captcha_element_count: int | None = None,
    loading_state: str = "",
    account_hint: bool | None = None,
    has_login_cookie: bool | None = None,
) -> Dict[str, Any]:
    """Classify a small, already-sanitized XHS page text sample."""
    text = str(text or "")
    title = str(title or "")
    url = str(final_url or url or "")
    initial_url = str(initial_url or "")
    loading_state = str(loading_state or "")
    text_len = _int_or_none(body_text_len)
    if text_len is None:
        text_len = len(text)
    selector_hits = _int_or_none(selector_hit_count)
    captcha_count = _int_or_none(captcha_element_count)
    status_code = _int_or_none(http_status)
    combined = "\n".join(part for part in (title, url, initial_url, text) if part)
    login_markers = bool(LOGIN_PROMPT_PATTERN.search(text[:1200]))
    risk_matches = _matches(RISK_PATTERNS, combined)
    detail_matches = _matches(DETAIL_BLOCK_PATTERNS, combined)
    url_signal = _url_signal(initial_url=initial_url, final_url=url)
    http_signal = _http_signal(status_code)
    structure_signals = _structure_signals(
        text_len=text_len,
        selector_hit_count=selector_hits,
        captcha_element_count=captcha_count,
        loading_state=loading_state,
    )
    signals = {
        "keyword_risk": risk_matches,
        "detail_block": detail_matches,
        "http_status": http_signal,
        "url": url_signal,
        "structure": structure_signals,
    }
    verification_markers = bool(risk_matches or structure_signals.get("captcha") or http_signal in {"http_429", "http_403"} or url_signal == "verification_redirect")
    app_scan_required = "app_scan_required" in detail_matches
    not_found = "not_found" in detail_matches
    empty_detail = "empty_detail" in detail_matches

    if app_scan_required:
        platform_state = "app_scan_required"
    elif url_signal == "login_redirect" or http_signal == "http_401":
        platform_state = "login_required"
    elif verification_markers:
        platform_state = "platform_verification_required"
    elif not_found:
        platform_state = "not_found"
    elif http_signal == "http_404":
        platform_state = "not_found"
    elif empty_detail:
        platform_state = "empty_detail"
    elif structure_signals.get("selector_miss"):
        platform_state = "empty_detail"
    elif login_markers and not account_hint:
        platform_state = "login_required"
    else:
        platform_state = "ok"

    login_state = "authenticated" if account_hint else ("cookie_present" if has_login_cookie else "unknown")
    if login_markers and not account_hint:
        login_state = "login_required"

    manual_action_required = platform_state in {"app_scan_required", "platform_verification_required", "login_required"}
    risk_level = "high" if platform_state in {"app_scan_required", "platform_verification_required"} else ("medium" if platform_state == "login_required" else "low")
    safe_to_retry = platform_state in {"not_found", "empty_detail", "ok"} and not manual_action_required
    failure_subtype = _failure_subtype(
        platform_state=platform_state,
        signals=signals,
        selector_hit_count=selector_hits,
        text_len=text_len,
    )
    detail_quality = {
        "minimum_text_chars": 120,
        "body_text_chars": text_len,
        "selector_hit_count": selector_hits,
        "dom_ready": bool(structure_signals.get("dom_ready")),
        "status": "PASS" if platform_state == "ok" and text_len >= 120 and selector_hits != 0 else "EXPECTED_DEGRADED",
    }

    return {
        "schema": "xhs-page-state/v1",
        "login_state": login_state,
        "platform_state": platform_state,
        "failure_subtype": failure_subtype,
        "login_markers": login_markers,
        "verification_markers": verification_markers,
        "verification_match_types": risk_matches,
        "detail_block_match_types": detail_matches,
        "signals": signals,
        "http_status": status_code,
        "selector_hit_count": selector_hits,
        "text_len": text_len,
        "captcha_element_count": captcha_count,
        "loading_state": loading_state,
        "url_redirected": bool(initial_url and url and initial_url != url),
        "account_hint": bool(account_hint) if account_hint is not None else False,
        "has_login_cookie": bool(has_login_cookie) if has_login_cookie is not None else False,
        "manual_action_required": manual_action_required,
        "safe_to_retry": safe_to_retry,
        "safe_to_switch_account": platform_state in {"login_required", "platform_verification_required", "empty_detail"},
        "risk_level": risk_level,
        "detail_quality": detail_quality,
    }


def js_classifier_body_expression(max_chars: int = 2000) -> str:
    """Return a browser-side JS expression compatible with the Python classifier."""
    return f"""
(() => {{
  const text = document.body ? (document.body.innerText || '').slice(0, {int(max_chars)}) : '';
  const bodyText = document.body ? (document.body.innerText || '') : '';
  const title = document.title || '';
  const url = location.href || '';
  const combined = [title, url, text].filter(Boolean).join('\\n');
  const loginMarkers = /登录后查看搜索结果|请先登录|扫码登录|手机号登录|微信登录|获取验证码|登录后推荐|验证后可见|登录后可见|登录后浏览/i.test(text.slice(0, 1200));
  const riskPatterns = [
    ['security_verification', /安全验证|请完成验证|完成验证|拖动滑块|滑块验证/i],
    ['frequency_limit', /访问频繁|操作频繁|请求频繁|搜索频繁|稍后再试/i],
    ['environment_risk', /环境异常|网络环境存在风险|当前环境异常|访问环境异常/i],
    ['account_risk', /账号异常|帐号异常|登录异常|存在风险|风险提示/i]
  ];
  const detailPatterns = [
    ['app_scan_required', /APP扫码查看|扫码查看|打开小红书APP|打开小红书App|小红书App扫码|小红书APP扫码|请使用小红书|请用小红书|当前笔记暂时无法浏览/i],
    ['not_found', /页面不见了|你访问的页面不见了|笔记不存在|内容无法浏览|页面不可访问|页面丢失/i],
    ['empty_detail', /详情为空|空详情/i]
  ];
  const riskMatches = riskPatterns.filter(([, pattern]) => pattern.test(combined)).map(([name]) => name);
  const detailMatches = detailPatterns.filter(([, pattern]) => pattern.test(combined)).map(([name]) => name);
  const verificationMarkers = riskMatches.length > 0;
  const accountHint = /(^|\\n)我(\\n|$)/.test(text.slice(0, 300));
  const captchaSelectors = [
    '[class*="captcha"]', '[id*="captcha"]', '[class*="verify"]', '[id*="verify"]',
    '[class*="geetest"]', '[id*="geetest"]', 'iframe[src*="captcha"]', 'iframe[src*="verify"]'
  ];
  const detailSelectors = [
    '#detail-title', '#detail-desc', '[class*="note-content"]', '[class*="desc"]',
    '[class*="title"]', '[data-v] [class*="content"]'
  ];
  const countMatches = selectors => selectors.reduce((sum, selector) => {{
    try {{ return sum + document.querySelectorAll(selector).length; }} catch (_) {{ return sum; }}
  }}, 0);
  return {{
    title,
    url,
    text_sample: text.slice(0, 360),
    text_len: bodyText.length,
    selector_hit_count: countMatches(detailSelectors),
    captcha_element_count: countMatches(captchaSelectors),
    loading_state: document.readyState || '',
    login_markers: Boolean(loginMarkers),
    verification_markers: Boolean(verificationMarkers),
    verification_match_types: riskMatches,
    detail_block_match_types: detailMatches,
    account_hint: Boolean(accountHint)
  }};
}})()
"""


def _matches(patterns: Dict[str, re.Pattern[str]], text: str) -> List[str]:
    return [name for name, pattern in patterns.items() if pattern.search(text)]


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _http_signal(status_code: int | None) -> str:
    if status_code is None or status_code <= 0:
        return ""
    if status_code == 401:
        return "http_401"
    if status_code == 403:
        return "http_403"
    if status_code == 404:
        return "http_404"
    if status_code == 429:
        return "http_429"
    if status_code >= 500:
        return "http_5xx"
    return "http_ok" if 200 <= status_code < 400 else f"http_{status_code}"


def _url_signal(*, initial_url: str, final_url: str) -> str:
    value = final_url.lower()
    if any(token in value for token in ("login", "signin")):
        return "login_redirect"
    if any(token in value for token in ("captcha", "verify", "security")):
        return "verification_redirect"
    if initial_url and final_url and initial_url != final_url:
        return "redirect"
    return ""


def _structure_signals(
    *,
    text_len: int,
    selector_hit_count: int | None,
    captcha_element_count: int | None,
    loading_state: str,
) -> Dict[str, Any]:
    selector_miss = selector_hit_count == 0 and text_len < 120
    return {
        "captcha": bool(captcha_element_count and captcha_element_count > 0),
        "selector_miss": bool(selector_miss),
        "short_text": text_len < 120,
        "dom_ready": loading_state in {"interactive", "complete"},
    }


def _failure_subtype(
    *,
    platform_state: str,
    signals: Dict[str, Any],
    selector_hit_count: int | None,
    text_len: int,
) -> str:
    structure = signals.get("structure") if isinstance(signals.get("structure"), dict) else {}
    if platform_state == "login_required":
        return "login_required"
    if platform_state == "platform_verification_required":
        if structure.get("captcha"):
            return "captcha_element_detected"
        if signals.get("http_status") in {"http_429", "http_403"}:
            return str(signals.get("http_status"))
        if signals.get("url") == "verification_redirect":
            return "verification_redirect"
        return "anti_bot_verification"
    if platform_state == "not_found":
        return "not_found_or_deleted"
    if platform_state == "empty_detail":
        if selector_hit_count == 0:
            return "selector_miss"
        if text_len <= 0:
            return "page_text_empty"
        if text_len < 120:
            return "short_text"
        return "empty_detail"
    if platform_state == "app_scan_required":
        return "app_scan_required"
    return ""
