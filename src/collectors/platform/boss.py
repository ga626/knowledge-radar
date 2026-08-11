"""BOSS直聘采集器 - 通过 CDP + stealth.js 绕过反爬检测。

隔离试验方案：独立端口 9337，独立 Profile 目录。
选择器从 config/selectors.json 动态加载，修改配置无需重启 MCP 服务。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List

import websocket

from kr_core.collection import CollectionTrace, format_search_error, format_search_response
from runtime.chrome_manager import BOSS_CHROME_DEBUG_PORT, _ensure_chrome_debugging, _managed_chrome_profile_dir, finish_chrome_automation
from runtime.page_access_state import classify_page_access_state, merge_page_state
from runtime.profile_registry import record_profile_state, select_main_chain_profile
from runtime.recruitment_city import resolve_recruitment_city
from runtime.recruitment_governance import check_search_gate, record_search_outcome
from runtime.recruitment_network import extract_recruitment_items_from_payloads, recruitment_network_observer_script

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SELECTORS_PATH = os.path.join(PROJECT_ROOT, "config", "selectors.json")


def _load_selectors(platform: str = "boss") -> Dict:
    """从配置文件加载选择器（每次调用时读取，修改配置无需重启）。"""
    try:
        with open(SELECTORS_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
        return config.get("platforms", {}).get(platform, {})
    except Exception:
        return {}
log = logging.getLogger("mcp-server")

BOSS_AUTH_KEEPALIVE_INTERVAL_S = int(os.environ.get("KR_BOSS_AUTH_KEEPALIVE_INTERVAL_S", "3600"))
BOSS_AUTH_MANUAL_COOLDOWN_S = int(os.environ.get("KR_BOSS_AUTH_MANUAL_COOLDOWN_S", "1800"))
BOSS_COOKIE_DOMAIN_MARKERS = ("zhipin.com", "bosszhipin.com")
BOSS_SESSION_COOKIE_MARKERS = ("zp_stoken", "wt2", "bst", "sid", "identity", "token", "session")

class BossCdpError(RuntimeError):
    def __init__(self, message: str, *, failure_type: str = "cdp_error", retryable: bool = True) -> None:
        super().__init__(message)
        self.failure_type = failure_type
        self.retryable = retryable


_CDP_SEQ = 0


def _selector_values(platform: str, key: str, fallback: list[str]) -> list[str]:
    selectors = _load_selectors(platform)
    fields = selectors.get("fields") if isinstance(selectors.get("fields"), dict) else {}
    value = selectors.get(key) if key == "card_selector" else fields.get(key)
    values = []
    if key == "card_selector" and isinstance(selectors.get("card_selectors"), list):
        values.extend(str(item) for item in selectors.get("card_selectors") or [] if str(item).strip())
    if isinstance(value, str) and value.strip():
        values.append(value)
    values.extend(fallback)
    deduped: list[str] = []
    for item in values:
        if item and item not in deduped:
            deduped.append(item)
    return deduped


def _json_array(values: list[str]) -> str:
    return json.dumps(values, ensure_ascii=False)


def _fetch_json(url: str, timeout: float = 5.0) -> Dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 KnowledgeRadar/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _cdp_send(ws, method: str, params: Dict | None = None, session_id: str | None = None, timeout: float = 15.0) -> Dict:
    global _CDP_SEQ
    _CDP_SEQ += 1
    msg = {"id": _CDP_SEQ, "method": method, "params": params or {}}
    if session_id:
        msg["sessionId"] = session_id
    previous_timeout = ws.gettimeout()
    ws.settimeout(timeout)
    try:
        ws.send(json.dumps(msg, ensure_ascii=False))
        while True:
            raw = ws.recv()
            data = json.loads(raw)
            if data.get("id") == _CDP_SEQ:
                if data.get("error"):
                    raise BossCdpError(f"CDP {method} 失败: {data['error']}", failure_type="cdp_method_error")
                return data
    finally:
        ws.settimeout(previous_timeout)


def _cdp_wait_for_page_state(ws, session_id: str, expression: str, *, timeout_s: float = 12.0, interval_s: float = 0.4) -> Dict[str, Any]:
    """Poll a lightweight page classifier and return as soon as a terminal state appears."""
    deadline = time.monotonic() + max(0.5, timeout_s)
    last: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        evaluated = _cdp_send(
            ws,
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            session_id,
            timeout=5.0,
        )
        raw = ((evaluated.get("result") or {}).get("result") or {}).get("value") or "{}"
        try:
            last = json.loads(raw)
        except json.JSONDecodeError:
            last = {"ready": False, "error": "wait_state_json_decode_failed", "raw": str(raw)[:200]}
        if last.get("ready"):
            return last
        time.sleep(interval_s)
    last.setdefault("ready", False)
    last["wait_timeout"] = True
    return last


def _fetch_boss_joblist_payloads(ws, session_id: str, keyword: str, city_code: str) -> list[dict[str, Any]]:
    """Fetch BOSS's same-origin job list JSON from the logged-in page context."""

    expr = f"""
        (async () => {{
            const keyword = {json.dumps(str(keyword or ""), ensure_ascii=False)};
            const city = {json.dumps(str(city_code or ""), ensure_ascii=False)};
            const params = new URLSearchParams();
            if (keyword) params.set('query', keyword);
            if (city) params.set('city', city);
            params.set('page', '1');
            const variants = [
                `/wapi/zpgeek/search/joblist.json?${{params.toString()}}`,
                `/wapi/zpgeek/search/joblist.json?${{params.toString()}}&scene=1`
            ];
            const decoder = new TextDecoder('utf-8');
            const entries = [];
            for (const path of variants) {{
                try {{
                    const response = await fetch(path, {{
                        credentials: 'include',
                        headers: {{ accept: 'application/json, text/plain, */*' }}
                    }});
                    const buffer = await response.arrayBuffer();
                    entries.push({{
                        url: new URL(path, location.href).href,
                        status: response.status,
                        contentType: response.headers.get('content-type') || '',
                        body: decoder.decode(buffer).slice(0, 300000)
                    }});
                }} catch (error) {{
                    entries.push({{
                        url: path,
                        status: 0,
                        error: String(error && error.message || error)
                    }});
                }}
            }}
            return JSON.stringify(entries);
        }})()
    """
    try:
        evaluated = _cdp_send(
            ws,
            "Runtime.evaluate",
            {"expression": expr, "returnByValue": True, "awaitPromise": True},
            session_id,
            timeout=20.0,
        )
        raw = ((evaluated.get("result") or {}).get("result") or {}).get("value") or "[]"
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except Exception as exc:
        log.debug(f"BOSS joblist API fetch failed: {exc}")
        return []


