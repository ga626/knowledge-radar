"""Optional browser candidate health checks."""

from __future__ import annotations

import os
import sqlite3
import time
import threading
from typing import Any, Dict, List

from .platform_diagnostics import build_platform_health_probe
from .chrome_manager import _chrome_debug_url, _ensure_chrome_debugging
from .executables import resolve_managed_chrome
from .profile_registry import raw_registry_for_platform, select_main_chain_profile
from .xhs_account_events import classify_account_event, record_xhs_account_event
from .xhs_candidates import (
    normalize_xhs_detail_snapshot,
    normalize_xhs_search_candidates,
    visible_click_candidates,
    xhs_detail_content_snapshot_js,
    xhs_detail_text_quality,
    xhs_search_card_snapshot_js,
)
from .xhs_page_state import classify_xhs_page_state, js_classifier_body_expression
from .xhs_route_events import record_xhs_route_event
from .paths import browser_data_dir, project_root


SRC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(SRC_ROOT)
P7_PROFILE_ID = "xhs-p7-playwright"


def _playwright_cdp_profile_id_for_slot(account_slot: str) -> str:
    return {
        "xhs_account_a": "xhs-a-12733-playwright-cdp",
        "xhs_account_b": "xhs-b-12733-playwright-cdp",
        "xhs_account_c": "xhs-c-12733-playwright-cdp",
    }.get(str(account_slot or ""), "xhs-playwright-cdp-managed")


def _camoufox_sdk_profile_id_for_slot(account_slot: str) -> str:
    return {
        "xhs_account_a": "xhs-a-camoufox-sdk",
        "xhs_account_b": "xhs-b-camoufox-sdk",
        "xhs_account_c": "xhs-c-camoufox-sdk",
    }.get(str(account_slot or ""), "xhs-camoufox-sdk-managed")


