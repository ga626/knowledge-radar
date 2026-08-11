"""智联招聘采集器 - 通过 CDP 控制 Chrome 搜索智联招聘职位。

注意：智联招聘有反爬机制，需要登录才能使用搜索功能。
当前使用 Chrome CDP 方式，后续可扩展为 Playwright 方式。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from urllib.parse import urlencode
from runtime.process import silent_subprocess_run
from typing import Dict, List

from kr_core.collection import CollectionTrace, format_search_error, format_search_response
from runtime.chrome_manager import ZHILIAN_CHROME_DEBUG_PORT, _ensure_chrome_debugging, finish_chrome_automation
from runtime.page_access_state import classify_page_access_state, merge_page_state
from runtime.recruitment_city import resolve_recruitment_city

log = logging.getLogger("mcp-server")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SELECTORS_PATH = os.path.join(PROJECT_ROOT, "config", "selectors.json")

def _load_selectors(platform: str = "zhilian") -> Dict:
    try:
        with open(SELECTORS_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
        return config.get("platforms", {}).get(platform, {})
    except Exception:
        return {}


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


def _zhilian_city_param(city: str) -> str:
    return str(resolve_recruitment_city("zhilian", city).get("param_value") or "")


def _build_zhilian_search_url(keyword: str, city: str = "", *, city_mode: str = "auto") -> str:
    mode = str(city_mode or "auto").strip().lower()
    city_value = str(city or "").strip()
    if mode in {"auto", "id", "code"}:
        city_value = _zhilian_city_param(city)
    params = {"kw": keyword or ""}
    if city_value:
        params["jl"] = city_value
    return "https://sou.zhaopin.com/?" + urlencode(params)


def _looks_like_selector_miss(data: Dict) -> bool:
    sample = str(data.get("textSample") or "")
    if any(token in sample for token in ("职位", "公司", "薪资", "招聘")):
        return True
    page_hint = " ".join(str(data.get(key) or "") for key in ("url", "title")).lower()
    return bool(sample.strip()) and ("zhaopin.com" in page_hint or "zhilian" in page_hint or "智联" in page_hint)


def _format_search_response(platform: str, items: List[Dict], *, trace: CollectionTrace | None = None) -> Dict:
    return format_search_response(platform, items, trace=trace)


def _format_search_error(
    platform: str,
    error_item: Dict,
    *,
    trace: CollectionTrace | None = None,
    strategy: str = "",
    metadata: Dict | None = None,
) -> Dict:
    return format_search_error(platform, error_item, trace=trace, strategy=strategy, metadata=metadata)


def zhilian_search_via_cdp_state(keyword: str, city: str = "", limit: int = 10) -> Dict:
    """通过 CDP 控制 Chrome 搜索智联招聘职位，并保留页面状态。"""
    port = int(ZHILIAN_CHROME_DEBUG_PORT)
    city_mode = os.environ.get("KR_ZHILIAN_CITY_MODE", "auto")
    city_resolution = resolve_recruitment_city("zhilian", city)
    if city and city_resolution.get("status") == "missing" and str(city_mode).lower() in {"auto", "id", "code"}:
        return {
            "items": [],
            "status": "failed",
            "failure_type": "city_mapping_missing",
            "platform_state": "city_mapping_missing",
            "manual_action_required": False,
            "diagnostics": {
                "schema": "knowledgeradar-recruitment-diagnostics/v1",
                "platform": "zhilian",
                "layers": {
                    "connection": {"status": "not_run"},
                    "navigation": {"status": "not_run"},
                    "params": {"status": "city_mapping_missing", "keyword": keyword, "city": city, "city_resolution": city_resolution},
                    "parser": {"status": "not_run"},
                    "evidence": {"status": "none"},
                },
            },
        }
    search_url = _build_zhilian_search_url(keyword, city, city_mode=city_mode)
    try:
        _ensure_chrome_debugging("zhilian")
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
                "platform": "zhilian",
                "layers": {
                    "connection": {"status": "failed", "failure_type": "cdp_unavailable", "port": port},
                    "navigation": {"status": "not_run"},
                    "params": {"status": "not_run", "keyword": keyword, "city": city, "city_param": city_resolution.get("param_value") or "", "city_resolution": city_resolution},
                    "parser": {"status": "not_run"},
                    "evidence": {"status": "none"},
                },
            },
        }

    card_selectors = _selector_values("zhilian", "card_selector", [".joblist-box__item", '[class*="joblist"] [class*="item"]', '[class*="job-card"]'])
    title_selectors = _selector_values("zhilian", "title", [".jobinfo__name", '[class*="jobinfo__name"]', 'a[href*="/jobs/"]', 'a[href*="/job/"]'])
    salary_selectors = _selector_values("zhilian", "salary", [".jobinfo__salary", '[class*="salary"]'])
    company_selectors = _selector_values("zhilian", "company", [".companyinfo__name", "[class*='companyinfo__name']", '[class*="company"]'])
    area_selectors = _selector_values("zhilian", "area", [".jobinfo__other-info-item:first-child", '[class*="other-info"] span', '[class*="area"]'])
    link_selectors = _selector_values("zhilian", "link", [".jobinfo__name", 'a[href*="/jobs/"]', 'a[href*="/job/"]'])

    js_code = r"""
    (async () => {
      const port = process.argv[1];
      const searchUrl = process.argv[2];
      const limit = Number(process.argv[3] || 10);
      const selectors = JSON.parse(process.argv[4] || '{}');
      const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

      const version = await fetch(`http://127.0.0.1:${port}/json/version`).then(r => r.json());
      const ws = new WebSocket(version.webSocketDebuggerUrl);
      await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });

      let seq = 0;
      const pending = new Map();
      ws.onmessage = event => {
        const message = JSON.parse(event.data);
        if (message.id && pending.has(message.id)) {
          pending.get(message.id)(message);
          pending.delete(message.id);
        }
      };
      const send = (method, params, sessionId) => new Promise(resolve => {
        const id = ++seq;
        pending.set(id, resolve);
        const message = { id, method, params };
        if (sessionId) message.sessionId = sessionId;
        ws.send(JSON.stringify(message));
      });

      let targets = await fetch(`http://127.0.0.1:${port}/json/list`).then(r => r.json());
      let page = targets.find(t => t.type === 'page' && (t.url || '').includes('zhaopin.com'))
        || targets.find(t => t.type === 'page')
        || targets[0];

      if (!page) {
        console.log(JSON.stringify({ items: [], error: "no_page_target" }));
        ws.close();
        return;
      }

      const attached = await send('Target.attachToTarget', { targetId: page.id, flatten: true });
      const sessionId = attached.result.sessionId;
      await send('Page.enable', {}, sessionId);
      await send('Runtime.enable', {}, sessionId);

      await send('Page.navigate', { url: searchUrl }, sessionId);
      const waitExpression = `
        (() => {
          const text = document.body ? document.body.innerText || '' : '';
          const cardSelectors = ${JSON.stringify(selectors.card || [])};
          const cardCount = cardSelectors.reduce((sum, sel) => sum + document.querySelectorAll(sel).length, 0);
          const isBlocked = /安全验证|滑动验证|captcha/i.test(text);
          const needLogin = /登录|扫码登录|请先登录|验证码登录/i.test(text) && !/职位|公司|薪资|招聘/.test(text);
          const rateLimited = /访问频繁|请求频繁|稍后再试|429/i.test(text);
          const empty = /暂无职位|没有找到|无搜索结果|换个关键词/.test(text);
          return JSON.stringify({
            ready: cardCount > 0 || isBlocked || needLogin || rateLimited || empty || document.readyState === 'complete',
            cardCount,
            isBlocked,
            needLogin,
            rateLimited,
            empty,
            readyState: document.readyState,
            url: location.href,
            title: document.title
          });
        })()
      `;
      let waitState = {};
      const waitStarted = Date.now();
      const waitTimeoutMs = Number(process.env.KR_ZHILIAN_READY_TIMEOUT_MS || 12000);
      while (Date.now() - waitStarted < waitTimeoutMs) {
        const probe = await send('Runtime.evaluate', { expression: waitExpression, returnByValue: true, awaitPromise: true }, sessionId);
        try { waitState = JSON.parse(probe.result && probe.result.result ? probe.result.result.value : '{}'); } catch (e) { waitState = {}; }
        if (waitState.ready) break;
        await sleep(400);
      }
      waitState.elapsedMs = Date.now() - waitStarted;

      const expression = `
        (() => {
          const maxItems = ${limit};
          const items = [];
          const text = document.body ? document.body.innerText || '' : '';

          const isBlocked = /安全验证|滑动验证|captcha/i.test(text);
          const needLogin = /登录|扫码登录|请先登录|验证码登录/i.test(text) && !/职位|公司|薪资|招聘/.test(text);
          const rateLimited = /访问频繁|请求频繁|稍后再试|429/i.test(text);

          const pickText = (root, sels) => {
            for (const sel of sels) {
              const el = root.querySelector(sel);
              const value = el && (el.innerText || el.textContent || '').trim();
              if (value) return value.replace(/\\s+/g, ' ');
            }
            return '';
          };
          const pickEl = (root, sels) => {
            for (const sel of sels) {
              const el = root.querySelector(sel);
              if (el) return el;
            }
            return null;
          };
          const selectorPayload = ${JSON.stringify(selectors)};
          const cards = Array.from(new Set((selectorPayload.card || []).flatMap(sel => Array.from(document.querySelectorAll(sel)))));

          for (const card of cards) {
            try {
              const title = pickText(card, selectorPayload.title || []);
              const salary = pickText(card, selectorPayload.salary || []);
              const company = pickText(card, selectorPayload.company || []);
              const area = pickText(card, selectorPayload.area || []);
              const linkEl = pickEl(card, selectorPayload.link || []) || card.querySelector('a[href]');
              const href = linkEl ? linkEl.href || linkEl.getAttribute('href') || '' : '';

              if (title) {
                items.push({ title, salary, company, area, url: href, platform: '智联招聘' });
              }
            } catch (e) {}
            if (items.length >= maxItems) break;
          }

          return JSON.stringify({
            items,
            needLogin,
            blocked: isBlocked,
            rateLimited,
            url: location.href,
            title: document.title,
            cardCount: cards.length,
            waitState: ${JSON.stringify(waitState)},
            textSample: text.slice(0, 500)
          });
        })()
      `;

      const evaluated = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, sessionId);
      const exceptionDetails = evaluated.exceptionDetails || (evaluated.result && evaluated.result.exceptionDetails);
      if (exceptionDetails) {
        console.log(JSON.stringify({
          items: [],
          error: 'runtime_evaluate_exception',
          exceptionText: exceptionDetails.text || '',
          exceptionDescription: exceptionDetails.exception ? exceptionDetails.exception.description || '' : '',
          url: searchUrl
        }));
      } else {
        const raw = evaluated.result && evaluated.result.result ? evaluated.result.result.value : '';
        console.log(typeof raw === 'string' && raw ? raw : JSON.stringify({ items: [], error: 'runtime_evaluate_no_value', url: searchUrl }));
      }
      ws.close();
    })().catch(error => {
      console.error(error && error.stack ? error.stack : String(error));
      process.exit(1);
    });
    """

    try:
        selectors_payload = json.dumps(
            {
                "card": card_selectors,
                "title": title_selectors,
                "salary": salary_selectors,
                "company": company_selectors,
                "area": area_selectors,
                "link": link_selectors,
            },
            ensure_ascii=False,
        )
        proc = silent_subprocess_run(
            ["node", "-e", js_code, str(port), search_url, str(limit), selectors_payload],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        if proc.returncode != 0:
            log.debug(f"智联招聘 CDP 搜索失败: {proc.stderr.strip()}")
            stderr = proc.stderr.strip()
            lowered = stderr.lower()
            failure_type = "cdp_unavailable" if any(token in lowered for token in ("econnrefused", "failed to fetch", "connect", "127.0.0.1")) else "cdp_runtime_error"
            return {"items": [], "status": "failed", "failure_type": failure_type, "platform_state": failure_type, "error": stderr}

        raw_stdout = proc.stdout.strip()
        if not raw_stdout:
            return {
                "items": [],
                "status": "failed",
                "failure_type": "cdp_no_output",
                "platform_state": "cdp_no_output",
                "manual_action_required": False,
                "error": (proc.stderr or "").strip(),
                "diagnostics": {
                    "schema": "knowledgeradar-recruitment-diagnostics/v1",
                    "platform": "zhilian",
                    "layers": {
                        "connection": {"status": "failed", "failure_type": "cdp_no_output", "port": port},
                        "navigation": {"status": "unknown", "url": search_url},
                        "params": {"status": "ok", "keyword": keyword, "city": city, "city_param": city_resolution.get("param_value") or "", "city_mode": city_mode, "city_resolution": city_resolution},
                        "parser": {"status": "not_run"},
                        "evidence": {"status": "none"},
                    },
                },
            }
        try:
            data = json.loads(raw_stdout)
        except json.JSONDecodeError as exc:
            return {
                "items": [],
                "status": "failed",
                "failure_type": "cdp_output_parse_error",
                "platform_state": "cdp_output_parse_error",
                "manual_action_required": False,
                "error": str(exc),
                "raw_stdout_sample": raw_stdout[:500],
            }
        items = data.get("items", [])
        diagnostics = {
            "schema": "knowledgeradar-recruitment-diagnostics/v1",
            "platform": "zhilian",
            "layers": {
                "connection": {"status": "ok", "port": port},
                "navigation": {"status": "ok", "url": data.get("url") or search_url, "title": data.get("title") or ""},
                "params": {
                    "status": "ok" if city_resolution.get("status") != "missing" else "city_mapping_missing",
                    "keyword": keyword,
                    "city": city,
                    "city_param": city_resolution.get("param_value") or "",
                    "city_mode": city_mode,
                    "city_resolution": city_resolution,
                },
                "parser": {"status": "ok" if items else ("selector_miss" if _looks_like_selector_miss(data) else "empty"), "card_count": data.get("cardCount", 0)},
                "evidence": {"status": "search_cards" if items else "none"},
            },
            "wait_state": data.get("waitState") or {},
            "selectors": {
                "card": card_selectors,
                "title": title_selectors,
                "salary": salary_selectors,
                "company": company_selectors,
                "area": area_selectors,
                "link": link_selectors,
            },
        }
        data["diagnostics"] = diagnostics
        classified = classify_page_access_state(
            platform="zhilian",
            operation="search",
            blocked_marker=bool(data.get("blocked")),
            login_marker=bool(data.get("needLogin")),
            rate_limit_marker=bool(data.get("rateLimited")),
            result_item_count=len(items),
            extra_signals={
                "title": data.get("title") or "",
                "url": data.get("url") or "",
            },
        )
        data = merge_page_state(data, classified)
        runtime_error = str(data.get("error") or "")
        if runtime_error in {"runtime_evaluate_exception", "runtime_evaluate_no_value"}:
            data["status"] = "failed"
            data["failure_type"] = runtime_error
            data["platform_state"] = runtime_error
            data["manual_action_required"] = False
            data["diagnostics"]["layers"]["parser"]["status"] = runtime_error
            data["diagnostics"]["layers"]["evidence"]["status"] = "none"
            return {**data, "items": []}

        if items:
            log.info(f"智联招聘 CDP 搜索返回 {len(items)} 条结果")
            return {**data, "items": items[:limit], "status": "ok", "failure_type": ""}

        if data.get("status") == "needs_interaction":
            log.warning("智联招聘页面需要登录或安全验证")
        if not data.get("failure_type") or data.get("failure_type") == "empty_results":
            data["failure_type"] = "selector_miss" if _looks_like_selector_miss(data) else "empty_results"
            data["platform_state"] = data["failure_type"]
        return {**data, "items": []}
    except subprocess.TimeoutExpired:
        log.debug("智联招聘 CDP 搜索超时")
        return {"items": [], "status": "failed", "failure_type": "network_timeout", "platform_state": "network_timeout"}
    except Exception as e:
        log.debug(f"智联招聘 CDP 搜索异常: {e}")
        return {"items": [], "status": "failed", "failure_type": "cdp_runtime_error", "platform_state": "cdp_runtime_error", "error": str(e)}
    finally:
        finish_chrome_automation("zhilian", reason="zhilian_search")


def zhilian_search_via_cdp(keyword: str, city: str = "", limit: int = 10) -> List[Dict]:
    """Compatibility wrapper returning only items."""
    return list(zhilian_search_via_cdp_state(keyword, city, limit).get("items") or [])


def legacy_search_zhilian(keyword: str, city: str = "", limit: int = 10) -> Dict:
    """智联招聘搜索入口函数。"""
    log.info(f"search_zhilian: {keyword}, city={city}, limit={limit}")
    limit = min(limit, 20)
    trace = CollectionTrace("智联招聘", ["chrome_cdp_page"])

    search_state = zhilian_search_via_cdp_state(keyword, city, limit)
    items = list(search_state.get("items") or [])

    if items:
        trace.add("chrome_cdp_page", "ok", item_count=len(items))
        return _format_search_response("智联招聘", items, trace=trace)

    if str(search_state.get("status") or "") == "needs_interaction":
        failure_type = str(search_state.get("failure_type") or "manual_action_required")
        trace.add("chrome_cdp_page", "failed", detail=failure_type, error_type=failure_type, retryable=True)
        return _format_search_error("智联招聘", {
            "error": "智联招聘页面需要登录或安全验证",
            "hint": "请通过统一浏览器人工交互入口完成智联登录或安全验证后重试。",
            "failure_type": failure_type,
            "user_action_required": True,
            "manual_action_required": True,
            "platform_state": str(search_state.get("platform_state") or "manual_action_required"),
            "recommended_action": "health_check(mode='request_browser_interaction:zhilian:login_or_security_verification')",
        }, trace=trace, strategy="chrome_cdp_page")

    if str(search_state.get("status") or "") == "retry_later":
        trace.add("chrome_cdp_page", "failed", detail="rate_limited", error_type="rate_limited", retryable=True)
        return _format_search_error("智联招聘", {
            "error": "智联招聘访问频繁，请稍后重试",
            "failure_type": "rate_limited",
            "manual_action_required": False,
            "platform_state": "rate_limited",
        }, trace=trace, strategy="chrome_cdp_page")

    failure_type = str(search_state.get("failure_type") or "empty_results")
    trace.add("chrome_cdp_page", "failed", detail=failure_type, error_type=failure_type, retryable=True)

    return _format_search_error("智联招聘", {
        "error": "智联招聘搜索未得到可采信职位卡片",
        "hint": "智联当前为 experimental_browser_source；CDP、选择器或页面结构异常不能支撑市场无岗位结论。",
        "failure_type": failure_type,
        "manual_action_required": False,
        "platform_state": str(search_state.get("platform_state") or "empty_results"),
        "diagnostics": search_state.get("diagnostics") or {},
    }, trace=trace, strategy="experimental_browser_source", metadata={"collector_status": "experimental_browser_source", "diagnostics": search_state.get("diagnostics") or {}})