def _format_search_response(
    platform: str,
    items: List[Dict],
    *,
    trace: CollectionTrace | None = None,
) -> Dict:
    return format_search_response(platform, items, trace=trace)


def _format_search_error(
    platform: str,
    error_item: Dict,
    *,
    trace: CollectionTrace | None = None,
    strategy: str = "",
) -> Dict:
    return format_search_error(platform, error_item, trace=trace, strategy=strategy)


def _boss_city_param(city: str) -> str:
    return str(resolve_recruitment_city("boss", city).get("param_value") or "")


def _build_boss_search_url(keyword: str, city: str = "") -> str:
    params = {
        "query": keyword or "",
    }
    city_code = _boss_city_param(city)
    if city_code:
        params["city"] = city_code
    return f"https://www.zhipin.com/web/geek/jobs?{urllib.parse.urlencode(params)}"


def _parse_boss_cards_from_page(port: int, keyword: str, city: str, limit: int) -> Dict:
    timings: Dict[str, float] = {}
    t0 = time.perf_counter()
    targets = _fetch_json(f"http://127.0.0.1:{port}/json/list")
    timings["target_list_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    if not isinstance(targets, list):
        raise BossCdpError("Chrome 调试端口返回异常 target 列表", failure_type="cdp_target_error")
    page = next(
        (t for t in targets if t.get("type") == "page" and "zhipin.com" in str(t.get("url") or "")),
        None,
    ) or next((t for t in targets if t.get("type") == "page"), None)
    if not page:
        raise BossCdpError("未找到可用 Chrome 页面 target", failure_type="no_page_target")

    version = _fetch_json(f"http://127.0.0.1:{port}/json/version")
    ws_url = version.get("webSocketDebuggerUrl")
    if not ws_url:
        raise BossCdpError("Chrome 调试端口未返回 WebSocket URL", failure_type="cdp_version_error")

    ws = websocket.create_connection(
        ws_url,
        timeout=15,
        origin=f"http://127.0.0.1:{port}",
    )
    try:
        attached = _cdp_send(ws, "Target.attachToTarget", {"targetId": page["id"], "flatten": True})
        session_id = attached["result"]["sessionId"]
        _cdp_send(ws, "Page.enable", {}, session_id)
        _cdp_send(ws, "Runtime.enable", {}, session_id)
        observer_script = recruitment_network_observer_script()
        _cdp_send(ws, "Page.addScriptToEvaluateOnNewDocument", {"source": observer_script}, session_id)
        _cdp_send(ws, "Runtime.evaluate", {"expression": observer_script, "awaitPromise": True}, session_id)

        city_resolution = resolve_recruitment_city("boss", city)
        search_url = _build_boss_search_url(keyword, city)
        nav_start = time.perf_counter()
        _cdp_send(ws, "Page.navigate", {"url": search_url}, session_id)
        timings["navigate_command_ms"] = round((time.perf_counter() - nav_start) * 1000, 1)

        card_selectors = _selector_values(
            "boss",
            "card_selector",
            [".job-card-box", ".job-card-wrapper", ".job-primary", 'li[class*="job-card"]', 'div[class*="job-card"]'],
        )
        title_selectors = _selector_values("boss", "title", [".job-name", ".job-title", '[class*="job-name"]', '[class*="job-title"]'])
        salary_selectors = _selector_values("boss", "salary", [".job-salary", ".salary", '[class*="salary"]'])
        company_selectors = _selector_values("boss", "company", [".company-name", ".boss-name", '[class*="company-name"]', '[class*="boss-name"]'])
        area_selectors = _selector_values("boss", "area", [".company-location", ".job-area", '[class*="location"]', '[class*="area"]'])
        link_selectors = _selector_values("boss", "link", ['a[href*="/job_detail/"]', 'a[href*="/job/"]'])
        wait_expr = f"""
            (() => {{
                const text = document.body ? document.body.innerText || '' : '';
                const cardSelectors = {_json_array(card_selectors)};
                const cardCount = cardSelectors.reduce((sum, sel) => sum + document.querySelectorAll(sel).length, 0);
                const networkCount = (window.__KR_RECRUITMENT_NETWORK__ && window.__KR_RECRUITMENT_NETWORK__.entries || []).length;
                const blocked = /安全验证|滑动验证|captcha|verify|访问异常|请求异常/i.test(text);
                const loginRequired = /扫码登录|密码登录|验证码登录|注册|立即登录/.test(text)
                    && !/沟通过|已投递|在线简历|附件简历|求职助手|我的|职位/.test(text);
                const empty = /暂无职位|没有找到|无搜索结果|换个关键词/.test(text);
                return JSON.stringify({{
                    ready: cardCount > 0 || networkCount > 0 || blocked || loginRequired || empty || document.readyState === 'complete',
                    cardCount,
                    networkCount,
                    blocked,
                    loginRequired,
                    empty,
                    readyState: document.readyState,
                    url: location.href,
                    title: document.title
                }});
            }})()
        """
        wait_start = time.perf_counter()
        wait_state = _cdp_wait_for_page_state(ws, session_id, wait_expr, timeout_s=float(os.environ.get("KR_BOSS_READY_TIMEOUT_S", "12")))
        timings["ready_wait_ms"] = round((time.perf_counter() - wait_start) * 1000, 1)
        api_fetch_start = time.perf_counter()
        api_entries = _fetch_boss_joblist_payloads(ws, session_id, keyword, str(city_resolution.get("param_value") or ""))
        timings["joblist_api_fetch_ms"] = round((time.perf_counter() - api_fetch_start) * 1000, 1)

        extract_expr = f"""
            (() => {{
                const maxItems = {int(limit)};
                const text = document.body ? document.body.innerText || '' : '';
                const blocked = /安全验证|滑动验证|captcha|verify|访问异常|请求异常/i.test(text);
                const loginRequired = /扫码登录|密码登录|验证码登录|注册/.test(text) && !/沟通过|已投递|在线简历|附件简历|求职助手|我的/.test(text);
                const cardSelectors = {_json_array(card_selectors)};
                const titleSelectors = {_json_array(title_selectors)};
                const salarySelectors = {_json_array(salary_selectors)};
                const companySelectors = {_json_array(company_selectors)};
                const areaSelectors = {_json_array(area_selectors)};
                const linkSelectors = {_json_array(link_selectors)};
                const cards = Array.from(new Set(cardSelectors.flatMap(sel => Array.from(document.querySelectorAll(sel)))));
                const items = [];
                for (const card of cards) {{
                    try {{
                        const getText = (sels) => {{
                            for (const sel of sels) {{
                                const el = card.querySelector(sel);
                                const value = el && (el.innerText || el.textContent || '').trim();
                                if (value) return value.replace(/\\s+/g, ' ');
                            }}
                            return '';
                        }};
                        const titleEl = linkSelectors.map(sel => card.querySelector(sel)).find(Boolean) || card.querySelector('a[href]');
                        const title = getText(titleSelectors) || (titleEl ? (titleEl.innerText || titleEl.textContent || '').trim() : '');
                        const salary = getText(salarySelectors);
                        const company = getText(companySelectors);
                        const area = getText(areaSelectors);
                        const href = titleEl ? titleEl.href || titleEl.getAttribute('href') || '' : '';
                        const url = href && href.startsWith('http') ? href : (href ? new URL(href, location.href).href : '');
                        if (title) items.push({{ title, salary, company, area, url, platform: 'BOSS直聘' }});
                    }} catch (e) {{}}
                    if (items.length >= maxItems) break;
                }}
                return JSON.stringify({{
                    items,
                    networkEntries: (window.__KR_RECRUITMENT_NETWORK__ && window.__KR_RECRUITMENT_NETWORK__.entries || []).slice(-40),
                    blocked,
                    loginRequired,
                    cardCount: cards.length,
                    waitState: {json.dumps("WAIT_STATE_PLACEHOLDER")},
                    title: document.title,
                    url: location.href,
                    textSample: text.slice(0, 800)
                }});
            }})()
        """
        extract_expr = extract_expr.replace('"WAIT_STATE_PLACEHOLDER"', json.dumps(wait_state, ensure_ascii=False))
        extract_start = time.perf_counter()
        evaluated = _cdp_send(
            ws,
            "Runtime.evaluate",
            {"expression": extract_expr, "returnByValue": True, "awaitPromise": True},
            session_id,
        )
        timings["extract_ms"] = round((time.perf_counter() - extract_start) * 1000, 1)
        raw = ((evaluated.get("result") or {}).get("result") or {}).get("value") or "{}"
        data = json.loads(raw)
        combined_network_entries = list(api_entries) + list(data.get("networkEntries") or [])
        network_items, network_diag = extract_recruitment_items_from_payloads(
            combined_network_entries,
            platform="boss",
            keyword=keyword,
            city=city,
            limit=limit,
        )
        if network_items:
            data["items"] = network_items
            data["networkSearch"] = network_diag
        data["diagnostics"] = {
            "schema": "knowledgeradar-recruitment-diagnostics/v1",
            "platform": "boss",
            "layers": {
                "connection": {"status": "ok", "port": port},
                "navigation": {"status": "ok", "url": data.get("url") or search_url, "title": data.get("title") or ""},
                "params": {
                    "status": "ok" if city_resolution.get("status") != "missing" else "city_mapping_missing",
                    "keyword": keyword,
                    "city": city,
                    "city_param": city_resolution.get("param_value") or "",
                    "city_resolution": city_resolution,
                },
                "parser": {
                    "status": "ok" if data.get("items") else "empty",
                    "card_count": data.get("cardCount", 0),
                    "network_item_count": len(network_items),
                },
                "evidence": {"status": "network_json" if network_items else "search_cards" if data.get("items") else "none"},
            },
            "timings_ms": timings,
            "wait_state": wait_state,
            "network_search": network_diag,
        }
        return data
    finally:
        ws.close()


def _classify_boss_page_state(data: Dict[str, Any]) -> Dict[str, Any]:
    text = str(data.get("textSample") or "")
    title = str(data.get("title") or data.get("pageTitle") or "")
    url = str(data.get("url") or "")
    blocked = bool(data.get("blocked")) or bool(re.search(r"安全验证|滑动验证|captcha|verify|访问异常|请求异常", text, re.I))
    login_required = bool(data.get("loginRequired")) or (
        bool(re.search(r"扫码登录|密码登录|验证码登录|注册|立即登录", text))
        and not bool(re.search(r"沟通过|已投递|在线简历|附件简历|求职助手|我的|职位", text))
    )
    if blocked:
        return {
            "auth_state": "platform_verification_required",
            "status": "needs_interaction",
            "manual_action_required": True,
            "failure_type": "platform_verification_required",
            "detail": "BOSS 页面触发安全验证",
            "url": url,
            "title": title,
        }
    if login_required or "/web/user" in url:
        return {
            "auth_state": "login_required",
            "status": "needs_interaction",
            "manual_action_required": True,
            "failure_type": "login_required",
            "detail": "BOSS 页面需要登录",
            "url": url,
            "title": title,
        }
    if "zhipin.com" in url and ("/web/geek" in url or "/job_detail/" in url or "职位" in text or "我的" in text):
        return {
            "auth_state": "authenticated",
            "status": "ok",
            "manual_action_required": False,
            "failure_type": "",
            "detail": "BOSS 页面登录态可用",
            "url": url,
            "title": title,
        }
    return {
        "auth_state": "unknown",
        "status": "unknown",
        "manual_action_required": False,
        "failure_type": "unknown",
        "detail": "BOSS 页面状态无法自动判定",
        "url": url,
        "title": title,
    }


def _boss_query_reflected(keyword: str, data: Dict[str, Any]) -> bool | None:
    value = str(keyword or "").strip()
    if not value:
        return None
    haystack = "\n".join(
        [
            urllib.parse.unquote(str(data.get("url") or "")),
            str(data.get("title") or ""),
            str(data.get("textSample") or ""),
        ]
    )
    return value in haystack


def _boss_cookie_quality(port: int) -> Dict[str, Any]:
    """Return sanitized BOSS cookie quality without cookie values."""
    try:
        version = _fetch_json(f"http://127.0.0.1:{port}/json/version")
        ws_url = version.get("webSocketDebuggerUrl")
        if not ws_url:
            raise BossCdpError("Chrome 调试端口未返回 WebSocket URL", failure_type="cdp_version_error")
        ws = websocket.create_connection(ws_url, timeout=10, origin=f"http://127.0.0.1:{port}")
        try:
            try:
                result = _cdp_send(ws, "Network.getAllCookies", {}, timeout=10)
            except Exception:
                result = _cdp_send(ws, "Storage.getCookies", {}, timeout=10)
        finally:
            ws.close()
        cookies = list(((result.get("result") or {}).get("cookies") or []))
        scoped = [
            cookie
            for cookie in cookies
            if any(marker in str(cookie.get("domain") or "") for marker in BOSS_COOKIE_DOMAIN_MARKERS)
        ]
        names = sorted({str(cookie.get("name") or "") for cookie in scoped if cookie.get("name")})
        now = time.time()
        expiries = [
            float(cookie.get("expires") or 0)
            for cookie in scoped
            if float(cookie.get("expires") or 0) > 0
        ]
        min_expiry = min(expiries) if expiries else 0
        has_session_like = any(any(marker in name.lower() for marker in BOSS_SESSION_COOKIE_MARKERS) for name in names)
        if not scoped:
            quality = "missing"
        elif not has_session_like:
            quality = "weak"
        elif min_expiry and min_expiry < now + 86400:
            quality = "near_expiry"
        else:
            quality = "present"
        return {
            "status": "ok",
            "quality": quality,
            "domain_cookie_count": len(scoped),
            "cookie_name_count": len(names),
            "observed_cookie_names": names[:20],
            "session_cookie_present": has_session_like,
            "expiry_bucket": "session_only" if not expiries else ("lt_24h" if min_expiry < now + 86400 else "gte_24h"),
        }
    except Exception as exc:
        return {
            "status": "degraded",
            "quality": "unknown",
            "error": str(exc),
            "domain_cookie_count": 0,
            "cookie_name_count": 0,
            "observed_cookie_names": [],
            "session_cookie_present": False,
            "expiry_bucket": "unknown",
        }


def _record_boss_auth_profile_state(auth_result: Dict[str, Any]) -> None:
    profile = select_main_chain_profile("boss")
    profile_id = str(profile.get("profile_id") or "")
    if not profile_id:
        return
    auth_state = str(auth_result.get("auth_state") or "unknown")
    if auth_state == "authenticated":
        state = "healthy"
        cooldown = 0
        manual = False
        safe = False
    elif auth_state in {"login_required", "platform_verification_required"}:
        state = "blocked"
        # Keep the managed profile selectable for the recovery flow; the probe
        # result carries the user-action signal.
        cooldown = 0
        manual = False
        safe = False
    else:
        state = "degraded"
        cooldown = 0
        manual = False
        safe = False
    record_profile_state(
        profile_id,
        platform="boss",
        state=state,
        reason_code=auth_state,
        cooldown_seconds=cooldown,
        manual_action_required=manual,
        safe_to_switch_account=safe,
        last_tool="probe_browser_auth:boss",
        notes=[
            f"auth_state={auth_state}",
            f"cookie_quality={(auth_result.get('cookie_quality') or {}).get('quality', 'unknown')}",
            f"next_check_at={int(auth_result.get('next_check_at') or 0)}",
            f"manual_action_required={bool(auth_result.get('manual_action_required'))}",
        ],
    )


def probe_boss_auth_state(*, keepalive: bool = True) -> Dict[str, Any]:
    """Probe BOSS login state using the existing managed Chrome profile."""
    try:
        if not _ensure_chrome_debugging("boss", visible=False, detach=False):
            return {
                "status": "degraded",
                "platform": "boss",
                "auth_state": "cdp_unavailable",
                "manual_action_required": False,
                "detail": "后台登录态探针无法启动或连接 BOSS 受管 Chrome",
                "retryable": True,
            }
        # Reuse the current search-page parser in a no-result low-impact mode.
        data = _parse_boss_cards_from_page(int(BOSS_CHROME_DEBUG_PORT), "", "", 1)
        classified = _classify_boss_page_state(data)
        cookie_quality = _boss_cookie_quality(int(BOSS_CHROME_DEBUG_PORT))
        now = time.time()
        result = {
            **classified,
            "platform": "boss",
            "schema": "knowledgeradar-boss-auth-probe/v1",
            "profile_dir": _managed_chrome_profile_dir("boss"),
            "keepalive": bool(keepalive),
            "last_observed_at": int(now),
            "next_check_at": int(now + BOSS_AUTH_KEEPALIVE_INTERVAL_S),
            "cookie_quality": cookie_quality,
            "recommended_action": (
                "health_check(mode='request_browser_interaction:boss:login_or_security_verification')"
                if classified.get("manual_action_required")
                else ""
            ),
        }
        _record_boss_auth_profile_state(result)
        return result
    except BossCdpError as exc:
        return {
            "status": "degraded",
            "platform": "boss",
            "auth_state": exc.failure_type,
            "manual_action_required": False,
            "detail": str(exc),
            "retryable": exc.retryable,
        }
    except Exception as exc:
        return {
            "status": "degraded",
            "platform": "boss",
            "auth_state": "probe_error",
            "manual_action_required": False,
            "detail": str(exc),
            "retryable": True,
        }
    finally:
        finish_chrome_automation("boss", reason="boss_auth_probe")


def boss_search_via_cdp_state(keyword: str, city: str = "", limit: int = 15) -> Dict:
    """通过 CDP 控制 Chrome 搜索 BOSS直聘职位，并保留页面状态。"""
    city_resolution = resolve_recruitment_city("boss", city)
    if city and city_resolution.get("status") == "missing":
        return {
            "items": [],
            "status": "failed",
            "failure_type": "city_mapping_missing",
            "platform_state": "city_mapping_missing",
            "manual_action_required": False,
            "diagnostics": {
                "schema": "knowledgeradar-recruitment-diagnostics/v1",
                "platform": "boss",
                "layers": {
                    "connection": {"status": "not_run"},
                    "navigation": {"status": "not_run"},
                    "params": {"status": "city_mapping_missing", "keyword": keyword, "city": city, "city_resolution": city_resolution},
                    "parser": {"status": "not_run"},
                    "evidence": {"status": "none"},
                },
            },
        }
    try:
        _ensure_chrome_debugging("boss")
    except RuntimeError as exc:
        return {
            "items": [],
            "status": "failed",
            "failure_type": "cdp_unavailable",
            "platform_state": "cdp_unavailable",
            "manual_action_required": False,
            "error": str(exc),
            "diagnostics": {
                "schema": "knowledgeradar-recruitment-diagnostics/v1",
                "platform": "boss",
                "layers": {
                    "connection": {"status": "failed", "failure_type": "cdp_unavailable", "port": int(BOSS_CHROME_DEBUG_PORT)},
                    "navigation": {"status": "not_run"},
                    "params": {"status": "ok", "keyword": keyword, "city": city, "city_resolution": city_resolution},
                    "parser": {"status": "not_run"},
                    "evidence": {"status": "none"},
                },
            },
        }
    try:
        data = _parse_boss_cards_from_page(int(BOSS_CHROME_DEBUG_PORT), keyword, city, limit)
        items = data.get("items", [])
        deduped: List[Dict] = []
        seen = set()
        for item in items:
            key = item.get("url") or f"{item.get('title')}|{item.get('company')}|{item.get('area')}"
            if key in seen:
                continue
            seen.add(key)
            item.setdefault(
                "field_confidence",
                {
                    "title": "medium_search_card",
                    "company": "low_to_medium_search_card",
                    "salary": "low_search_card",
                    "area": "medium_search_card",
                    "url": "medium_search_card",
                },
            )
            item.setdefault("salary_claim_allowed", False)
            deduped.append(item)
        items = deduped
        text = str(data.get("textSample") or "")
        route_decision = "network_items" if any(str(item.get("source") or "").endswith("_network_search") for item in items) else "dom_items" if items else "needs_route_repair"
        classified = classify_page_access_state(
            platform="boss",
            operation="search",
            blocked_marker=bool(data.get("blocked")),
            login_marker=bool(data.get("loginRequired")),
            rate_limit_marker=bool(re.search(r"访问频繁|请求频繁|稍后再试|429", text, re.I)),
            result_item_count=len(items),
            card_count=data.get("cardCount"),
            empty_marker=bool((data.get("waitState") or {}).get("empty")) or bool(re.search(r"暂无职位|没有找到|无搜索结果|换个关键词", text)),
            structured_list_expected=True,
            query_reflected=_boss_query_reflected(keyword, data),
            extra_signals={
                "title": data.get("title") or "",
                "url": data.get("url") or "",
                "route_probe": {
                    "query_reflected": _boss_query_reflected(keyword, data),
                    "card_count": data.get("cardCount") or 0,
                    "job_link_count": 0,
                    "wait_empty": bool((data.get("waitState") or {}).get("empty")),
                    "route_decision": route_decision,
                    "network_item_count": len([item for item in items if str(item.get("source") or "").endswith("_network_search")]),
                },
            },
        )
        data = merge_page_state({**data, "items": items}, classified)

        if items:
            log.info(f"BOSS直聘 CDP 搜索返回 {len(items)} 条结果")
            return {**data, "items": items[:limit], "status": "ok", "failure_type": ""}

        if data.get("status") == "needs_interaction":
            if data.get("failure_type") == "platform_verification_required":
                log.warning("BOSS直聘安全验证触发，需要人工介入")
            elif data.get("failure_type") == "login_required":
                log.info("BOSS直聘需要登录")
            return {**data, "items": []}

        if data.get("status") == "retry_later":
            log.info("BOSS直聘触发频控，稍后重试")
            return {**data, "items": []}

        log.debug(f"BOSS直聘 CDP 搜索无结果，页面标题: {data.get('title', '?')}")
        if data.get("textSample"):
            log.debug(f"页面文本样本: {data['textSample'][:200]}")
        return {**data, "items": []}

    except BossCdpError as e:
        log.debug(f"BOSS直聘 CDP 搜索失败: {e}")
        raise
    except Exception as e:
        log.debug(f"BOSS直聘 CDP 搜索异常: {e}")
        raise BossCdpError(str(e), failure_type="cdp_runtime_error", retryable=True) from e


def boss_search_via_cdp(keyword: str, city: str = "", limit: int = 15) -> List[Dict]:
    """Compatibility wrapper returning only items."""
    return list(boss_search_via_cdp_state(keyword, city, limit).get("items") or [])


def boss_detail_via_cdp(url: str) -> Dict:
    """Extract a BOSS job detail page through the managed logged-in Chrome."""
    if not url or "zhipin.com" not in url:
        return {
            "status": "failed",
            "error": "不是 BOSS直聘职位详情 URL",
            "failure_type": "unsupported_url",
            "platform": "BOSS直聘",
            "url": url,
        }
    try:
        timings: Dict[str, float] = {}
        started = time.perf_counter()
        _ensure_chrome_debugging("boss")
        timings["ensure_chrome_ms"] = round((time.perf_counter() - started) * 1000, 1)
        targets = _fetch_json(f"http://127.0.0.1:{BOSS_CHROME_DEBUG_PORT}/json/list")
        page = next(
            (t for t in targets if t.get("type") == "page" and "zhipin.com" in str(t.get("url") or "")),
            None,
        ) or next((t for t in targets if t.get("type") == "page"), None)
        if not page:
            raise BossCdpError("未找到可用 Chrome 页面 target", failure_type="no_page_target")

        version = _fetch_json(f"http://127.0.0.1:{BOSS_CHROME_DEBUG_PORT}/json/version")
        ws = websocket.create_connection(
            version["webSocketDebuggerUrl"],
            timeout=15,
            origin=f"http://127.0.0.1:{BOSS_CHROME_DEBUG_PORT}",
        )
        try:
            attached = _cdp_send(ws, "Target.attachToTarget", {"targetId": page["id"], "flatten": True})
            session_id = attached["result"]["sessionId"]
            _cdp_send(ws, "Page.enable", {}, session_id)
            _cdp_send(ws, "Runtime.enable", {}, session_id)
            nav_start = time.perf_counter()
            _cdp_send(ws, "Page.navigate", {"url": url}, session_id)
            timings["navigate_command_ms"] = round((time.perf_counter() - nav_start) * 1000, 1)
            wait_expr = r"""
                (() => {
                    const text = document.body ? document.body.innerText || '' : '';
                    const blocked = /安全验证|滑动验证|captcha|verify|访问异常|请求异常/i.test(text);
                    const loginRequired = /扫码登录|密码登录|验证码登录|注册/.test(text)
                        && !/职位详情|职位描述|岗位职责|任职要求|在线沟通/.test(text);
                    const detailText = Array.from(document.querySelectorAll('.job-sec-text, .job-detail-section, .job-detail, .detail-content, [class*="job-sec"], [class*="detail"]'))
                        .map(el => (el.innerText || el.textContent || '').trim())
                        .filter(Boolean)
                        .sort((a, b) => b.length - a.length)[0] || '';
                    return JSON.stringify({
                        ready: detailText.length >= 80 || blocked || loginRequired || document.readyState === 'complete',
                        detailChars: detailText.length,
                        blocked,
                        loginRequired,
                        readyState: document.readyState,
                        url: location.href,
                        title: document.title
                    });
                })()
            """
            wait_start = time.perf_counter()
            wait_state = _cdp_wait_for_page_state(ws, session_id, wait_expr, timeout_s=float(os.environ.get("KR_BOSS_DETAIL_READY_TIMEOUT_S", "10")))
            timings["ready_wait_ms"] = round((time.perf_counter() - wait_start) * 1000, 1)
            expr = r"""
                (() => {
                    const text = document.body ? document.body.innerText || '' : '';
                    const blocked = /安全验证|滑动验证|captcha|verify|访问异常|请求异常/i.test(text);
                    const loginRequired = /扫码登录|密码登录|验证码登录|注册/.test(text) && !/职位详情|职位描述|岗位职责|任职要求|在线沟通/.test(text);
                    const getText = (sels) => {
                        for (const sel of sels) {
                            const el = document.querySelector(sel);
                            const value = el && (el.innerText || el.textContent || '').trim();
                            if (value) return value.replace(/\s+/g, ' ');
                        }
                        return '';
                    };
                    const title = getText(['.job-name', '.name', '[class*="job-name"]', '[class*="job-title"]', 'h1']);
                    const salary = getText(['.job-salary', '.salary', '[class*="salary"]']);
                    const company = getText(['.company-name', '[class*="company-name"]', '.sider-company .name']);
                    const jd = getText([
                        '.job-sec-text',
                        '.job-detail-section',
                        '.job-detail',
                        '.detail-content',
                        '[class*="job-sec"]',
                        '[class*="detail"]'
                    ]);
                    return JSON.stringify({
                        blocked,
                        loginRequired,
                        title,
                        salary,
                        company,
                        jd,
                        url: location.href,
                        pageTitle: document.title,
                        textSample: text.slice(0, 1000)
                    });
                })()
            """
            extract_start = time.perf_counter()
            evaluated = _cdp_send(
                ws,
                "Runtime.evaluate",
                {"expression": expr, "returnByValue": True, "awaitPromise": True},
                session_id,
            )
            timings["extract_ms"] = round((time.perf_counter() - extract_start) * 1000, 1)
            raw = ((evaluated.get("result") or {}).get("result") or {}).get("value") or "{}"
            data = json.loads(raw)
            data["waitState"] = wait_state
            data["diagnostics"] = {
                "schema": "knowledgeradar-recruitment-diagnostics/v1",
                "platform": "boss",
                "layers": {
                    "connection": {"status": "ok", "port": int(BOSS_CHROME_DEBUG_PORT)},
                    "navigation": {"status": "ok", "url": data.get("url") or url, "title": data.get("pageTitle") or ""},
                    "params": {"status": "ok", "url": url},
                    "parser": {"status": "ok" if data.get("jd") else "empty", "content_chars": len(str(data.get("jd") or ""))},
                    "evidence": {"status": "detail_text" if data.get("jd") else "none"},
                },
                "timings_ms": timings,
                "wait_state": wait_state,
            }
        finally:
            ws.close()
            finish_chrome_automation("boss", reason="boss_detail")

        jd = str(data.get("jd") or "")
        classified = classify_page_access_state(
            platform="boss",
            operation="detail",
            blocked_marker=bool(data.get("blocked")),
            login_marker=bool(data.get("loginRequired")),
            content_chars=len(jd),
            content_readable=len(jd) >= 80,
            extra_signals={
                "title": data.get("title") or data.get("pageTitle") or "",
                "url": data.get("url") or url,
            },
        )
        data = merge_page_state(data, classified)

        if data.get("status") == "needs_interaction":
            return {
                "status": "needs_interaction",
                "platform": "BOSS直聘",
                "url": url,
                "failure_type": str(data.get("failure_type") or "manual_action_required"),
                "platform_state": str(data.get("platform_state") or "manual_action_required"),
                "manual_action_required": True,
                "hint": "请通过统一浏览器人工交互入口完成 BOSS 登录或安全验证后重试。",
                "content": data.get("textSample") or "",
            }
        if len(jd) < 80:
            return {
                "status": "empty",
                "platform": "BOSS直聘",
                "url": data.get("url") or url,
                "title": data.get("title") or data.get("pageTitle") or "",
                "salary": data.get("salary") or "",
                "company": data.get("company") or "",
                "content": jd or data.get("textSample") or "",
                "diagnostics": data.get("diagnostics") or {},
                "error": "BOSS 职位详情正文过短",
                "failure_type": "empty_detail",
                "field_confidence": {
                    "title": "low_empty_detail",
                    "company": "low_empty_detail",
                    "salary": "low_empty_detail",
                    "content": "low_empty_detail",
                },
                "salary_claim_allowed": False,
            }
        return {
            "status": "ok",
            "platform": "BOSS直聘",
            "url": data.get("url") or url,
            "title": data.get("title") or data.get("pageTitle") or "",
            "salary": data.get("salary") or "",
            "company": data.get("company") or "",
            "content": jd,
            "jd": jd,
            "source": "boss_cdp_detail",
            "warning_type": data.get("warning_type") or "",
            "platform_state": data.get("platform_state") or "detail_ok",
            "manual_action_required": False,
            "page_state": data.get("page_state") or {},
            "diagnostics": data.get("diagnostics") or {},
            "field_confidence": {
                "title": "high_detail",
                "company": "high_detail",
                "salary": "high_detail_when_present",
                "content": "high_detail",
            },
            "salary_claim_allowed": True,
            "company_identity_status": "detail_preferred",
        }
    except BossCdpError as e:
        return {
            "status": "failed",
            "platform": "BOSS直聘",
            "url": url,
            "error": str(e),
            "failure_type": e.failure_type,
        }
    except Exception as e:
        return {
            "status": "failed",
            "platform": "BOSS直聘",
            "url": url,
            "error": str(e),
            "failure_type": "cdp_runtime_error",
        }


def _bring_boss_to_front_for_login() -> None:
    """已弃用：不再自动弹出 Chrome 窗口。登录请使用 request_user_login('boss')。"""
    log.info("BOSS直聘登录态失效，请使用 request_user_login('boss') 手动登录")


def legacy_search_boss(keyword: str, city: str = "", limit: int = 10) -> Dict:
    """BOSS直聘搜索入口函数，注册到平台适配器系统。"""
    log.info(f"search_boss: {keyword}, city={city}, limit={limit}")
    limit = min(limit, 20)
    trace = CollectionTrace("BOSS直聘", ["stealth_cdp_page"])

    auth_probe = probe_boss_auth_state(keepalive=True)
    if auth_probe.get("status") != "ok":
        failure_type = str(auth_probe.get("failure_type") or auth_probe.get("auth_state") or "auth_preflight_failed")
        record_search_outcome("boss", "failed", failure_type, keyword=keyword, city=city)
        trace.add("stealth_cdp_page", "failed", detail=failure_type, error_type=failure_type, retryable=True)
        return _format_search_error("BOSS直聘", {
            "error": "BOSS直聘登录态预检未通过，已停止搜索请求",
            "failure_type": failure_type,
            "hint": "请通过统一浏览器人工交互入口完成 BOSS 登录或安全验证后重试。",
            "user_action_required": bool(auth_probe.get("manual_action_required")),
            "manual_action_required": bool(auth_probe.get("manual_action_required")),
            "platform_state": str(auth_probe.get("auth_state") or "auth_preflight_failed"),
            "recommended_action": auth_probe.get("recommended_action") or "health_check(mode='probe_browser_auth:boss')",
            "auth_probe": auth_probe,
        }, trace=trace, strategy="stealth_cdp_page")

    # 策略门禁检查
    gate = check_search_gate("boss", keyword=keyword, city=city)
    if not gate["allowed"]:
        log.warning(f"BOSS直聘搜索被门禁拦截: {gate['reason']}")
        return _format_search_error("BOSS直聘", {
            "error": f"搜索被策略门禁拦截: {gate['reason']}",
            "gate_status": gate,
        }, trace=trace, strategy="stealth_cdp_page")

    # 确保 Chrome 调试模式就绪（含 stealth.js 注入）
    try:
        _ensure_chrome_debugging("boss")
    except RuntimeError as e:
        log.error(f"BOSS直聘 Chrome 启动失败: {e}")
        record_search_outcome("boss", "failed", f"Chrome启动失败: {e}", keyword=keyword, city=city)
        return _format_search_error("BOSS直聘", {
            "error": f"Chrome 启动失败: {e}",
            "hint": "请确认 Chrome 已安装",
        }, trace=trace, strategy="stealth_cdp_page")

    try:
        try:
            search_state = boss_search_via_cdp_state(keyword, city, limit)
            items = list(search_state.get("items") or [])
        except BossCdpError as e:
            record_search_outcome("boss", "failed", e.failure_type, keyword=keyword, city=city)
            trace.add("stealth_cdp_page", "failed", detail=str(e), error_type=e.failure_type, retryable=e.retryable)
            return _format_search_error("BOSS直聘", {
                "error": f"BOSS直聘 CDP 搜索失败: {e}",
                "failure_type": e.failure_type,
                "hint": "这是采集链路故障，不代表账号状态失效。请检查 Chrome 调试端口、WebSocket Origin 或依赖环境。",
                "user_action_required": False,
                "platform_state": "collector_error",
            }, trace=trace, strategy="stealth_cdp_page")

        if items:
            record_search_outcome("boss", "ok", keyword=keyword, city=city)
            trace.add("stealth_cdp_page", "ok", item_count=len(items))
            return _format_search_response("BOSS直聘", items, trace=trace)

        if str(search_state.get("status") or "") == "needs_interaction":
            failure_type = str(search_state.get("failure_type") or "manual_action_required")
            record_search_outcome("boss", "failed", failure_type, keyword=keyword, city=city)
            trace.add("stealth_cdp_page", "failed", detail=failure_type, error_type=failure_type, retryable=True)
            return _format_search_error("BOSS直聘", {
                "error": "BOSS直聘页面需要登录或安全验证",
                "failure_type": failure_type,
                "hint": "请通过统一浏览器人工交互入口完成 BOSS 登录或安全验证后重试。",
                "user_action_required": True,
                "manual_action_required": True,
                "platform_state": str(search_state.get("platform_state") or "manual_action_required"),
                "recommended_action": "health_check(mode='request_browser_interaction:boss:login_or_security_verification')",
                "diagnostic_evidence": [
                    f"title={search_state.get('title') or ''}",
                    f"url={search_state.get('url') or ''}",
                ],
            }, trace=trace, strategy="stealth_cdp_page")

        empty_reason = str(search_state.get("failure_type") or "empty_results")
        record_search_outcome("boss", "failed", empty_reason, keyword=keyword, city=city)
        trace.add("stealth_cdp_page", "failed", detail=empty_reason, error_type=empty_reason, retryable=True)

        return _format_search_error("BOSS直聘", {
            "error": "BOSS直聘搜索无结果",
            "hint": "页面可访问但未解析到职位卡片；可能是关键词无结果、选择器变化或页面加载异常。未自动弹出浏览器。",
            "failure_type": empty_reason,
            "user_action_required": False,
            "manual_action_required": False,
            "platform_state": str(search_state.get("platform_state") or empty_reason),
            "diagnostics": search_state.get("diagnostics"),
            "recommended_action": "inspect_boss_page_state",
        }, trace=trace, strategy="stealth_cdp_page")
    finally:
        finish_chrome_automation("boss", reason="boss_search")