def _enabled() -> bool:
    return os.environ.get("KR_CAMOUFOX_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}


def _camoufox_exe() -> str:
    configured = os.environ.get("KR_CAMOUFOX_EXE", "").strip()
    if configured:
        return configured
    candidates = [
        os.path.join(os.path.expanduser("~"), "AppData", "Local", "camoufox", "camoufox", "Cache", "camoufox.exe"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return candidates[0]


def _profile_dir() -> str:
    return os.environ.get("KR_CAMOUFOX_PROFILE_DIR", "").strip() or os.path.join(str(browser_data_dir()), "xhs_camoufox_profile_v2")


def _truthy_env(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _system_chrome_exe() -> str:
    selection = resolve_managed_chrome()
    return selection.path if selection else ""


def _chrome_exe() -> str:
    return str(_playwright_chromium_executable_summary().get("exe") or _system_chrome_exe())


def _playwright_chromium_profile_dir() -> str:
    return os.environ.get("KR_PLAYWRIGHT_CHROMIUM_XHS_PROFILE_DIR", "").strip() or os.path.join(
        str(browser_data_dir()), "profiles", "xiaohongshu", "playwright_chromium_xhs_p7_login"
    )


def _playwright_bundled_chromium_summary() -> Dict[str, Any]:
    roots = []
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if configured:
        roots.append(configured)
    roots.append(os.path.join(os.path.expanduser("~"), "AppData", "Local", "ms-playwright"))
    roots.append(os.path.join(str(project_root()), "ms-playwright"))
    seen = set()
    candidates = []
    for root in roots:
        root = os.path.abspath(os.path.expandvars(os.path.expanduser(root)))
        if root in seen or not os.path.isdir(root):
            continue
        seen.add(root)
        try:
            for name in os.listdir(root):
                if not name.startswith("chromium-"):
                    continue
                base = os.path.join(root, name)
                exe_candidates = [
                    os.path.join(base, "chrome-win64", "chrome.exe"),
                    os.path.join(base, "chrome-win", "chrome.exe"),
                    os.path.join(base, "chrome-linux", "chrome"),
                    os.path.join(base, "chrome-mac", "Chromium.app"),
                ]
                exe = next((candidate for candidate in exe_candidates if os.path.exists(candidate)), exe_candidates[0])
                candidates.append(
                    {
                        "name": name,
                        "root": root,
                        "exe": exe,
                        "exe_exists": os.path.exists(exe),
                    }
                )
        except Exception:
            continue
    installed = [item for item in candidates if item.get("exe_exists")]
    latest = sorted(installed, key=lambda item: item.get("name", ""))[-1] if installed else {}
    return {
        "status": "ok" if installed else "missing",
        "configured_path": configured,
        "candidate_count": len(candidates),
        "installed_count": len(installed),
        "latest": latest,
        "notes": [
            "non-launching check only",
            "system Chrome remains the current P7 executable until bundled Chromium is explicitly selected",
        ],
    }


def _playwright_chromium_executable_summary() -> Dict[str, Any]:
    requested = os.environ.get("KR_PLAYWRIGHT_CHROMIUM_EXECUTABLE_SOURCE", "system_chrome").strip().lower()
    if requested not in {"system_chrome", "bundled_chromium", "auto"}:
        requested = "system_chrome"
    system_exe = _system_chrome_exe()
    system_exists = os.path.isfile(system_exe)
    bundled = _playwright_bundled_chromium_summary()
    bundled_exe = str((bundled.get("latest") or {}).get("exe") or "")
    bundled_exists = bool(bundled_exe and os.path.exists(bundled_exe))
    selected_source = "system_chrome"
    selected_exe = system_exe
    selection_reason = "default_system_chrome"
    if requested == "bundled_chromium":
        if bundled_exists:
            selected_source = "bundled_chromium"
            selected_exe = bundled_exe
            selection_reason = "requested_bundled_chromium"
        else:
            selection_reason = "requested_bundled_but_missing_fallback_system"
    elif requested == "auto" and bundled_exists:
        selected_source = "bundled_chromium"
        selected_exe = bundled_exe
        selection_reason = "auto_prefers_bundled_when_available"
    return {
        "requested_source": requested,
        "selected_source": selected_source,
        "selection_reason": selection_reason,
        "exe": selected_exe,
        "exe_exists": os.path.isfile(selected_exe),
        "system_chrome": {"exe": system_exe, "exe_exists": system_exists},
        "bundled_chromium": bundled,
    }


def camoufox_v2_health() -> Dict[str, Any]:
    """Return a non-launching health summary for the validated Camoufox v2 candidate."""
    exe = _camoufox_exe()
    profile = _profile_dir()
    enabled = _enabled()
    exe_exists = os.path.isfile(exe)
    profile_exists = os.path.isdir(profile)
    if enabled and exe_exists and profile_exists:
        status = "ok"
        detail = "Camoufox v2 candidate is configured; launch/search probes are explicit, not part of health_check fast path"
    elif enabled and (exe_exists or profile_exists):
        status = "degraded"
        detail = "Camoufox v2 candidate is partially configured"
    else:
        status = "not_configured"
        detail = "Camoufox v2 candidate is not configured"
    return {
        "schema": "knowledgeradar-browser-candidate-health/v1",
        "status": status,
        "detail": detail,
        "enabled": enabled,
        "role": "xiaohongshu_backup_candidate",
        "browser_base": "camoufox_v2",
        "automation": "playwright_dom",
        "launch_policy": "manual_or_explicit_probe_only",
        "profile_dir": profile,
        "profile_exists": profile_exists,
        "exe": exe,
        "exe_exists": exe_exists,
        "risk_flags": ["isolated_profile_required", "low_frequency_only", "never_default_primary"],
    }


def playwright_chromium_isolated_health() -> Dict[str, Any]:
    """Return a non-launching health summary for the XHS Playwright Chromium candidate."""
    executable = _playwright_chromium_executable_summary()
    exe = str(executable.get("exe") or "")
    profile = _playwright_chromium_profile_dir()
    enabled = _truthy_env("KR_PLAYWRIGHT_CHROMIUM_XHS_ENABLED", "1")
    exe_exists = os.path.isfile(exe)
    profile_exists = os.path.isdir(profile)
    if enabled and exe_exists and profile_exists:
        status = "ok"
        detail = "Playwright Chromium isolated XHS profile is configured; explicit probe only"
    elif enabled and (exe_exists or profile_exists):
        status = "degraded"
        detail = "Playwright Chromium isolated XHS profile is partially configured"
    else:
        status = "not_configured"
        detail = "Playwright Chromium isolated XHS profile is not configured"
    return {
        "schema": "knowledgeradar-browser-candidate-health/v1",
        "status": status,
        "detail": detail,
        "enabled": enabled,
        "role": "xiaohongshu_isolated_backup_candidate",
        "browser_base": f"playwright_chromium_{executable.get('selected_source')}",
        "automation": "playwright_dom",
        "launch_policy": "explicit_probe_only",
        "profile_dir": profile,
        "profile_exists": profile_exists,
        "exe": exe,
        "exe_exists": exe_exists,
        "executable_source": executable.get("selected_source"),
        "executable_selection": executable,
        "bundled_chromium": executable.get("bundled_chromium"),
        "admission_notes": [
            "validated_login_persistence_2026_05_25",
            "validated_three_low_frequency_searches_2026_05_25",
            "validated_visible_card_detail_state_2026_05_25",
            "validated_detail_content_selectors_2026_05_25",
            "validated_bundled_chromium_launch_page_search_detail_2026_05_25",
            "detail_extraction_not_wired",
        ],
        "risk_flags": [
            "isolated_account_required",
            "low_frequency_only",
            "never_default_primary",
            "detail_extraction_not_wired",
            "bundled_chromium_not_selected" if executable.get("selected_source") != "bundled_chromium" else "bundled_chromium_selected_for_explicit_probe",
        ],
    }


def _chromium_cookie_session_summary(profile_dir: str) -> Dict[str, Any]:
    """Best-effort cookie persistence check for Chromium profile without decrypting cookie values."""
    cookie_path = os.path.join(profile_dir, "Default", "Network", "Cookies")
    fallback_cookie_path = os.path.join(profile_dir, "Default", "Cookies")
    if not os.path.isfile(cookie_path) and os.path.isfile(fallback_cookie_path):
        cookie_path = fallback_cookie_path
    required = {"web_session", "id_token", "a1"}
    if not os.path.isfile(cookie_path):
        return {"status": "missing", "cookie_db_exists": False, "required_present": [], "required_unexpired": []}
    try:
        con = sqlite3.connect(f"file:{cookie_path}?mode=ro", uri=True, timeout=2)
        rows = con.execute(
            "select host_key, name, expires_utc from cookies where host_key like ?",
            ("%xiaohongshu%",),
        ).fetchall()
        con.close()
    except Exception as exc:
        return {"status": "unreadable", "cookie_db_exists": True, "error": str(exc)}

    # Chromium expires_utc is microseconds since 1601-01-01 UTC.
    now_chrome = int((time.time() + 11644473600) * 1000000)
    names = {str(row[1] or "") for row in rows}
    unexpired_names = {
        str(row[1] or "")
        for row in rows
        if int(row[2] or 0) == 0 or int(row[2] or 0) > now_chrome
    }
    required_present = sorted(required & names)
    required_unexpired = sorted(required & unexpired_names)
    status = "ok" if required <= unexpired_names else ("partial" if required_present else "missing")
    return {
        "status": status,
        "cookie_db_exists": True,
        "cookie_db_path": cookie_path,
        "xhs_cookie_count": len(rows),
        "required_present": required_present,
        "required_unexpired": required_unexpired,
        "has_web_session": "web_session" in unexpired_names,
        "has_id_token": "id_token" in unexpired_names,
        "has_a1": "a1" in unexpired_names,
    }


def _xhs_cookie_session_summary(profile_dir: str) -> Dict[str, Any]:
    cookie_path = os.path.join(profile_dir, "cookies.sqlite")
    required = {"web_session", "id_token", "a1"}
    if not os.path.isfile(cookie_path):
        return {"status": "missing", "cookie_db_exists": False, "required_present": []}
    try:
        con = sqlite3.connect(f"file:{cookie_path}?mode=ro", uri=True, timeout=2)
        rows = con.execute(
            "select host, name, expiry from moz_cookies where host like ?",
            ("%xiaohongshu%",),
        ).fetchall()
        con.close()
    except Exception as exc:
        return {"status": "unreadable", "cookie_db_exists": True, "error": str(exc)}

    now = int(time.time())
    names = {str(row[1] or "") for row in rows}
    unexpired_names = {str(row[1] or "") for row in rows if int(row[2] or 0) > now}
    required_present = sorted(required & names)
    required_unexpired = sorted(required & unexpired_names)
    status = "ok" if required <= unexpired_names else ("partial" if required_present else "missing")
    return {
        "status": status,
        "cookie_db_exists": True,
        "xhs_cookie_count": len(rows),
        "required_present": required_present,
        "required_unexpired": required_unexpired,
        "has_web_session": "web_session" in unexpired_names,
        "has_id_token": "id_token" in unexpired_names,
        "has_a1": "a1" in unexpired_names,
    }


def _xhs_cookie_browser_session_ok(session: Dict[str, Any]) -> bool:
    """Return whether Firefox/Camoufox cookies are enough for browser page use."""
    return bool(session.get("has_a1") and session.get("has_web_session"))


def _xhs_cookie_full_session_ok(session: Dict[str, Any]) -> bool:
    """Return whether the stricter legacy Camoufox v2 session contract passes."""
    return bool(session.get("status") == "ok")


def _profile_from_registry(profile_id: str) -> Dict[str, Any]:
    for platform in ("xiaohongshu", "zhihu", "boss", "liepin", "maimai", "cnki"):
        for row in raw_registry_for_platform(platform).get("profiles", []) or []:
            if not isinstance(row, dict) or str(row.get("profile_id") or "") != profile_id:
                continue
            profile_dir = str(row.get("profile_dir") or "").strip()
            if profile_dir and not os.path.isabs(profile_dir):
                profile_dir = os.path.join(str(project_root()), profile_dir)
            return {**row, "profile_dir": os.path.abspath(os.path.expandvars(os.path.expanduser(profile_dir)))}
    return {}


def probe_camoufox_v2_login(timeout_ms: int = 15000) -> Dict[str, Any]:
    """Explicit, read-only login probe for the Xiaohongshu Camoufox v2 candidate."""
    try:
        import asyncio

        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        return _probe_camoufox_v2_login_in_thread(timeout_ms=timeout_ms)
    return _probe_camoufox_v2_login_sync(timeout_ms=timeout_ms)


def probe_camoufox_v2_search_page(keyword: str = "书桌布置", timeout_ms: int = 25000) -> Dict[str, Any]:
    """Explicit low-frequency Camoufox search-page canary. Reads candidates, no click."""
    try:
        import asyncio

        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        return _probe_camoufox_v2_search_page_in_thread(keyword=keyword, timeout_ms=timeout_ms)
    return _probe_camoufox_v2_search_page_sync(keyword=keyword, timeout_ms=timeout_ms)


def probe_camoufox_sdk_xhs_search_page(keyword: str = "书桌布置", timeout_ms: int = 25000) -> Dict[str, Any]:
    """Explicit low-frequency Camoufox SDK account-bound canary. Reads candidates, no click."""
    try:
        import asyncio

        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        return _probe_camoufox_sdk_xhs_search_page_in_thread(keyword=keyword, timeout_ms=timeout_ms)
    return _probe_camoufox_sdk_xhs_search_page_sync(keyword=keyword, timeout_ms=timeout_ms)


def probe_playwright_chromium_xhs_login(timeout_ms: int = 30000) -> Dict[str, Any]:
    """Explicit, read-only login probe for the XHS Playwright Chromium isolated profile."""
    try:
        import asyncio

        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        return _probe_playwright_chromium_xhs_login_in_thread(timeout_ms=timeout_ms)
    return _probe_playwright_chromium_xhs_login_sync(timeout_ms=timeout_ms)


def probe_playwright_chromium_xhs_detail(keyword: str = "书桌布置", timeout_ms: int = 45000) -> Dict[str, Any]:
    """Explicit low-frequency P7 detail probe. Never called from summary health."""
    try:
        import asyncio

        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        return _probe_playwright_chromium_xhs_detail_in_thread(keyword=keyword, timeout_ms=timeout_ms)
    return _probe_playwright_chromium_xhs_detail_sync(keyword=keyword, timeout_ms=timeout_ms)


def probe_playwright_chromium_launch_only(timeout_ms: int = 20000) -> Dict[str, Any]:
    """Explicit launch-only probe for P7 executable/profile without visiting XHS."""
    try:
        import asyncio

        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        return _probe_playwright_chromium_launch_only_in_thread(timeout_ms=timeout_ms)
    return _probe_playwright_chromium_launch_only_sync(timeout_ms=timeout_ms)


def probe_playwright_chromium_xhs_page_load(timeout_ms: int = 30000) -> Dict[str, Any]:
    """Explicit page-load-only probe for P7. Opens XHS explore, no search/click."""
    try:
        import asyncio

        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        return _probe_playwright_chromium_xhs_page_load_in_thread(timeout_ms=timeout_ms)
    return _probe_playwright_chromium_xhs_page_load_sync(timeout_ms=timeout_ms)


def probe_playwright_chromium_xhs_search_page(keyword: str = "书桌布置", timeout_ms: int = 35000) -> Dict[str, Any]:
    """Explicit search-page-only probe for P7. Reads candidates, no click."""
    try:
        import asyncio

        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        return _probe_playwright_chromium_xhs_search_page_in_thread(keyword=keyword, timeout_ms=timeout_ms)
    return _probe_playwright_chromium_xhs_search_page_sync(keyword=keyword, timeout_ms=timeout_ms)


def probe_playwright_cdp_xhs_search_page(keyword: str = "书桌布置", timeout_ms: int = 35000) -> Dict[str, Any]:
    """Low-frequency XHS search-page canary through the managed Chrome CDP channel."""
    try:
        import asyncio

        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        return _probe_playwright_cdp_xhs_search_page_in_thread(keyword=keyword, timeout_ms=timeout_ms)
    return _probe_playwright_cdp_xhs_search_page_sync(keyword=keyword, timeout_ms=timeout_ms)


def _probe_playwright_chromium_xhs_login_in_thread(timeout_ms: int = 30000) -> Dict[str, Any]:
    box: Dict[str, Any] = {}

    def _target() -> None:
        box["result"] = _probe_playwright_chromium_xhs_login_sync(timeout_ms=timeout_ms)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(max(10.0, timeout_ms / 1000 + 10.0))
    if "result" in box:
        return box["result"]
    health = playwright_chromium_isolated_health()
    return {
        **health,
        "probe": "playwright_chromium_xhs_login",
        "status": "degraded",
        "detail": "Playwright Chromium XHS explicit probe timed out in worker thread",
        "admission": "blocked",
        "elapsed_s": round(max(10.0, timeout_ms / 1000 + 10.0), 2),
    }


def _probe_playwright_chromium_xhs_login_sync(timeout_ms: int = 30000) -> Dict[str, Any]:
    started = time.time()
    health = playwright_chromium_isolated_health()
    browser_base = str(health.get("browser_base") or "playwright_chromium")
    session = _chromium_cookie_session_summary(str(health.get("profile_dir") or ""))
    if health.get("status") != "ok":
        profile_path = str(health.get("profile_dir") or "")
        return {
            **health,
            "probe": "playwright_chromium_xhs_login",
            "status": "degraded",
            "detail": "Playwright Chromium isolated candidate is not ready for explicit probe",
            "session_persistence": session,
            "platform_health_probe": build_platform_health_probe(
                platform="xiaohongshu",
                tool="playwright_chromium_xhs_probe",
                mode="read_only",
                status="fail",
                reason_code="BROWSER_START_FAILED",
                profile_id="xhs_playwright_chromium_isolated",
                profile_path=profile_path,
                browser_base=browser_base,
                risk_level="medium",
                health={"browser": health, "auth": {"session_persistence": session}},
                recommended_action="检查 Chrome exe/profile 配置；通过前不要进入搜索。",
            ),
            "elapsed_s": round(time.time() - started, 2),
        }

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return {
            **health,
            "probe": "playwright_chromium_xhs_login",
            "status": "degraded",
            "detail": f"Playwright unavailable: {exc}",
            "session_persistence": session,
            "platform_health_probe": build_platform_health_probe(
                platform="xiaohongshu",
                tool="playwright_chromium_xhs_probe",
                mode="read_only",
                status="fail",
                reason_code="DEPENDENCY_UNAVAILABLE",
                profile_id="xhs_playwright_chromium_isolated",
                profile_path=str(health.get("profile_dir") or ""),
                browser_base=browser_base,
                risk_level="medium",
                health={"browser": health, "auth": {"session_persistence": session}},
                evidence={"error": str(exc)},
                recommended_action="安装/修复 Playwright 后再做 Chromium 隔离诊断。",
            ),
            "elapsed_s": round(time.time() - started, 2),
        }

    context = None
    page = None
    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                health["profile_dir"],
                executable_path=health["exe"],
                headless=False,
                timeout=timeout_ms,
                viewport={"width": 1280, "height": 900},
                args=["--no-first-run", "--no-default-browser-check"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://www.xiaohongshu.com/explore", wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(3000)
            state = page.evaluate(js_classifier_body_expression(2000))
            cookies = context.cookies("https://www.xiaohongshu.com")
            cookie_names = sorted({str(cookie.get("name") or "") for cookie in cookies})
            has_login_cookie = {"a1", "web_session"} <= set(cookie_names) and bool(
                {"id_token", "websectiga", "xsecappid"} & set(cookie_names)
            )
            classified = classify_xhs_page_state(
                str(state.get("text_sample") or ""),
                title=str(state.get("title") or ""),
                url=str(state.get("url") or ""),
                account_hint=bool(state.get("account_hint")),
                has_login_cookie=has_login_cookie,
            )
            login_markers = bool(classified.get("login_markers"))
            verification = bool(classified.get("verification_markers"))
            account_hint = bool(classified.get("account_hint"))
            platform_ok = str(classified.get("platform_state") or "") == "ok"
            login_authenticated = str(classified.get("login_state") or "") == "authenticated"
            ok = has_login_cookie and platform_ok and login_authenticated and not verification
            status = "ok" if ok else "degraded"
            if ok:
                detail = "Playwright Chromium isolated XHS login probe passed"
            elif has_login_cookie and not verification:
                detail = "Playwright Chromium has persisted cookies but page state is inconclusive"
            elif verification:
                detail = "Playwright Chromium page shows verification/risk markers"
            else:
                detail = "Playwright Chromium page shows login or missing session markers"
            probe_status = "pass" if ok else ("pass" if has_login_cookie and not verification and not login_markers else "fail")
            reason_code = "OK" if probe_status == "pass" else ("RISK_CONTROL" if verification else "LOGIN_REQUIRED")
            return {
                **health,
                "probe": "playwright_chromium_xhs_login",
                "status": status,
                "detail": detail,
                "admission": "isolated_backup_candidate" if ok else ("session_persistence_ok_dom_degraded" if probe_status == "pass" else "blocked"),
                "session_persistence": session,
                "login_state": {
                    "has_login_cookie": has_login_cookie,
                    "account_hint": account_hint,
                    "login_markers": login_markers,
                    "verification_markers": verification,
                    "verification_match_types": classified.get("verification_match_types", []),
                    "page_state": classified.get("platform_state"),
                    "title": state.get("title", ""),
                    "url": state.get("url", ""),
                    "cookie_names": cookie_names,
                },
                "platform_health_probe": build_platform_health_probe(
                    platform="xiaohongshu",
                    tool="playwright_chromium_xhs_probe",
                    mode="read_only",
                    status=probe_status,
                    reason_code=reason_code,
                    elapsed_ms=(time.time() - started) * 1000,
                    profile_id="xhs_playwright_chromium_isolated",
                    profile_path=str(health.get("profile_dir") or ""),
                    browser_base=browser_base,
                    risk_scope="platform" if reason_code == "RISK_CONTROL" else ("account" if reason_code == "LOGIN_REQUIRED" else "unknown"),
                    risk_level="low" if probe_status == "pass" else "medium",
                    safe_to_retry=probe_status == "fail" and reason_code != "RISK_CONTROL",
                    safe_to_switch_account=False,
                    manual_action_required=reason_code == "LOGIN_REQUIRED",
                    health={
                        "browser": health,
                        "auth": {"session_persistence": session},
                        "page": {
                            "login_markers": login_markers,
                            "verification_markers": verification,
                            "verification_match_types": classified.get("verification_match_types", []),
                            "page_state": classified,
                            "account_hint": account_hint,
                        },
                    },
                    evidence={
                        "title_redacted": state.get("title", ""),
                        "final_url_redacted": state.get("url", ""),
                        "verification_match_types": classified.get("verification_match_types", []),
                    },
                    recommended_action="保持为隔离备用候选；只做低频搜索/诊断，详情路径另行验收。",
                ),
                "elapsed_s": round(time.time() - started, 2),
            }
    except Exception as exc:
        probe_status = "pass" if session.get("status") == "ok" else "fail"
        return {
            **health,
            "probe": "playwright_chromium_xhs_login",
            "status": "degraded" if session.get("status") != "ok" else "ok",
            "detail": f"Playwright Chromium explicit probe failed: {exc}",
            "admission": "session_persistence_ok_dom_probe_failed" if session.get("status") == "ok" else "blocked",
            "session_persistence": session,
            "platform_health_probe": build_platform_health_probe(
                platform="xiaohongshu",
                tool="playwright_chromium_xhs_probe",
                mode="read_only",
                status=probe_status,
                reason_code="OK" if probe_status == "pass" else "BROWSER_START_FAILED",
                elapsed_ms=(time.time() - started) * 1000,
                profile_id="xhs_playwright_chromium_isolated",
                profile_path=str(health.get("profile_dir") or ""),
                browser_base=browser_base,
                risk_level="low" if probe_status == "pass" else "medium",
                safe_to_retry=probe_status == "fail",
                health={"browser": health, "auth": {"session_persistence": session}},
                evidence={"probe_error": str(exc)[:240]},
                recommended_action="session cookie 可用时仅代表持久化通过；页面诊断失败需重试或人工查看。",
            ),
            "elapsed_s": round(time.time() - started, 2),
        }
    finally:
        try:
            if page:
                page.close()
        except Exception:
            pass
        try:
            if context:
                context.close()
        except Exception:
            pass


def _probe_playwright_chromium_launch_only_in_thread(timeout_ms: int = 20000) -> Dict[str, Any]:
    box: Dict[str, Any] = {}

    def _target() -> None:
        box["result"] = _probe_playwright_chromium_launch_only_sync(timeout_ms=timeout_ms)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(max(10.0, timeout_ms / 1000 + 5.0))
    if "result" in box:
        return box["result"]
    health = playwright_chromium_isolated_health()
    return {
        **health,
        "probe": "playwright_chromium_launch_only",
        "status": "degraded",
        "detail": "Playwright Chromium launch-only probe timed out in worker thread",
        "admission": "blocked",
        "elapsed_s": round(max(10.0, timeout_ms / 1000 + 5.0), 2),
    }


def _probe_playwright_chromium_launch_only_sync(timeout_ms: int = 20000) -> Dict[str, Any]:
    started = time.time()
    health = playwright_chromium_isolated_health()
    session = _chromium_cookie_session_summary(str(health.get("profile_dir") or ""))
    if health.get("status") != "ok":
        return {
            **health,
            "probe": "playwright_chromium_launch_only",
            "status": "degraded",
            "detail": "Playwright Chromium isolated candidate is not ready for launch-only probe",
            "admission": "blocked",
            "session_persistence": session,
            "elapsed_s": round(time.time() - started, 2),
        }
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return {
            **health,
            "probe": "playwright_chromium_launch_only",
            "status": "degraded",
            "detail": f"Playwright unavailable: {exc}",
            "admission": "blocked",
            "session_persistence": session,
            "elapsed_s": round(time.time() - started, 2),
        }

    context = None
    page = None
    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                health["profile_dir"],
                executable_path=health["exe"],
                headless=False,
                timeout=timeout_ms,
                viewport={"width": 1280, "height": 900},
                args=["--no-first-run", "--no-default-browser-check"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("about:blank", wait_until="domcontentloaded", timeout=timeout_ms)
            title = page.title()
            return {
                **health,
                "probe": "playwright_chromium_launch_only",
                "status": "ok",
                "detail": "Playwright Chromium launch-only probe passed",
                "admission": "launch_only_ok",
                "session_persistence": session,
                "page": {"url": page.url, "title": title},
                "elapsed_s": round(time.time() - started, 2),
            }
    except Exception as exc:
        return {
            **health,
            "probe": "playwright_chromium_launch_only",
            "status": "degraded",
            "detail": f"Playwright Chromium launch-only probe failed: {exc}",
            "admission": "blocked",
            "session_persistence": session,
            "elapsed_s": round(time.time() - started, 2),
        }
    finally:
        try:
            if page:
                page.close()
        except Exception:
            pass
        try:
            if context:
                context.close()
        except Exception:
            pass


def _probe_playwright_chromium_xhs_page_load_in_thread(timeout_ms: int = 30000) -> Dict[str, Any]:
    box: Dict[str, Any] = {}

    def _target() -> None:
        box["result"] = _probe_playwright_chromium_xhs_page_load_sync(timeout_ms=timeout_ms)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(max(10.0, timeout_ms / 1000 + 8.0))
    if "result" in box:
        return box["result"]
    health = playwright_chromium_isolated_health()
    return {
        **health,
        "probe": "playwright_chromium_xhs_page_load",
        "status": "degraded",
        "detail": "Playwright Chromium XHS page-load probe timed out in worker thread",
        "admission": "blocked",
        "elapsed_s": round(max(10.0, timeout_ms / 1000 + 8.0), 2),
    }


def _probe_playwright_chromium_xhs_page_load_sync(timeout_ms: int = 30000) -> Dict[str, Any]:
    started = time.time()
    health = playwright_chromium_isolated_health()
    session = _chromium_cookie_session_summary(str(health.get("profile_dir") or ""))
    if health.get("status") != "ok":
        return {
            **health,
            "probe": "playwright_chromium_xhs_page_load",
            "status": "degraded",
            "detail": "Playwright Chromium isolated candidate is not ready for page-load probe",
            "admission": "blocked",
            "session_persistence": session,
            "elapsed_s": round(time.time() - started, 2),
        }
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return {
            **health,
            "probe": "playwright_chromium_xhs_page_load",
            "status": "degraded",
            "detail": f"Playwright unavailable: {exc}",
            "admission": "blocked",
            "session_persistence": session,
            "elapsed_s": round(time.time() - started, 2),
        }

    context = None
    page = None
    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                health["profile_dir"],
                executable_path=health["exe"],
                headless=False,
                timeout=timeout_ms,
                viewport={"width": 1280, "height": 900},
                args=["--no-first-run", "--no-default-browser-check"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://www.xiaohongshu.com/explore", wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(2500)
            state_raw = page.evaluate(js_classifier_body_expression(2000))
            cookies = context.cookies("https://www.xiaohongshu.com")
            cookie_names = sorted({str(cookie.get("name") or "") for cookie in cookies})
            has_login_cookie = {"a1", "web_session"} <= set(cookie_names) and bool(
                {"id_token", "websectiga", "xsecappid"} & set(cookie_names)
            )
            classified = classify_xhs_page_state(
                str(state_raw.get("text_sample") or ""),
                title=str(state_raw.get("title") or ""),
                url=str(state_raw.get("url") or ""),
                account_hint=bool(state_raw.get("account_hint")),
                has_login_cookie=has_login_cookie,
            )
            ok = classified.get("platform_state") == "ok" and has_login_cookie and not classified.get("verification_markers")
            profile_state = _record_xhs_p7_profile_state(
                probe="playwright_chromium_xhs_page_load",
                status="ok" if ok else "degraded",
                reason_code="OK" if ok else str(classified.get("platform_state") or "PAGE_LOAD_DEGRADED"),
                manual_action_required=bool(classified.get("manual_action_required")),
                notes=[str(health.get("executable_source") or "")],
            )
            return {
                **health,
                "probe": "playwright_chromium_xhs_page_load",
                "status": "ok" if ok else "degraded",
                "detail": "Playwright Chromium XHS explore page loaded" if ok else "Playwright Chromium XHS explore page loaded with degraded state",
                "admission": "page_load_ok" if ok else "blocked",
                "session_persistence": session,
                "profile_state_record": profile_state,
                "page_state": classified,
                "page": {
                    "url": state_raw.get("url", ""),
                    "title": state_raw.get("title", ""),
                    "cookie_names": cookie_names,
                    "has_login_cookie": has_login_cookie,
                },
                "elapsed_s": round(time.time() - started, 2),
            }
    except Exception as exc:
        profile_state = _record_xhs_p7_profile_state(
            probe="playwright_chromium_xhs_page_load",
            status="degraded",
            reason_code="PROBE_FAILED",
            notes=[str(health.get("executable_source") or ""), str(exc)[:80]],
        )
        return {
            **health,
            "probe": "playwright_chromium_xhs_page_load",
            "status": "degraded",
            "detail": f"Playwright Chromium XHS page-load probe failed: {exc}",
            "admission": "blocked",
            "profile_state_record": profile_state,
            "session_persistence": session,
            "elapsed_s": round(time.time() - started, 2),
        }
    finally:
        try:
            if page:
                page.close()
        except Exception:
            pass
        try:
            if context:
                context.close()
        except Exception:
            pass


def _probe_playwright_chromium_xhs_search_page_in_thread(keyword: str, timeout_ms: int = 35000) -> Dict[str, Any]:
    box: Dict[str, Any] = {}

    def _target() -> None:
        box["result"] = _probe_playwright_chromium_xhs_search_page_sync(keyword=keyword, timeout_ms=timeout_ms)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(max(12.0, timeout_ms / 1000 + 8.0))
    if "result" in box:
        return box["result"]
    health = playwright_chromium_isolated_health()
    return {
        **health,
        "probe": "playwright_chromium_xhs_search_page",
        "status": "degraded",
        "detail": "Playwright Chromium XHS search-page probe timed out in worker thread",
        "admission": "blocked",
        "elapsed_s": round(max(12.0, timeout_ms / 1000 + 8.0), 2),
    }


def _probe_playwright_cdp_xhs_search_page_in_thread(keyword: str, timeout_ms: int = 35000) -> Dict[str, Any]:
    box: Dict[str, Any] = {}

    def _target() -> None:
        box["result"] = _probe_playwright_cdp_xhs_search_page_sync(keyword=keyword, timeout_ms=timeout_ms)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(max(12.0, timeout_ms / 1000 + 8.0))
    if "result" in box:
        return box["result"]
    return {
        "schema": "knowledgeradar-browser-candidate-health/v1",
        "status": "degraded",
        "probe": "playwright_cdp_xhs_search_page",
        "detail": "Managed Chrome CDP XHS search-page canary timed out in worker thread",
        "admission": "blocked",
        "elapsed_s": round(max(12.0, timeout_ms / 1000 + 8.0), 2),
    }


def _probe_playwright_cdp_xhs_search_page_sync(keyword: str, timeout_ms: int = 35000) -> Dict[str, Any]:
    started = time.time()
    keyword = str(keyword or "书桌布置").strip()[:40] or "书桌布置"
    selected_profile = select_main_chain_profile("xiaohongshu")
    account_slot = str(selected_profile.get("account_slot") or "")
    selected_profile_id = str(selected_profile.get("profile_id") or "")
    resource_key = f"xhs:{selected_profile_id}" if selected_profile_id else "xhs"
    profile_id = _playwright_cdp_profile_id_for_slot(account_slot)
    channel_id = "chrome_12733_playwright_cdp_attach"
    browser_base = "chrome_12733"
    if not _ensure_chrome_debugging(resource_key):
        route_event = _record_xhs_search_canary_event(
            actor="codex",
            profile_id=profile_id,
            account_slot=account_slot,
            browser_base=browser_base,
            channel_id=channel_id,
            status="degraded",
            reason_code="CDP_UNAVAILABLE",
            started=started,
            keyword=keyword,
        )
        return {
            "schema": "knowledgeradar-browser-candidate-health/v1",
            "status": "degraded",
            "probe": "playwright_cdp_xhs_search_page",
            "detail": "Managed Chrome CDP channel is unavailable",
            "admission": "blocked",
            "route_event_record": route_event,
            "elapsed_s": round(time.time() - started, 2),
        }
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        route_event = _record_xhs_search_canary_event(
            actor="codex",
            profile_id=profile_id,
            account_slot=account_slot,
            browser_base=browser_base,
            channel_id=channel_id,
            status="degraded",
            reason_code="DEPENDENCY_UNAVAILABLE",
            started=started,
            keyword=keyword,
        )
        return {
            "schema": "knowledgeradar-browser-candidate-health/v1",
            "status": "degraded",
            "probe": "playwright_cdp_xhs_search_page",
            "detail": f"Playwright unavailable: {exc}",
            "admission": "blocked",
            "route_event_record": route_event,
            "elapsed_s": round(time.time() - started, 2),
        }

    browser = None
    page = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(_chrome_debug_url(resource_key), timeout=timeout_ms)
            context = browser.contexts[0] if browser.contexts else browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.pages[0] if context.pages else context.new_page()
            navigation_started = time.time()
            page.goto(
                f"https://www.xiaohongshu.com/search_result?keyword={_url_encode(keyword)}&source=web_search_result_notes",
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            page.wait_for_timeout(4500)
            state_raw = page.evaluate(js_classifier_body_expression(2400))
            search_state = classify_xhs_page_state(
                str(state_raw.get("text_sample") or ""),
                title=str(state_raw.get("title") or ""),
                url=str(state_raw.get("url") or ""),
                account_hint=bool(state_raw.get("account_hint")),
                has_login_cookie=True,
            )
            snapshot = page.evaluate(xhs_search_card_snapshot_js(max_links=80, max_cards=20))
            cards = snapshot.get("cards", []) if isinstance(snapshot, dict) else []
            normalized = normalize_xhs_search_candidates(cards, source="playwright_cdp_snapshot")
            visible = visible_click_candidates(cards)
            ok = (
                search_state.get("platform_state") == "ok"
                and len(cards) > 0
                and len(visible) > 0
                and len(normalized) > 0
            )
            route_event = _record_xhs_search_canary_event(
                actor="codex",
                profile_id=profile_id,
                account_slot=account_slot,
                browser_base=browser_base,
                channel_id=channel_id,
                status="ok" if ok else "degraded",
                reason_code="OK" if ok else str(search_state.get("platform_state") or "SEARCH_PAGE_DEGRADED"),
                started=started,
                latency_started=navigation_started,
                manual_action_required=bool(search_state.get("manual_action_required")),
                keyword=keyword,
                candidate_count=len(cards),
                visible_count=len(visible),
                normalized_count=len(normalized),
            )
            return {
                "schema": "knowledgeradar-browser-candidate-health/v1",
                "status": "ok" if ok else "degraded",
                "probe": "playwright_cdp_xhs_search_page",
                "detail": "Managed Chrome CDP XHS search page candidates loaded" if ok else "Managed Chrome CDP XHS search page candidates weak or blocked",
                "admission": "search_page_ok" if ok else "blocked",
                "route_event_record": route_event,
                "search_state": search_state,
                "evidence": {
                    "keyword": keyword,
                    "account_slot": account_slot,
                    "selected_profile_id": selected_profile_id,
                    "candidate_profile_id": profile_id,
                    "candidate_count": len(cards),
                    "visible_count": len(visible),
                    "normalized_count": len(normalized),
                    "url": snapshot.get("url", "") if isinstance(snapshot, dict) else "",
                    "title": snapshot.get("title", "") if isinstance(snapshot, dict) else "",
                },
                "elapsed_s": round(time.time() - started, 2),
            }
    except Exception as exc:
        route_event = _record_xhs_search_canary_event(
            actor="codex",
            profile_id=profile_id,
            account_slot=account_slot,
            browser_base=browser_base,
            channel_id=channel_id,
            status="degraded",
            reason_code="PROBE_FAILED",
            started=started,
            keyword=keyword,
        )
        return {
            "schema": "knowledgeradar-browser-candidate-health/v1",
            "status": "degraded",
            "probe": "playwright_cdp_xhs_search_page",
            "detail": f"Managed Chrome CDP XHS search-page canary failed: {exc}",
            "admission": "blocked",
            "route_event_record": route_event,
            "elapsed_s": round(time.time() - started, 2),
        }
    finally:
        try:
            if page:
                page.close()
        except Exception:
            pass
        try:
            if browser:
                browser.close()
        except Exception:
            pass


def _probe_playwright_chromium_xhs_search_page_sync(keyword: str, timeout_ms: int = 35000) -> Dict[str, Any]:
    started = time.time()
    health = playwright_chromium_isolated_health()
    session = _chromium_cookie_session_summary(str(health.get("profile_dir") or ""))
    if health.get("status") != "ok":
        return {
            **health,
            "probe": "playwright_chromium_xhs_search_page",
            "status": "degraded",
            "detail": "Playwright Chromium isolated candidate is not ready for search-page probe",
            "admission": "blocked",
            "session_persistence": session,
            "elapsed_s": round(time.time() - started, 2),
        }
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return {
            **health,
            "probe": "playwright_chromium_xhs_search_page",
            "status": "degraded",
            "detail": f"Playwright unavailable: {exc}",
            "admission": "blocked",
            "session_persistence": session,
            "elapsed_s": round(time.time() - started, 2),
        }

    context = None
    page = None
    keyword = str(keyword or "书桌布置").strip()[:40] or "书桌布置"
    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                health["profile_dir"],
                executable_path=health["exe"],
                headless=False,
                timeout=timeout_ms,
                viewport={"width": 1280, "height": 900},
                args=["--no-first-run", "--no-default-browser-check"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(
                f"https://www.xiaohongshu.com/search_result?keyword={_url_encode(keyword)}&source=web_search_result_notes",
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            page.wait_for_timeout(3500)
            state_raw = page.evaluate(js_classifier_body_expression(2000))
            search_state = classify_xhs_page_state(
                str(state_raw.get("text_sample") or ""),
                title=str(state_raw.get("title") or ""),
                url=str(state_raw.get("url") or ""),
                account_hint=bool(state_raw.get("account_hint")),
                has_login_cookie=session.get("status") == "ok",
            )
            snapshot = page.evaluate(xhs_search_card_snapshot_js(max_links=80, max_cards=20))
            cards = snapshot.get("cards", []) if isinstance(snapshot, dict) else []
            normalized = normalize_xhs_search_candidates(cards, source="playwright_p7_snapshot")
            visible = visible_click_candidates(cards)
            ok = (
                search_state.get("platform_state") == "ok"
                and len(cards) > 0
                and len(visible) > 0
                and len(normalized) > 0
            )
            profile_state = _record_xhs_p7_profile_state(
                probe="playwright_chromium_xhs_search_page",
                status="ok" if ok else "degraded",
                reason_code="OK" if ok else str(search_state.get("platform_state") or "SEARCH_PAGE_DEGRADED"),
                manual_action_required=bool(search_state.get("manual_action_required")),
                notes=[str(health.get("executable_source") or ""), f"cards={len(cards)}"],
            )
            route_event = _record_xhs_search_canary_event(
                actor="codex",
                profile_id=P7_PROFILE_ID,
                browser_base="playwright_chromium",
                channel_id="playwright_chromium_isolated_probe",
                status="ok" if ok else "degraded",
                reason_code="OK" if ok else str(search_state.get("platform_state") or "SEARCH_PAGE_DEGRADED"),
                started=started,
                manual_action_required=bool(search_state.get("manual_action_required")),
                keyword=keyword,
                candidate_count=len(cards),
                visible_count=len(visible),
                normalized_count=len(normalized),
            )
            return {
                **health,
                "probe": "playwright_chromium_xhs_search_page",
                "status": "ok" if ok else "degraded",
                "detail": "Playwright Chromium XHS search page candidates loaded" if ok else "Playwright Chromium XHS search page candidates weak or blocked",
                "admission": "search_page_ok" if ok else "blocked",
                "profile_state_record": profile_state,
                "route_event_record": route_event,
                "session_persistence": session,
                "search_state": search_state,
                "evidence": {
                    "keyword": keyword,
                    "candidate_count": len(cards),
                    "visible_count": len(visible),
                    "normalized_count": len(normalized),
                    "url": snapshot.get("url", "") if isinstance(snapshot, dict) else "",
                    "title": snapshot.get("title", "") if isinstance(snapshot, dict) else "",
                },
                "elapsed_s": round(time.time() - started, 2),
            }
    except Exception as exc:
        profile_state = _record_xhs_p7_profile_state(
            probe="playwright_chromium_xhs_search_page",
            status="degraded",
            reason_code="PROBE_FAILED",
            notes=[str(health.get("executable_source") or ""), str(exc)[:80]],
        )
        route_event = _record_xhs_search_canary_event(
            actor="codex",
            profile_id=P7_PROFILE_ID,
            browser_base="playwright_chromium",
            channel_id="playwright_chromium_isolated_probe",
            status="degraded",
            reason_code="PROBE_FAILED",
            started=started,
            keyword=keyword,
        )
        return {
            **health,
            "probe": "playwright_chromium_xhs_search_page",
            "status": "degraded",
            "detail": f"Playwright Chromium XHS search-page probe failed: {exc}",
            "admission": "blocked",
            "profile_state_record": profile_state,
            "route_event_record": route_event,
            "session_persistence": session,
            "elapsed_s": round(time.time() - started, 2),
        }
    finally:
        try:
            if page:
                page.close()
        except Exception:
            pass
        try:
            if context:
                context.close()
        except Exception:
            pass


def _probe_playwright_chromium_xhs_detail_in_thread(keyword: str, timeout_ms: int = 45000) -> Dict[str, Any]:
    box: Dict[str, Any] = {}

    def _target() -> None:
        box["result"] = _probe_playwright_chromium_xhs_detail_sync(keyword=keyword, timeout_ms=timeout_ms)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(max(15.0, timeout_ms / 1000 + 10.0))
    if "result" in box:
        return box["result"]
    health = playwright_chromium_isolated_health()
    return {
        **health,
        "probe": "playwright_chromium_xhs_detail",
        "status": "degraded",
        "detail": "Playwright Chromium XHS detail probe timed out in worker thread",
        "admission": "blocked",
        "elapsed_s": round(max(15.0, timeout_ms / 1000 + 10.0), 2),
    }


def _probe_playwright_chromium_xhs_detail_sync(keyword: str, timeout_ms: int = 45000) -> Dict[str, Any]:
    started = time.time()
    health = playwright_chromium_isolated_health()
    session = _chromium_cookie_session_summary(str(health.get("profile_dir") or ""))
    if health.get("status") != "ok":
        return _p7_detail_probe_result(
            health=health,
            session=session,
            started=started,
            status="degraded",
            detail="Playwright Chromium isolated candidate is not ready for detail probe",
            reason_code="BROWSER_START_FAILED",
        )

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return _p7_detail_probe_result(
            health=health,
            session=session,
            started=started,
            status="degraded",
            detail=f"Playwright unavailable: {exc}",
            reason_code="DEPENDENCY_UNAVAILABLE",
            evidence={"error": str(exc)[:240]},
        )

    context = None
    page = None
    keyword = str(keyword or "书桌布置").strip()[:40] or "书桌布置"
    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                health["profile_dir"],
                executable_path=health["exe"],
                headless=False,
                timeout=timeout_ms,
                viewport={"width": 1280, "height": 900},
                args=["--no-first-run", "--no-default-browser-check"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(
                f"https://www.xiaohongshu.com/search_result?keyword={_url_encode(keyword)}&source=web_search_result_notes",
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            page.wait_for_timeout(3500)
            search_state_raw = page.evaluate(js_classifier_body_expression(2000))
            search_state = classify_xhs_page_state(
                str(search_state_raw.get("text_sample") or ""),
                title=str(search_state_raw.get("title") or ""),
                url=str(search_state_raw.get("url") or ""),
                account_hint=bool(search_state_raw.get("account_hint")),
                has_login_cookie=session.get("status") == "ok",
            )
            if search_state.get("manual_action_required"):
                return _p7_detail_probe_result(
                    health=health,
                    session=session,
                    started=started,
                    status="degraded",
                    detail="Search page requires manual action; detail probe stopped",
                    reason_code=str(search_state.get("platform_state") or "MANUAL_ACTION_REQUIRED"),
                    search_state=search_state,
                )

            snapshot = page.evaluate(xhs_search_card_snapshot_js(max_links=80, max_cards=20))
            cards = snapshot.get("cards", []) if isinstance(snapshot, dict) else []
            normalized = normalize_xhs_search_candidates(cards, source="playwright_p7_snapshot")
            visible = visible_click_candidates(cards)
            if not visible:
                return _p7_detail_probe_result(
                    health=health,
                    session=session,
                    started=started,
                    status="degraded",
                    detail="No visible XHS card candidates found",
                    reason_code="NO_CLICKABLE_CANDIDATES",
                    search_state=search_state,
                    evidence={"candidate_count": len(cards), "normalized_count": len(normalized)},
                )

            target = visible[0]
            point = target.get("click_point") or {}
            page.mouse.click(float(point.get("x") or 0), float(point.get("y") or 0))
            page.wait_for_timeout(4500)
            detail_state_raw = page.evaluate(js_classifier_body_expression(2400))
            detail_state = classify_xhs_page_state(
                str(detail_state_raw.get("text_sample") or ""),
                title=str(detail_state_raw.get("title") or ""),
                url=str(detail_state_raw.get("url") or ""),
                account_hint=bool(detail_state_raw.get("account_hint")),
                has_login_cookie=session.get("status") == "ok",
            )
            detail_snapshot = page.evaluate(xhs_detail_content_snapshot_js(max_chars=2400))
            detail_normalized = normalize_xhs_detail_snapshot(detail_snapshot)
            text_quality = xhs_detail_text_quality(detail_normalized)
            ok = (
                detail_normalized.get("status") == "ok"
                and text_quality.get("status") == "ok"
                and detail_state.get("platform_state") == "ok"
            )
            return _p7_detail_probe_result(
                health=health,
                session=session,
                started=started,
                status="ok" if ok else "degraded",
                detail="P7 detail selectors normalized" if ok else "P7 detail selectors were weak or blocked",
                reason_code="OK" if ok else str(detail_state.get("platform_state") or detail_normalized.get("status") or "DETAIL_WEAK"),
                search_state=search_state,
                detail_state=detail_state,
                evidence={
                    "keyword": keyword,
                    "candidate_count": len(cards),
                    "visible_count": len(visible),
                    "normalized_count": len(normalized),
                    "clicked_note_id": target.get("noteId", ""),
                    "click_point": point,
                    "detail": {
                        "status": detail_normalized.get("status"),
                        "selector_keys": detail_normalized.get("selector_keys", []),
                        "content_signals": detail_normalized.get("content_signals", []),
                        "title_present": bool(detail_normalized.get("title")),
                        "body_present": bool(detail_normalized.get("body")),
                        "text_len": detail_normalized.get("text_len"),
                        "url": detail_normalized.get("url"),
                        "text_quality": text_quality,
                    },
                },
            )
    except Exception as exc:
        return _p7_detail_probe_result(
            health=health,
            session=session,
            started=started,
            status="degraded",
            detail=f"Playwright Chromium detail probe failed: {exc}",
            reason_code="PROBE_FAILED",
            evidence={"probe_error": str(exc)[:240]},
        )
    finally:
        try:
            if page:
                page.close()
        except Exception:
            pass
        try:
            if context:
                context.close()
        except Exception:
            pass


def _p7_detail_probe_result(
    *,
    health: Dict[str, Any],
    session: Dict[str, Any],
    started: float,
    status: str,
    detail: str,
    reason_code: str,
    search_state: Dict[str, Any] | None = None,
    detail_state: Dict[str, Any] | None = None,
    evidence: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    browser_base = str(health.get("browser_base") or "playwright_chromium")
    manual = bool((search_state or {}).get("manual_action_required") or (detail_state or {}).get("manual_action_required"))
    profile_state = _record_xhs_p7_profile_state(
        probe="playwright_chromium_xhs_detail",
        status=status,
        reason_code=reason_code,
        manual_action_required=manual,
        notes=[str(health.get("executable_source") or ""), detail[:80]],
    )
    return {
        **health,
        "probe": "playwright_chromium_xhs_detail",
        "status": status,
        "detail": detail,
        "admission": "isolated_detail_probe_ok" if status == "ok" else "blocked",
        "profile_state_record": profile_state,
        "session_persistence": session,
        "search_state": search_state or {},
        "detail_state": detail_state or {},
        "platform_health_probe": build_platform_health_probe(
            platform="xiaohongshu",
            tool="playwright_chromium_xhs_detail_probe",
            mode="explicit_low_frequency_detail_probe",
            status="pass" if status == "ok" else "fail",
            reason_code=reason_code,
            elapsed_ms=(time.time() - started) * 1000,
            profile_id="xhs_playwright_chromium_isolated",
            profile_path=str(health.get("profile_dir") or ""),
            browser_base=browser_base,
            risk_level="low" if status == "ok" else "medium",
            safe_to_retry=False,
            safe_to_switch_account=False,
            manual_action_required=manual,
            health={"browser": health, "auth": {"session_persistence": session}},
            evidence=evidence or {},
            recommended_action="保持为隔离详情验收探针；通过前不要接入主采集链路。",
        ),
        "elapsed_s": round(time.time() - started, 2),
    }


def _url_encode(value: str) -> str:
    try:
        from urllib.parse import quote

        return quote(value)
    except Exception:
        return value


def _record_xhs_p7_profile_state(
    *,
    probe: str,
    status: str,
    reason_code: str = "",
    manual_action_required: bool = False,
    notes: list[str] | None = None,
) -> Dict[str, Any]:
    code = _normalize_xhs_probe_reason(reason_code, status=status, manual_action_required=manual_action_required)
    event = classify_account_event(code)
    result = record_xhs_account_event(P7_PROFILE_ID, code, last_tool=probe, notes=notes or [])
    result["event_classification"] = event
    return result


def _normalize_xhs_probe_reason(reason_code: str, *, status: str, manual_action_required: bool) -> str:
    code = str(reason_code or "").strip().upper()
    if status == "ok" and not manual_action_required:
        return "OK"
    if code in {"", "PROBE_DEGRADED", "PROBE_FAILED"}:
        return "LOGIN_REQUIRED" if manual_action_required else "PROFILE_START_FAILED"
    if code in {"PLATFORM_VERIFICATION_REQUIRED", "RISK_CONTROL", "MANUAL_ACTION_REQUIRED"}:
        return "SECURITY_VERIFICATION" if manual_action_required else "ACCOUNT_RISK"
    if code in {"RATE_LIMITED", "SEARCH_COOLDOWN_ACTIVE"}:
        return "HTTP_429"
    return code


def _record_xhs_search_canary_event(
    *,
    actor: str,
    profile_id: str,
    account_slot: str = "",
    browser_base: str,
    channel_id: str,
    status: str,
    reason_code: str,
    started: float,
    latency_started: float | None = None,
    manual_action_required: bool = False,
    keyword: str = "",
    candidate_count: int = 0,
    visible_count: int = 0,
    normalized_count: int = 0,
    network_statuses: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    result = "ok" if status == "ok" else "degraded"
    parse_failed = (
        status != "ok"
        and normalized_count <= 0
        and str(reason_code or "").upper()
        not in {
            "LOGIN_REQUIRED",
            "COOKIE_INCOMPLETE",
            "BROWSER_START_FAILED",
            "DEPENDENCY_UNAVAILABLE",
            "CDP_UNAVAILABLE",
            "PROBE_FAILED",
            "PLATFORM_VERIFICATION_REQUIRED",
            "APP_SCAN_REQUIRED",
            "ANTI_BOT_BLOCKED",
        }
    )
    safe_network_statuses = []
    for item in network_statuses or []:
        if not isinstance(item, dict):
            continue
        safe_network_statuses.append(
            {
                "endpoint": str(item.get("endpoint") or "")[:80],
                "status": int(item.get("status") or 0),
                "ok": bool(item.get("ok")),
            }
        )
    metadata = {
        "limit": 1,
        "canary_limit": 1,
        "total_elapsed_ms": round((time.time() - started) * 1000, 3),
        "keyword_len": len(str(keyword or "")),
        "candidate_count": candidate_count,
        "visible_count": visible_count,
        "normalized_count": normalized_count,
        "parse_failed": parse_failed,
    }
    if safe_network_statuses:
        metadata["network_status_summary"] = ";".join(
            f"{item['endpoint']}={item['status']}" for item in safe_network_statuses[:8]
        )
        metadata["network_block_statuses"] = ",".join(
            sorted({str(item["status"]) for item in safe_network_statuses if not item["ok"]})
        )
    try:
        return record_xhs_route_event(
            actor=actor,
            account_slot=account_slot,
            profile_id=profile_id,
            browser_base=browser_base,
            channel_id=channel_id,
            capability="search_canary_limit_1",
            action_type="limit_1_canary",
            result=result,
            reason_code=reason_code,
            latency_ms=(time.time() - (latency_started or started)) * 1000,
            manual_action_required=manual_action_required,
            metadata=metadata,
        )
    except Exception as exc:
        return {"status": "degraded", "detail": f"route event record failed: {exc}"}


def _probe_camoufox_v2_login_in_thread(timeout_ms: int = 15000) -> Dict[str, Any]:
    box: Dict[str, Any] = {}

    def _target() -> None:
        box["result"] = _probe_camoufox_v2_login_sync(timeout_ms=timeout_ms)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(max(5.0, timeout_ms / 1000 + 5.0))
    if "result" in box:
        return box["result"]
    health = camoufox_v2_health()
    return {
        **health,
        "probe": "camoufox_v2_login",
        "status": "degraded",
        "detail": "Camoufox v2 explicit probe timed out in worker thread",
        "admission": "blocked",
        "elapsed_s": round(max(5.0, timeout_ms / 1000 + 5.0), 2),
    }


def _probe_camoufox_v2_search_page_in_thread(keyword: str, timeout_ms: int = 25000) -> Dict[str, Any]:
    box: Dict[str, Any] = {}

    def _target() -> None:
        box["result"] = _probe_camoufox_v2_search_page_sync(keyword=keyword, timeout_ms=timeout_ms)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(max(8.0, timeout_ms / 1000 + 6.0))
    if "result" in box:
        return box["result"]
    health = camoufox_v2_health()
    return {
        **health,
        "probe": "camoufox_v2_search_page",
        "status": "degraded",
        "detail": "Camoufox v2 search-page canary timed out in worker thread",
        "admission": "blocked",
        "elapsed_s": round(max(8.0, timeout_ms / 1000 + 6.0), 2),
    }


def _probe_camoufox_sdk_xhs_search_page_in_thread(keyword: str, timeout_ms: int = 25000) -> Dict[str, Any]:
    box: Dict[str, Any] = {}

    def _target() -> None:
        box["result"] = _probe_camoufox_sdk_xhs_search_page_sync(keyword=keyword, timeout_ms=timeout_ms)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(max(8.0, timeout_ms / 1000 + 6.0))
    if "result" in box:
        return box["result"]
    return {
        "schema": "knowledgeradar-browser-candidate-health/v1",
        "probe": "camoufox_sdk_xhs_search_page",
        "status": "degraded",
        "detail": "Camoufox SDK search-page canary timed out in worker thread",
        "admission": "blocked",
        "elapsed_s": round(max(8.0, timeout_ms / 1000 + 6.0), 2),
    }


def _camoufox_search_page_canary(
    *,
    health: Dict[str, Any],
    profile_dir: str,
    profile_id: str,
    account_slot: str,
    browser_base: str,
    channel_id: str,
    keyword: str,
    timeout_ms: int,
    session_contract: str,
    source: str,
) -> Dict[str, Any]:
    started = time.time()
    session = _xhs_cookie_session_summary(profile_dir)
    keyword = str(keyword or "书桌布置").strip()[:40] or "书桌布置"
    session_ok = _xhs_cookie_full_session_ok(session) if session_contract == "full" else _xhs_cookie_browser_session_ok(session)
    if health.get("status") != "ok":
        route_event = _record_xhs_search_canary_event(
            actor="codex",
            account_slot=account_slot,
            profile_id=profile_id,
            browser_base=browser_base,
            channel_id=channel_id,
            status="degraded",
            reason_code="BROWSER_START_FAILED",
            started=started,
            keyword=keyword,
        )
        return {
            **health,
            "probe": f"{browser_base}_search_page",
            "status": "degraded",
            "detail": "Camoufox candidate is not ready for search-page canary",
            "admission": "blocked",
            "session_persistence": session,
            "route_event_record": route_event,
            "elapsed_s": round(time.time() - started, 2),
        }
    if not session_ok:
        route_event = _record_xhs_search_canary_event(
            actor="codex",
            account_slot=account_slot,
            profile_id=profile_id,
            browser_base=browser_base,
            channel_id=channel_id,
            status="degraded",
            reason_code="COOKIE_INCOMPLETE",
            started=started,
            manual_action_required=True,
            keyword=keyword,
        )
        return {
            **health,
            "probe": f"{browser_base}_search_page",
            "status": "degraded",
            "detail": "Camoufox XHS session cookies are incomplete; search canary blocked before page parse",
            "admission": "blocked",
            "session_persistence": session,
            "route_event_record": route_event,
            "elapsed_s": round(time.time() - started, 2),
        }

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        route_event = _record_xhs_search_canary_event(
            actor="codex",
            account_slot=account_slot,
            profile_id=profile_id,
            browser_base=browser_base,
            channel_id=channel_id,
            status="degraded",
            reason_code="DEPENDENCY_UNAVAILABLE",
            started=started,
            keyword=keyword,
        )
        return {
            **health,
            "probe": f"{browser_base}_search_page",
            "status": "degraded",
            "detail": f"Playwright unavailable: {exc}",
            "admission": "blocked",
            "session_persistence": session,
            "route_event_record": route_event,
            "elapsed_s": round(time.time() - started, 2),
        }

    context = None
    page = None
    network_statuses: List[Dict[str, Any]] = []
    try:
        with sync_playwright() as p:
            context = p.firefox.launch_persistent_context(
                profile_dir,
                executable_path=health["exe"],
                headless=True,
                timeout=timeout_ms,
                viewport={"width": 1280, "height": 900},
            )
            page = context.pages[0] if context.pages else context.new_page()

            def _record_search_response(response: Any) -> None:
                try:
                    url = str(response.url)
                    if "/api/sns/web/v1/search/" not in url:
                        return
                    endpoint = url.split("/api/sns/web/v1/search/", 1)[1].split("?", 1)[0]
                    network_statuses.append(
                        {
                            "endpoint": f"search/{endpoint}",
                            "status": int(response.status),
                            "ok": bool(response.ok),
                        }
                    )
                except Exception:
                    return

            page.on("response", _record_search_response)
            navigation_started = time.time()
            page.goto(
                f"https://www.xiaohongshu.com/search_result?keyword={_url_encode(keyword)}&source=web_search_result_notes",
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            state_raw = page.evaluate(js_classifier_body_expression(2200))
            search_state = classify_xhs_page_state(
                str(state_raw.get("text_sample") or ""),
                title=str(state_raw.get("title") or ""),
                url=str(state_raw.get("url") or ""),
                account_hint=bool(state_raw.get("account_hint")),
                has_login_cookie=session_ok,
            )
            snapshot = _wait_for_xhs_search_snapshot(page, max_wait_ms=4500)
            cards = snapshot.get("cards", []) if isinstance(snapshot, dict) else []
            normalized = normalize_xhs_search_candidates(cards, source=source)
            visible = visible_click_candidates(cards)
            ok = (
                search_state.get("platform_state") == "ok"
                and len(cards) > 0
                and len(visible) > 0
                and len(normalized) > 0
            )
            route_event = _record_xhs_search_canary_event(
                actor="codex",
                account_slot=account_slot,
                profile_id=profile_id,
                browser_base=browser_base,
                channel_id=channel_id,
                status="ok" if ok else "degraded",
                reason_code="OK"
                if ok
                else _xhs_search_page_degraded_reason(
                    search_state,
                    cards,
                    visible,
                    normalized,
                    network_statuses=network_statuses,
                ),
                started=started,
                latency_started=navigation_started,
                manual_action_required=bool(search_state.get("manual_action_required")),
                keyword=keyword,
                candidate_count=len(cards),
                visible_count=len(visible),
                normalized_count=len(normalized),
                network_statuses=network_statuses,
            )
            return {
                **health,
                "probe": f"{browser_base}_search_page",
                "status": "ok" if ok else "degraded",
                "detail": "Camoufox XHS search page candidates loaded" if ok else "Camoufox XHS search page candidates weak or blocked",
                "admission": "search_page_ok" if ok else "blocked",
                "session_persistence": session,
                "route_event_record": route_event,
                "search_state": search_state,
                "evidence": {
                    "keyword": keyword,
                    "account_slot": account_slot,
                    "candidate_profile_id": profile_id,
                    "candidate_count": len(cards),
                    "visible_count": len(visible),
                    "normalized_count": len(normalized),
                    "url": snapshot.get("url", "") if isinstance(snapshot, dict) else "",
                    "title": snapshot.get("title", "") if isinstance(snapshot, dict) else "",
                    "network_statuses": network_statuses[:8],
                },
                "elapsed_s": round(time.time() - started, 2),
            }
    except Exception as exc:
        route_event = _record_xhs_search_canary_event(
            actor="codex",
            account_slot=account_slot,
            profile_id=profile_id,
            browser_base=browser_base,
            channel_id=channel_id,
            status="degraded",
            reason_code="PROBE_FAILED",
            started=started,
            keyword=keyword,
        )
        return {
            **health,
            "probe": f"{browser_base}_search_page",
            "status": "degraded",
            "detail": f"Camoufox search-page canary failed: {exc}",
            "admission": "blocked",
            "session_persistence": session,
            "route_event_record": route_event,
            "elapsed_s": round(time.time() - started, 2),
        }
    finally:
        try:
            if page:
                page.close()
        except Exception:
            pass
        try:
            if context:
                context.close()
        except Exception:
            pass


def _wait_for_xhs_search_snapshot(page: Any, max_wait_ms: int = 4500) -> Dict[str, Any]:
    deadline = time.time() + max(0.5, max_wait_ms / 1000.0)
    snapshot: Dict[str, Any] = {}
    while True:
        try:
            current = page.evaluate(xhs_search_card_snapshot_js(max_links=80, max_cards=20))
            if isinstance(current, dict):
                snapshot = current
                if int(current.get("rawCount") or 0) > 0 or len(current.get("cards") or []) > 0:
                    return snapshot
        except Exception:
            pass
        if time.time() >= deadline:
            return snapshot
        page.wait_for_timeout(350)


def _xhs_search_page_degraded_reason(
    search_state: Dict[str, Any],
    cards: List[Dict[str, Any]],
    visible: List[Dict[str, Any]],
    normalized: List[Dict[str, Any]],
    network_statuses: List[Dict[str, Any]] | None = None,
) -> str:
    for item in network_statuses or []:
        endpoint = str(item.get("endpoint") or "")
        status = int(item.get("status") or 0)
        if endpoint in {"search/notes", "search/onebox"} and status in {403, 429, 461, 471}:
            return "ANTI_BOT_BLOCKED"
    platform_state = str(search_state.get("platform_state") or "").upper()
    if platform_state and platform_state != "OK":
        return platform_state
    if not cards:
        return "EMPTY_CANDIDATES"
    if not visible:
        return "NO_VISIBLE_CANDIDATES"
    if not normalized:
        return "NORMALIZE_FAILED"
    return "SEARCH_PAGE_DEGRADED"


def _probe_camoufox_sdk_xhs_search_page_sync(keyword: str, timeout_ms: int = 25000) -> Dict[str, Any]:
    selected_profile = select_main_chain_profile("xiaohongshu")
    account_slot = str(selected_profile.get("account_slot") or "")
    profile_id = _camoufox_sdk_profile_id_for_slot(account_slot)
    raw_profile = _profile_from_registry(profile_id)
    health = {
        "schema": "knowledgeradar-browser-candidate-health/v1",
        "status": "ok" if raw_profile and os.path.isdir(str(raw_profile.get("profile_dir") or "")) and os.path.isfile(_camoufox_exe()) else "degraded",
        "detail": "Camoufox SDK account-bound candidate is configured",
        "enabled": _enabled(),
        "role": "xiaohongshu_backup_candidate",
        "browser_base": "camoufox_sdk",
        "automation": "playwright_dom",
        "launch_policy": str(raw_profile.get("launch_policy") or "explicit_probe_only"),
        "profile_id": profile_id,
        "account_slot": account_slot,
        "profile_dir": str(raw_profile.get("profile_dir") or ""),
        "profile_exists": bool(raw_profile and os.path.isdir(str(raw_profile.get("profile_dir") or ""))),
        "exe": _camoufox_exe(),
        "exe_exists": os.path.isfile(_camoufox_exe()),
    }
    return _camoufox_search_page_canary(
        health=health,
        profile_dir=str(raw_profile.get("profile_dir") or ""),
        profile_id=profile_id,
        account_slot=account_slot,
        browser_base="camoufox_sdk",
        channel_id="camoufox_sdk_persistent_context",
        keyword=keyword,
        timeout_ms=timeout_ms,
        session_contract="browser",
        source="camoufox_sdk_snapshot",
    )


def _probe_camoufox_v2_search_page_sync(keyword: str, timeout_ms: int = 25000) -> Dict[str, Any]:
    health = camoufox_v2_health()
    return _camoufox_search_page_canary(
        health=health,
        profile_dir=str(health.get("profile_dir") or ""),
        profile_id="xhs-camoufox-v2",
        account_slot="",
        browser_base="camoufox_v2",
        channel_id="camoufox_v2_dom_probe",
        keyword=keyword,
        timeout_ms=timeout_ms,
        session_contract="full",
        source="camoufox_v2_snapshot",
    )


def _probe_camoufox_v2_login_sync(timeout_ms: int = 15000) -> Dict[str, Any]:
    """Synchronous implementation; wrapped in a worker when called from asyncio."""
    started = time.time()
    health = camoufox_v2_health()
    session = _xhs_cookie_session_summary(str(health.get("profile_dir") or ""))
    if health.get("status") != "ok":
        profile_path = str(health.get("profile_dir") or "")
        return {
            **health,
            "probe": "camoufox_v2_login",
            "status": "degraded",
            "detail": "Camoufox v2 candidate is not ready for explicit probe",
            "session_persistence": session,
            "platform_health_probe": build_platform_health_probe(
                platform="xiaohongshu",
                tool="camoufox_v2_probe",
                mode="read_only",
                status="fail",
                reason_code="BROWSER_START_FAILED",
                profile_id="xhs_camoufox_v2",
                profile_path=profile_path,
                browser_base="camoufox_v2",
                risk_level="medium",
                health={"browser": health, "auth": {"session_persistence": session}},
                recommended_action="检查 Camoufox exe/profile 配置；通过前不要进入搜索。",
            ),
            "elapsed_s": round(time.time() - started, 2),
        }

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return {
            **health,
            "probe": "camoufox_v2_login",
            "status": "degraded",
            "detail": f"Playwright unavailable: {exc}",
            "session_persistence": session,
            "platform_health_probe": build_platform_health_probe(
                platform="xiaohongshu",
                tool="camoufox_v2_probe",
                mode="read_only",
                status="fail",
                reason_code="DEPENDENCY_UNAVAILABLE",
                profile_id="xhs_camoufox_v2",
                profile_path=str(health.get("profile_dir") or ""),
                browser_base="camoufox_v2",
                risk_level="medium",
                health={"browser": health, "auth": {"session_persistence": session}},
                evidence={"error": str(exc)},
                recommended_action="安装/修复 Playwright 后再做 Camoufox 诊断。",
            ),
            "elapsed_s": round(time.time() - started, 2),
        }

    context = None
    page = None
    try:
        with sync_playwright() as p:
            context = p.firefox.launch_persistent_context(
                health["profile_dir"],
                executable_path=health["exe"],
                headless=True,
                timeout=timeout_ms,
                viewport={"width": 1280, "height": 900},
            )
            page = context.new_page()
            page.goto("https://www.xiaohongshu.com/explore", wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(1500)
            state = page.evaluate(
                """() => {
                    const text = document.body ? document.body.innerText.slice(0, 2000) : "";
                    const state = window.__INITIAL_STATE__ || {};
                    const user = state.user || state.userInfo || state.loginUser || {};
                    const userId = user.userId || user.id || user.userid || "";
                    const guest = user.guest === true || user.isGuest === true;
                    const loginMarkers = /登录|扫码|验证码/.test(text);
                    const verificationMarkers = /验证|安全|captcha|异常/.test(text);
                    return {
                        title: document.title || "",
                        url: location.href,
                        user_id_present: Boolean(userId),
                        guest: Boolean(guest),
                        login_markers: Boolean(loginMarkers),
                        verification_markers: Boolean(verificationMarkers),
                        body_sample: text.slice(0, 240)
                    };
                }"""
            )
            user_id_present = bool(state.get("user_id_present"))
            guest = bool(state.get("guest"))
            verification = bool(state.get("verification_markers"))
            login_markers = bool(state.get("login_markers"))
            ok = user_id_present and not guest and not verification
            if ok:
                status = "ok"
                detail = "Camoufox v2 login probe passed: user_id present and guest=false"
            elif user_id_present and not guest:
                status = "degraded"
                detail = "Camoufox v2 login state looks authenticated but page shows verification markers"
            elif user_id_present:
                status = "degraded"
                detail = "Camoufox v2 has user_id but guest=true; not eligible for backup admission"
            elif login_markers:
                status = "degraded" if session.get("status") != "ok" else "ok"
                detail = (
                    "Camoufox v2 session cookies are persisted; headless DOM still shows login markers"
                    if status == "ok"
                    else "Camoufox v2 page shows login markers"
                )
            else:
                status = "degraded" if session.get("status") != "ok" else "ok"
                detail = (
                    "Camoufox v2 session cookies are persisted; headless DOM state is inconclusive"
                    if status == "ok"
                    else "Camoufox v2 login state is inconclusive"
                )
            probe_status = "pass" if status == "ok" and user_id_present and not verification and not login_markers else ("fail" if session.get("status") != "ok" else "pass")
            reason_code = "OK" if probe_status == "pass" else "LOGIN_REQUIRED"
            return {
                **health,
                "probe": "camoufox_v2_login",
                "status": status,
                "detail": detail,
                "admission": "session_persistence_ok_dom_degraded" if status == "ok" and not user_id_present else ("ok" if status == "ok" else "blocked"),
                "session_persistence": session,
                "login_state": {
                    "user_id_present": user_id_present,
                    "guest": guest,
                    "headless_dom_probe": True,
                    "login_markers": login_markers,
                    "verification_markers": verification,
                    "title": state.get("title", ""),
                    "url": state.get("url", ""),
                },
                "platform_health_probe": build_platform_health_probe(
                    platform="xiaohongshu",
                    tool="camoufox_v2_probe",
                    mode="read_only",
                    status=probe_status,
                    reason_code=reason_code,
                    elapsed_ms=(time.time() - started) * 1000,
                    profile_id="xhs_camoufox_v2",
                    profile_path=str(health.get("profile_dir") or ""),
                    browser_base="camoufox_v2",
                    risk_scope="account" if reason_code == "LOGIN_REQUIRED" else "unknown",
                    risk_level="low" if probe_status == "pass" else "medium",
                    safe_to_retry=probe_status == "fail",
                    safe_to_switch_account=False,
                    manual_action_required=reason_code == "LOGIN_REQUIRED",
                    health={
                        "browser": health,
                        "auth": {"session_persistence": session},
                        "page": {
                            "headless_dom_probe": True,
                            "login_markers": login_markers,
                            "verification_markers": verification,
                        },
                    },
                    evidence={
                        "title_redacted": state.get("title", ""),
                        "final_url_redacted": state.get("url", ""),
                    },
                    recommended_action="Camoufox 可作为隔离诊断；自动搜索准入仍需 remote server/SDK 控制验证。",
                ),
                "elapsed_s": round(time.time() - started, 2),
            }
    except Exception as exc:
        probe_status = "pass" if session.get("status") == "ok" else "fail"
        return {
            **health,
            "probe": "camoufox_v2_login",
            "status": "degraded" if session.get("status") != "ok" else "ok",
            "detail": f"Camoufox v2 explicit probe failed: {exc}",
            "admission": "session_persistence_ok_dom_probe_failed" if session.get("status") == "ok" else "blocked",
            "session_persistence": session,
            "platform_health_probe": build_platform_health_probe(
                platform="xiaohongshu",
                tool="camoufox_v2_probe",
                mode="read_only",
                status=probe_status,
                reason_code="OK" if probe_status == "pass" else "BROWSER_START_FAILED",
                elapsed_ms=(time.time() - started) * 1000,
                profile_id="xhs_camoufox_v2",
                profile_path=str(health.get("profile_dir") or ""),
                browser_base="camoufox_v2",
                risk_level="low" if probe_status == "pass" else "medium",
                safe_to_retry=probe_status == "fail",
                health={"browser": health, "auth": {"session_persistence": session}},
                evidence={"probe_error": str(exc)[:240]},
                recommended_action="session cookie 可用时仅代表持久化通过；控制通道仍需 remote server/SDK 攻坚。",
            ),
            "elapsed_s": round(time.time() - started, 2),
        }
    finally:
        try:
            if page:
                page.close()
        except Exception:
            pass
        try:
            if context:
                context.close()
        except Exception:
            pass
