"""脉脉采集器 - 通过 CDP 控制 Chrome 搜索脉脉职位。

注意：脉脉有 WAF 防护，需要登录才能使用搜索功能。
"""

from __future__ import annotations

import json
import logging
import subprocess
from runtime.process import silent_subprocess_run
from typing import Dict, List

from kr_core.collection import CollectionTrace, format_search_error, format_search_response
from runtime.chrome_manager import MAIMAI_CHROME_DEBUG_PORT, _ensure_chrome_debugging, finish_chrome_automation
from runtime.page_access_state import classify_page_access_state, merge_page_state
from runtime.recruitment_governance import check_search_gate, record_search_outcome

log = logging.getLogger("mcp-server")


def _format_search_response(platform: str, items: List[Dict], *, trace: CollectionTrace | None = None) -> Dict:
    return format_search_response(platform, items, trace=trace)


def _format_search_error(platform: str, error_item: Dict, *, trace: CollectionTrace | None = None, strategy: str = "") -> Dict:
    return format_search_error(platform, error_item, trace=trace, strategy=strategy)


def maimai_search_via_cdp_state(keyword: str, limit: int = 10) -> Dict:
    """通过 CDP 控制 Chrome 搜索脉脉职位，并保留页面状态。"""
    port = int(MAIMAI_CHROME_DEBUG_PORT)

    js_code = r"""
    (async () => {
      const port = process.argv[1];
      const keyword = process.argv[2];
      const limit = Number(process.argv[3] || 10);
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
      let page = targets.find(t => t.type === 'page' && (t.url || '').includes('maimai.cn'))
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

      // 导航到搜索页面
      const searchUrl = `https://maimai.cn/web/gear/test/job/search?query=${encodeURIComponent(keyword)}`;
      await send('Page.navigate', { url: searchUrl }, sessionId);
      await sleep(8000);

      const expression = `
        (() => {
          const items = [];
          const text = document.body ? document.body.innerText || '' : '';

          const needLogin = /登录|扫码/i.test(text) && !text.includes('职位');
          const isBlocked = /访问被拦截|418|WAF/i.test(text);

          // 脉脉职位卡片选择器（需要根据实际页面调整）
          const cards = document.querySelectorAll('[class*="job-card"], [class*="position-card"], .job-list-item');

          for (const card of cards) {
            try {
              const titleEl = card.querySelector('[class*="title"], [class*="name"], h3, h4');
              const title = titleEl ? titleEl.textContent.trim() : '';

              const salaryEl = card.querySelector('[class*="salary"], [class*="薪资"]');
              const salary = salaryEl ? salaryEl.textContent.trim() : '';

              const companyEl = card.querySelector('[class*="company"], [class*="企业"]');
              const company = companyEl ? companyEl.textContent.trim() : '';

              const areaEl = card.querySelector('[class*="area"], [class*="location"], [class*="地点"]');
              const area = areaEl ? areaEl.textContent.trim() : '';

              if (title) {
                items.push({ title, salary, company, area, platform: '脉脉' });
              }
            } catch (e) {}
            if (items.length >= limit) break;
          }

          return JSON.stringify({
            items,
            needLogin,
            blocked: isBlocked,
            url: location.href,
            title: document.title,
            textSample: text.slice(0, 500)
          });
        })()
      `;

      const evaluated = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, sessionId);
      const raw = evaluated.result && evaluated.result.result ? evaluated.result.result.value : '{"items":[]}';
      console.log(raw);
      ws.close();
    })().catch(error => {
      console.error(error && error.stack ? error.stack : String(error));
      process.exit(1);
    });
    """

    try:
        proc = silent_subprocess_run(
            ["node", "-e", js_code, str(port), keyword, str(limit)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        if proc.returncode != 0:
            log.debug(f"脉脉 CDP 搜索失败: {proc.stderr.strip()}")
            return {"items": [], "status": "failed", "failure_type": "cdp_runtime_error", "error": proc.stderr.strip()}

        data = json.loads(proc.stdout.strip() or "{}")

        items = data.get("items", [])
        text = str(data.get("textSample") or "")
        classified = classify_page_access_state(
            platform="maimai",
            operation="search",
            blocked_marker=bool(data.get("blocked")),
            login_marker=bool(data.get("needLogin")),
            rate_limit_marker="访问频繁" in text or "稍后再试" in text,
            result_item_count=len(items),
            extra_signals={
                "title": data.get("title") or "",
                "url": data.get("url") or "",
            },
        )
        data = merge_page_state(data, classified)

        if items:
            log.info(f"脉脉 CDP 搜索返回 {len(items)} 条结果")
            return {**data, "items": items[:limit], "status": "ok", "failure_type": ""}

        if data.get("status") == "needs_interaction":
            if data.get("failure_type") == "platform_verification_required":
                log.warning("脉脉 WAF 拦截")
            elif data.get("failure_type") == "login_required":
                log.info("脉脉需要登录")
        return {**data, "items": []}
    except subprocess.TimeoutExpired:
        log.debug("脉脉 CDP 搜索超时")
        return {"items": [], "status": "failed", "failure_type": "network_timeout"}
    except Exception as e:
        log.debug(f"脉脉 CDP 搜索异常: {e}")
        return {"items": [], "status": "failed", "failure_type": "cdp_runtime_error", "error": str(e)}


def maimai_search_via_cdp(keyword: str, limit: int = 10) -> List[Dict]:
    """Compatibility wrapper returning only items."""
    return list(maimai_search_via_cdp_state(keyword, limit).get("items") or [])


def _bring_maimai_to_front_for_login() -> None:
    """已弃用：不再自动弹出 Chrome 窗口。登录请使用 request_user_login('maimai')。"""
    log.info("脉脉登录态失效，请使用 request_user_login('maimai') 手动登录")


def legacy_search_maimai(keyword: str, limit: int = 10) -> Dict:
    """脉脉搜索入口函数。"""
    log.info(f"search_maimai: {keyword}, limit={limit}")
    limit = min(limit, 20)
    trace = CollectionTrace("脉脉", ["chrome_cdp_page"])

    # 策略门禁检查
    gate = check_search_gate("maimai")
    if not gate["allowed"]:
        log.warning(f"脉脉搜索被门禁拦截: {gate['reason']}")
        return _format_search_error("脉脉", {
            "error": f"搜索被策略门禁拦截: {gate['reason']}",
            "gate_status": gate,
        }, trace=trace, strategy="chrome_cdp_page")

    try:
        _ensure_chrome_debugging("maimai")
    except RuntimeError as e:
        log.error(f"脉脉 Chrome 启动失败: {e}")
        record_search_outcome("maimai", "failed", f"Chrome启动失败: {e}")
        return _format_search_error("脉脉", {"error": f"Chrome 启动失败: {e}"}, trace=trace, strategy="chrome_cdp_page")

    try:
        search_state = maimai_search_via_cdp_state(keyword, limit)
        items = list(search_state.get("items") or [])

        if items:
            record_search_outcome("maimai", "ok")
            trace.add("chrome_cdp_page", "ok", item_count=len(items))
            return _format_search_response("脉脉", items, trace=trace)

        if str(search_state.get("status") or "") == "needs_interaction":
            failure_type = str(search_state.get("failure_type") or "manual_action_required")
            record_search_outcome("maimai", "failed", failure_type)
            trace.add("chrome_cdp_page", "failed", detail=failure_type, error_type=failure_type, retryable=True)
            return _format_search_error("脉脉", {
                "error": "脉脉页面需要登录或安全验证",
                "hint": "请通过统一浏览器人工交互入口完成脉脉登录或安全验证后重试。",
                "failure_type": failure_type,
                "user_action_required": True,
                "manual_action_required": True,
                "platform_state": str(search_state.get("platform_state") or "manual_action_required"),
                "recommended_action": "health_check(mode='request_browser_interaction:maimai:login_or_security_verification')",
            }, trace=trace, strategy="chrome_cdp_page")

        record_search_outcome("maimai", "failed", "empty_results")
        trace.add("chrome_cdp_page", "failed", detail="empty_results", error_type="empty_results", retryable=True)

        return _format_search_error("脉脉", {
            "error": "脉脉搜索无结果",
            "hint": "未自动弹出浏览器。脉脉网页版搜索已降级为联网搜索兜底；如需浏览器验证，请走统一人工交互入口。",
            "user_action_required": False,
            "manual_action_required": False,
            "platform_state": "provider_unavailable",
            "recommended_action": "use_web_search_fallback",
        }, trace=trace, strategy="chrome_cdp_page")
    finally:
        finish_chrome_automation("maimai", reason="maimai_search")
