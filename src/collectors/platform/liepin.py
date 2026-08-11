"""猎聘采集器 - 通过 CDP 控制 Chrome 搜索猎聘职位。

复用 KnowledgeRadar 的 Chrome 管理机制，端口 9338。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from runtime.process import silent_subprocess_run
from urllib.parse import quote, unquote
from typing import Dict, List, Optional

from generic_web import GenericWebRequest, collect_url
from kr_core.collection import CollectionTrace, format_search_error, format_search_response
from runtime.chrome_manager import LIEPIN_CHROME_DEBUG_PORT, _ensure_chrome_debugging, finish_chrome_automation
from runtime.page_access_state import classify_page_access_state, merge_page_state
from runtime.recruitment_city import resolve_recruitment_city
from runtime.recruitment_governance import check_search_gate, record_search_outcome
from runtime.recruitment_network import extract_recruitment_items_from_payloads, recruitment_network_observer_script

log = logging.getLogger("mcp-server")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SELECTORS_PATH = os.path.join(PROJECT_ROOT, "config", "selectors.json")

REMOTE_CITY_MARKERS = ("远程", "全国", "不限", "居家", "remote")
CITY_MARKERS = (
    "北京", "上海", "广州", "深圳", "杭州", "南京", "苏州", "成都", "重庆", "武汉", "西安", "天津",
    "合肥", "长沙", "郑州", "青岛", "济南", "厦门", "福州", "宁波", "无锡", "常州", "佛山", "东莞",
    "珠海", "中山", "南昌", "南宁", "昆明", "贵阳", "海口", "三亚", "沈阳", "大连", "长春", "哈尔滨",
    "石家庄", "太原", "呼和浩特", "兰州", "银川", "西宁", "乌鲁木齐", "拉萨",
)


def _load_selectors(platform: str = "liepin") -> Dict:
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


def _format_search_response(platform: str, items: List[Dict], *, trace: CollectionTrace | None = None) -> Dict:
    return format_search_response(platform, items, trace=trace)


def _format_search_error(platform: str, error_item: Dict, *, trace: CollectionTrace | None = None, strategy: str = "") -> Dict:
    return format_search_error(platform, error_item, trace=trace, strategy=strategy)


def _city_match_label(area: str, requested_city: str, extra_text: str = "") -> str:
    city = str(requested_city or "").strip()
    text = " ".join(part for part in (str(area or "").strip(), str(extra_text or "").strip()) if part)
    if not city:
        return "not_requested"
    if not text:
        return "unknown"
    lowered = text.lower()
    if any(marker.lower() in lowered for marker in REMOTE_CITY_MARKERS):
        return "remote"
    if city in text or text in city:
        return "match"
    if any(marker in text for marker in CITY_MARKERS if marker != city):
        return "mismatch"
    return "unknown"


def _apply_city_filter(items: List[Dict], requested_city: str) -> tuple[List[Dict], Dict]:
    if not requested_city:
        return items, {
            "requested_city": "",
            "kept_count": len(items),
            "dropped_count": 0,
            "mismatch_count": 0,
        }
    kept: List[Dict] = []
    dropped = 0
    mismatch = 0
    for item in items:
        area = str(item.get("area") or item.get("location") or "")
        extra_text = " ".join(
            str(item.get(key) or "")
            for key in ("title", "salary", "company", "url")
        )
        label = _city_match_label(area, requested_city, extra_text=extra_text)
        item["requested_city"] = requested_city
        item["city_match"] = label
        if label == "mismatch":
            dropped += 1
            mismatch += 1
            continue
        kept.append(item)
    return kept, {
        "requested_city": requested_city,
        "kept_count": len(kept),
        "dropped_count": dropped,
        "mismatch_count": mismatch,
    }


def _finalize_liepin_search_state(data: Dict, city: str = "", limit: int = 15) -> Dict:
    raw_items = list(data.get("items", []) or [])
    items, city_filter = _apply_city_filter(raw_items, city)
    data["city_filter"] = city_filter

    page_state = dict(data.get("page_state") or {})
    blocked_marker = bool(data.get("blocked")) or bool(page_state.get("blocked_marker"))
    login_marker = bool(data.get("loginRequired")) or bool(page_state.get("login_marker"))
    card_count = int(data.get("cardCount") or page_state.get("card_count") or 0)
    job_link_count = int(data.get("jobLinkCount") or page_state.get("job_link_count") or 0)
    captcha_element_count = int(data.get("captchaElementCount") or page_state.get("captcha_element_count") or 0)
    blocking_modal_count = int(data.get("blockingModalCount") or page_state.get("blocking_modal_count") or 0)
    login_modal_count = int(data.get("loginModalCount") or page_state.get("login_modal_count") or 0)
    url_signal = str(data.get("urlSignal") or page_state.get("url_signal") or "")
    wait_state = data.get("waitState") if isinstance(data.get("waitState"), dict) else {}
    keyword = str(data.get("keyword") or "")
    if keyword:
        query_reflected = keyword in "\n".join(
            [
                unquote(str(data.get("url") or "")),
                str(data.get("title") or ""),
                str(data.get("textSample") or ""),
            ]
        )
    else:
        query_reflected = None
    security_strength = str(page_state.get("security_evidence_strength") or "")
    login_strength = str(page_state.get("login_evidence_strength") or "")
    if not security_strength:
        security_strength = (
            "strong"
            if captcha_element_count or blocking_modal_count or url_signal == "verification_redirect"
            else "weak" if blocked_marker else "none"
        )
    if not login_strength:
        login_strength = (
            "strong"
            if login_modal_count or url_signal == "login_redirect"
            else "weak" if login_marker else "none"
        )
    route_decision = (
        "network_items"
        if any(str(item.get("source") or "").endswith("_network_search") for item in raw_items)
        else "dom_items" if raw_items else "needs_route_repair"
    )
    classified = classify_page_access_state(
        platform="liepin",
        operation="search",
        blocked_marker=blocked_marker,
        login_marker=login_marker,
        captcha_element_count=captcha_element_count,
        result_item_count=len(raw_items),
        card_count=card_count,
        link_count=job_link_count,
        url_signal=url_signal,
        empty_marker=bool(wait_state.get("empty")),
        structured_list_expected=True,
        query_reflected=query_reflected,
        security_evidence_strength=security_strength,
        login_evidence_strength=login_strength,
        blocking_modal_count=blocking_modal_count,
        login_modal_count=login_modal_count,
        extra_signals={
            "raw_item_count": len(raw_items),
            "kept_item_count": len(items),
            "route_probe": {
                "query_reflected": query_reflected,
                "card_count": card_count,
                "job_link_count": job_link_count,
                "wait_empty": bool(wait_state.get("empty")),
                "route_decision": route_decision,
                "network_item_count": len([item for item in raw_items if str(item.get("source") or "").endswith("_network_search")]),
            },
            **page_state,
        },
    )
    data = merge_page_state(data, classified)

    if items:
        log.info(f"猎聘 CDP 搜索返回 {len(items)} 条结果")
        return {**data, "items": items[:limit], "status": "ok", "failure_type": ""}

    if raw_items and city and city_filter.get("mismatch_count"):
        return {
            **data,
            "items": [],
            "status": "empty",
            "failure_type": "city_mismatch",
            "manual_action_required": False,
        }

    if data.get("status") == "needs_interaction" and data.get("failure_type") == "platform_verification_required":
        log.warning("猎聘安全验证触发")
        return {**data, "items": []}

    if data.get("status") == "needs_interaction" and data.get("failure_type") == "login_required":
        log.info("猎聘需要登录")
        return {**data, "items": []}

    return {**data, "items": []}


def _job_detail_quality(text: str) -> Dict:
    """Score whether extracted text looks like a usable recruitment JD."""
    content = str(text or "").strip()
    positive_tokens = ["职位介绍", "职责描述", "任职要求", "岗位职责", "岗位要求", "工作内容", "职位描述"]
    negative_tokens = ["首页 职位 校园", "登录/注册", "登录获取更匹配职位"]
    positive = [token for token in positive_tokens if token in content]
    negative = [token for token in negative_tokens if token in content[:500]]
    ok = len(content) >= 180 and bool(positive) and len(negative) < 2
    return {
        "ok": ok,
        "chars": len(content),
        "positive_tokens": positive,
        "negative_tokens": negative,
    }


def _static_liepin_detail(url: str) -> Optional[Dict]:
    """Try open-web extraction before browser automation.

    Some Liepin enterprise job pages expose a complete static JD, while
    headhunter-style or personalized pages may not. The quality gate prevents
    navigation chrome from being treated as a successful detail.
    """
    try:
        response = collect_url(GenericWebRequest(url=url, timeout=20.0, use_jina=True)).to_mcp_dict()
    except Exception as exc:
        log.debug(f"猎聘静态详情抽取异常: {exc}")
        return None

    content = str(response.get("content") or "")
    quality = _job_detail_quality(content)
    if not quality["ok"]:
        return None

    return {
        "platform": "猎聘",
        "url": str(response.get("final_url") or url),
        "title": str(response.get("title") or ""),
        "salary": "",
        "jd": content,
        "content": content,
        "status": "ok",
        "source": "liepin_static_web_detail",
        "detail_quality": quality,
        "collector": response.get("collector"),
        "metadata": {
            "static_metadata": response.get("metadata") or {},
        },
    }


def liepin_search_via_cdp_state(keyword: str, city: str = "", limit: int = 15) -> Dict:
    """通过 CDP 控制 Chrome 搜索猎聘职位，并保留页面状态。"""
    port = int(LIEPIN_CHROME_DEBUG_PORT)
    city_resolution = resolve_recruitment_city("liepin", city)
    if city and city_resolution.get("status") == "missing":
        return {
            "items": [],
            "status": "failed",
            "failure_type": "city_mapping_missing",
            "platform_state": "city_mapping_missing",
            "manual_action_required": False,
            "diagnostics": {
                "schema": "knowledgeradar-recruitment-diagnostics/v1",
                "platform": "liepin",
                "layers": {
                    "connection": {"status": "not_run"},
                    "navigation": {"status": "not_run"},
                    "params": {"status": "city_mapping_missing", "keyword": keyword, "city": city, "city_resolution": city_resolution},
                    "parser": {"status": "not_run"},
                    "evidence": {"status": "none"},
                },
            },
        }
    city_value = str(city_resolution.get("param_value") or "")
    city_param = f"&city={quote(city_value, safe='')}&dq={quote(city_value, safe='')}" if city_value else ""
    search_url = f"https://www.liepin.com/zhaopin/?key={quote(str(keyword), safe='')}{city_param}&currentPage=0"
    card_selectors = _selector_values("liepin", "card_selector", [".job-card-pc-container", '[class*="job-card"]'])
    try:
        _ensure_chrome_debugging("liepin")
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
                "platform": "liepin",
                "layers": {
                    "connection": {"status": "failed", "failure_type": "cdp_unavailable", "port": port},
                    "navigation": {"status": "not_run"},
                    "params": {"status": "ok", "keyword": keyword, "city": city, "city_param": city_value, "city_resolution": city_resolution},
                    "parser": {"status": "not_run"},
                    "evidence": {"status": "none"},
                },
            },
        }

    js_code = r"""
    (async () => {
      const port = process.argv[1];
      const searchUrl = process.argv[2];
      const limit = Number(process.argv[3] || 15);
      const selectors = JSON.parse(process.argv[4] || '{}');
      const observerScript = process.argv[5] || '';
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
      let page = targets.find(t => t.type === 'page' && (t.url || '').includes('liepin.com'))
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
      if (observerScript) {
        await send('Page.addScriptToEvaluateOnNewDocument', { source: observerScript }, sessionId);
        await send('Runtime.evaluate', { expression: observerScript, awaitPromise: true }, sessionId);
      }

      await send('Page.navigate', { url: searchUrl }, sessionId);
      const waitExpression = `
        (() => {
          const text = document.body ? document.body.innerText || '' : '';
          const cardSelectors = ${JSON.stringify(selectors.card || [])};
          const cards = Array.from(new Set(cardSelectors.flatMap(sel => Array.from(document.querySelectorAll(sel)))));
          const jobLinkCount = document.querySelectorAll('a[href*="/job/"], a[href*="/a/"]').length;
          const networkEntries = (window.__KR_RECRUITMENT_NETWORK__ && window.__KR_RECRUITMENT_NETWORK__.entries || []);
          const networkCount = networkEntries.length;
          const searchNetworkCount = networkEntries.filter(entry =>
            /com\.liepin\.searchfront4c\.pc-search-job(?:$|[?#])/i.test(String(entry.url || ''))
          ).length;
          const blockedMarker = /安全验证|滑动验证|captcha/i.test(text);
          const loginRequired = /登录\\/注册|登录获取更匹配职位|请先登录|扫码登录/i.test(text)
            && !/职位|公司|薪资|经验|学历/.test(text.slice(0, 1200));
          const empty = /暂无职位|没有找到|无搜索结果|换个关键词/.test(text);
          return JSON.stringify({
            ready: cards.length > 0 || jobLinkCount > 0 || searchNetworkCount > 0 || blockedMarker || loginRequired || empty,
            cardCount: cards.length,
            jobLinkCount,
            networkCount,
            searchNetworkCount,
            blockedMarker,
            loginRequired,
            empty,
            textLength: text.length,
            readyState: document.readyState,
            url: location.href,
            title: document.title
          });
        })()
      `;
      let waitState = {};
      const waitStarted = Date.now();
      const waitTimeoutMs = Number(process.env.KR_LIEPIN_READY_TIMEOUT_MS || 12000);
      while (Date.now() - waitStarted < waitTimeoutMs) {
        const probe = await send('Runtime.evaluate', { expression: waitExpression, returnByValue: true, awaitPromise: true }, sessionId);
        const probeException = probe.exceptionDetails || (probe.result && probe.result.exceptionDetails);
        if (probeException) {
          waitState = {
            ready: false,
            error: 'wait_runtime_evaluate_exception',
            exceptionText: probeException.text || '',
            exceptionDescription: probeException.exception ? probeException.exception.description || '' : ''
          };
        } else {
          try { waitState = JSON.parse(probe.result && probe.result.result ? probe.result.result.value : '{}'); } catch (e) { waitState = {}; }
        }
        const elapsedMs = Date.now() - waitStarted;
        if (!waitState.ready && elapsedMs >= 1600 && Number(waitState.textLength || 0) >= 800 && !waitState.blockedMarker && !waitState.loginRequired) {
          waitState.earlyReadable = true;
        }
        const hasSearchRoute = Number(waitState.searchNetworkCount || 0) > 0;
        const hasDomResults = Number(waitState.cardCount || 0) > 0 || Number(waitState.jobLinkCount || 0) > 0;
        const hasTerminalMarker = Boolean(waitState.blockedMarker || waitState.loginRequired || waitState.empty);
        if (hasSearchRoute || hasDomResults || hasTerminalMarker) {
          waitState.ready = true;
          break;
        }
        await sleep(400);
      }
      waitState.elapsedMs = Date.now() - waitStarted;
      if (!waitState.ready) waitState.waitTimeout = true;

      const expression = String.raw`
        (() => {
          const maxItems = ${limit};
          const items = [];
          const text = document.body ? document.body.innerText || '' : '';

          const blockedMarker = /安全验证|滑动验证|captcha/i.test(text);
          const loginRequired = /登录\/注册|登录获取更匹配职位|请先登录|扫码登录/i.test(text)
            && !/职位|公司|薪资|经验|学历/.test(text.slice(0, 1200));
          const isVisible = el => {
            if (!el || !el.getBoundingClientRect) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style && style.visibility !== 'hidden' && style.display !== 'none'
              && rect.width > 0 && rect.height > 0;
          };
          const visibleText = el => String((el && (el.innerText || el.textContent)) || '');
          const captchaElementCount = Array.from(document.querySelectorAll('iframe, canvas, [class], [id]'))
            .filter(el => {
              const marker = [el.id || '', String(el.className || ''), el.getAttribute('src') || ''].join(' ').toLowerCase();
              return isVisible(el) && /(captcha|geetest|yidun|slider|nc_|verify)/.test(marker);
            }).length;
          const dialogCandidates = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], [class*="modal"], [class*="Modal"], [class*="popup"], [class*="Popup"], [class*="mask"], [class*="Mask"]'))
            .filter(isVisible);
          const blockingModalCount = dialogCandidates
            .filter(el => /安全验证|滑动验证|人机验证|captcha/i.test(visibleText(el)))
            .length;
          const loginModalCount = dialogCandidates
            .filter(el => /扫码登录|登录\/注册|请先登录|立即登录|登录后/i.test(visibleText(el)))
            .length;
          const currentUrl = location.href || '';
          const urlSignal = /passport|login|account\/login/i.test(currentUrl)
            ? 'login_redirect'
            : /(verify|captcha|security|risk|safe-check|sec)/i.test(currentUrl)
              ? 'verification_redirect'
              : '';
          const securityEvidenceStrength = (captchaElementCount || blockingModalCount || urlSignal === 'verification_redirect')
            ? 'strong'
            : blockedMarker ? 'weak' : 'none';
          const loginEvidenceStrength = (loginModalCount || urlSignal === 'login_redirect')
            ? 'strong'
            : loginRequired ? 'weak' : 'none';

          const selectorPayload = ${JSON.stringify(selectors)};
          const cards = Array.from(new Set((selectorPayload.card || []).flatMap(sel => Array.from(document.querySelectorAll(sel)))))
            .map(el => el.closest('.job-card-pc-container, [class*="job-card"]') || el)
            .filter((el, idx, arr) => el && arr.indexOf(el) === idx);
          const jobLinkCount = document.querySelectorAll('a[href*="/job/"], a[href*="/a/"]').length;

          const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
          const splitLines = value => String(value || '')
            .split('\n')
            .map(line => clean(line))
            .filter(Boolean);
          const salaryRe = /(\d+(?:\.\d+)?\s*[-~]\s*\d+(?:\.\d+)?\s*[Kk万]?(?:·\d+薪)?|\d+(?:\.\d+)?\s*[Kk万]\s*(?:以上|以下)?(?:·\d+薪)?|薪资面议|面议)/;

          for (const card of cards) {
            try {
              const lines = splitLines(card.innerText);
              const link = card.querySelector('a[href*="/job/"], a[href*="/a/"]') || card.querySelector('a[href]');
              const href = link ? link.href || '' : '';
              const linkText = link ? link.innerText || '' : '';
              const linkLines = splitLines(linkText);
              const title = clean(linkLines[0] || lines[0] || '');
              const bracketArea = linkText.match(/[【\\[]\\s*([^】\\]\\n]{1,30})\\s*[】\\]]/);
              let area = clean(bracketArea ? bracketArea[1] : '');
              if (!area) {
                const leftBracket = lines.findIndex(line => line === '【' || line === '[');
                if (leftBracket >= 0 && lines[leftBracket + 1]) {
                  area = clean(lines[leftBracket + 1].replace(/[【】\[\]]/g, ''));
                }
              }
              if (!area) {
                const inlineArea = lines.find(line => /^[【\[][^】\]]{1,30}[】\]]$/.test(line));
                area = clean(String(inlineArea || '').replace(/[【】\[\]]/g, ''));
              }
              const salaryLine = lines.find(line => salaryRe.test(line)) || '';
              const salary = clean(salaryLine.replace(/^急聘\s*/, ''));
              const salaryIndex = salaryLine ? lines.indexOf(salaryLine) : -1;
              const company = lines
                .slice(Math.max(0, salaryIndex + 3), salaryIndex + 7)
                .find(line => !/经验|学历|本科|大专|统招|硕士|广告|急聘/.test(line)) || '';

              if (title && href && (href.includes('/job/') || href.includes('/a/'))) {
                items.push({
                  title,
                  salary: clean(salary),
                  company,
                  area,
                  location: area,
                  url: href,
                  platform: '猎聘',
                  source: 'liepin_cdp_search',
                });
              }
            } catch (e) {}
            if (items.length >= maxItems) break;
          }

          return JSON.stringify({
            items,
            blocked: blockedMarker,
            loginRequired,
            url: location.href,
            title: document.title,
            cardCount: cards.length,
            jobLinkCount,
            captchaElementCount,
            blockingModalCount,
            loginModalCount,
            urlSignal,
            waitState: ${JSON.stringify(waitState)},
            networkEntries: (window.__KR_RECRUITMENT_NETWORK__ && window.__KR_RECRUITMENT_NETWORK__.entries || []).slice(-40),
            textSample: text.slice(0, 500),
            page_state: {
              blocked_marker: blockedMarker,
              login_marker: loginRequired,
              card_count: cards.length,
              job_link_count: jobLinkCount,
              captcha_element_count: captchaElementCount,
              blocking_modal_count: blockingModalCount,
              login_modal_count: loginModalCount,
              url_signal: urlSignal,
              security_evidence_strength: securityEvidenceStrength,
              login_evidence_strength: loginEvidenceStrength,
              result_readability: items.length || cards.length || jobLinkCount ? 'readable' : 'not_readable',
            },
          });
        })()
      `;

      const evaluated = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, sessionId);
      const raw = evaluated.result && evaluated.result.result ? evaluated.result.result.value : '{"items":[]}';
      if (process.env.KR_LIEPIN_DEBUG_PARSE === '1') {
        console.error(raw.slice(0, 2000));
      }
      console.log(raw);
      ws.close();
    })().catch(error => {
      console.error(error && error.stack ? error.stack : String(error));
      process.exit(1);
    });
    """

    try:
        selectors_payload = json.dumps({"card": card_selectors}, ensure_ascii=False)
        observer_script = recruitment_network_observer_script()
        proc = silent_subprocess_run(
            ["node", "-e", js_code, str(port), search_url, str(limit), selectors_payload, observer_script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        if proc.returncode != 0:
            log.debug(f"猎聘 CDP 搜索失败: {proc.stderr.strip()}")
            stderr = proc.stderr.strip()
            lowered = stderr.lower()
            failure_type = "cdp_unavailable" if any(token in lowered for token in ("econnrefused", "failed to fetch", "connect", "127.0.0.1")) else "cdp_runtime_error"
            return {
                "items": [],
                "status": "failed",
                "failure_type": failure_type,
                "platform_state": failure_type,
                "manual_action_required": False,
                "error": stderr,
                "diagnostics": {
                    "schema": "knowledgeradar-recruitment-diagnostics/v1",
                    "platform": "liepin",
                    "layers": {
                        "connection": {"status": "failed", "failure_type": failure_type, "port": port},
                        "navigation": {"status": "not_run"},
                        "params": {"status": "ok", "keyword": keyword, "city": city, "city_param": city_value, "city_resolution": city_resolution},
                        "parser": {"status": "not_run"},
                        "evidence": {"status": "none"},
                    },
                },
            }

        data = json.loads(proc.stdout.strip() or "{}")
        data["keyword"] = keyword
        network_items, network_diag = extract_recruitment_items_from_payloads(
            list(data.get("networkEntries") or []),
            platform="liepin",
            keyword=keyword,
            city=city,
            limit=limit,
        )
        if network_items:
            data["items"] = network_items
            data["networkSearch"] = network_diag

        result = _finalize_liepin_search_state(data, city=city, limit=limit)
        result["diagnostics"] = {
            "schema": "knowledgeradar-recruitment-diagnostics/v1",
            "platform": "liepin",
            "layers": {
                "connection": {"status": "ok", "port": port},
                "navigation": {"status": "ok", "url": data.get("url") or search_url, "title": data.get("title") or ""},
                "params": {
                    "status": "ok" if city_resolution.get("status") != "missing" else "city_mapping_missing",
                    "keyword": keyword,
                    "city": city,
                    "city_param": city_value,
                    "city_resolution": city_resolution,
                },
                "parser": {
                    "status": "ok" if result.get("items") else str(result.get("failure_type") or "empty"),
                    "card_count": data.get("cardCount", 0),
                    "job_link_count": data.get("jobLinkCount", 0),
                    "network_item_count": len(network_items),
                },
                "evidence": {"status": "network_json" if network_items else "search_cards" if result.get("items") else "none"},
            },
            "wait_state": data.get("waitState") or {},
            "selectors": {"card": card_selectors},
            "network_search": network_diag,
        }
        return result
    except subprocess.TimeoutExpired:
        log.debug("猎聘 CDP 搜索超时")
        return {"items": [], "status": "failed", "failure_type": "network_timeout"}
    except Exception as e:
        log.debug(f"猎聘 CDP 搜索异常: {e}")
        return {"items": [], "status": "failed", "failure_type": "cdp_runtime_error", "error": str(e)}


def liepin_search_via_cdp(keyword: str, city: str = "", limit: int = 15) -> List[Dict]:
    """Compatibility wrapper returning only items."""
    return list(liepin_search_via_cdp_state(keyword, city, limit).get("items") or [])


def liepin_detail_via_cdp(url: str, timeout_s: int = 30) -> Dict:
    """Extract a Liepin job detail page through the managed CDP browser."""
    static_result = _static_liepin_detail(url)
    if static_result:
        return static_result

    port = int(LIEPIN_CHROME_DEBUG_PORT)
    try:
        _ensure_chrome_debugging("liepin")
    except RuntimeError as e:
        return {
            "platform": "猎聘",
            "url": url,
            "error": f"Chrome 启动失败: {e}",
            "status": "failed",
            "failure_type": "cdp_unavailable",
            "platform_state": "cdp_unavailable",
            "manual_action_required": False,
            "recommended_fallback": "static_detail_retry",
        }

    js_code = r"""
    (async () => {
      const port = process.argv[1];
      const detailUrl = process.argv[2];
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
      const created = await send('Target.createTarget', { url: 'about:blank' });
      const targetId = created.result.targetId;
      const attached = await send('Target.attachToTarget', { targetId, flatten: true });
      const sessionId = attached.result.sessionId;
      await send('Page.enable', {}, sessionId);
      await send('Runtime.enable', {}, sessionId);
      await send('Page.navigate', { url: detailUrl }, sessionId);
      const waitExpression = `
        (() => {
          const text = document.body ? document.body.innerText || '' : '';
          const blocked = /安全验证|滑动验证|captcha/i.test(text);
          const loginLike = text.includes('登录/注册') || text.includes('扫码登录') || text.includes('立即登录') || text.includes('登录后查看') || text.includes('请登录');
          const detailText = Array.from(document.querySelectorAll('.job-intro-container, .job-intro-container .paragraph, section, [class*="job-intro"], [class*="JobIntro"], [class*="description"], [class*="Description"], [class*="content"], [class*="Content"]'))
            .map(el => String(el.innerText || el.textContent || '').trim())
            .filter(Boolean)
            .sort((a, b) => b.length - a.length)[0] || '';
          return JSON.stringify({
            ready: detailText.length >= 180 || blocked || loginLike || document.readyState === 'complete',
            detailChars: detailText.length,
            blocked,
            loginLike,
            readyState: document.readyState,
            url: location.href,
            title: document.title
          });
        })()
      `;
      let waitState = {};
      const waitStarted = Date.now();
      const waitTimeoutMs = Number(process.env.KR_LIEPIN_DETAIL_READY_TIMEOUT_MS || 10000);
      while (Date.now() - waitStarted < waitTimeoutMs) {
        const probe = await send('Runtime.evaluate', { expression: waitExpression, returnByValue: true, awaitPromise: true }, sessionId);
        try { waitState = JSON.parse(probe.result && probe.result.result ? probe.result.result.value : '{}'); } catch (e) { waitState = {}; }
        if (waitState.ready) break;
        await sleep(400);
      }
      waitState.elapsedMs = Date.now() - waitStarted;
      const expression = `
        (() => {
          const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
          const text = document.body ? document.body.innerText || '' : '';
          const blocked = /安全验证|滑动验证|captcha/i.test(text);
          const isVisible = el => {
            if (!el || !el.getBoundingClientRect) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style && style.visibility !== 'hidden' && style.display !== 'none'
              && rect.width > 0 && rect.height > 0;
          };
          const visibleText = el => String((el && (el.innerText || el.textContent)) || '');
          const captchaElementCount = Array.from(document.querySelectorAll('iframe, canvas, [class], [id]'))
            .filter(el => {
              const marker = [el.id || '', String(el.className || ''), el.getAttribute('src') || ''].join(' ').toLowerCase();
              return isVisible(el) && /(captcha|geetest|yidun|slider|nc_|verify)/.test(marker);
            }).length;
          const dialogCandidates = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], [class*="modal"], [class*="Modal"], [class*="popup"], [class*="Popup"], [class*="mask"], [class*="Mask"]'))
            .filter(isVisible);
          const blockingModalCount = dialogCandidates
            .filter(el => /安全验证|滑动验证|人机验证|captcha/i.test(visibleText(el)))
            .length;
          const loginModalCount = dialogCandidates
            .filter(el => /扫码登录|登录\/注册|请先登录|立即登录|登录后/i.test(visibleText(el)))
            .length;
          const currentUrl = location.href || '';
          const urlSignal = /passport|login|account\/login/i.test(currentUrl)
            ? 'login_redirect'
            : /(verify|captcha|security|risk|safe-check|sec)/i.test(currentUrl)
              ? 'verification_redirect'
              : '';
          const jsonLdItems = Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
            .map(el => {
              try { return JSON.parse(el.textContent || '{}'); } catch (e) { return null; }
            })
            .filter(Boolean);
          const jobPosting = jsonLdItems.find(item => item && item['@type'] === 'JobPosting') || {};
          const title = clean(
            jobPosting.title
            || (document.querySelector('h1') || document.querySelector('[class*="job-title"], [class*="JobTitle"]') || {}).textContent
            || document.title
          );
          const salary = clean(((text.match(/\\d+(?:\\.\\d+)?\\s*[-~]\\s*\\d+(?:\\.\\d+)?\\s*[Kk万]?(?:·\\d+薪)?|面议/) || [])[0]) || '');
          const primaryIntro = clean(
            (document.querySelector('.job-intro-container .paragraph') || document.querySelector('.job-intro-container') || {}).innerText
            || (document.querySelector('.job-intro-container .paragraph') || document.querySelector('.job-intro-container') || {}).textContent
            || ''
          );
          const jsonLdDescription = clean(jobPosting.description || '');
          const sections = Array.from(document.querySelectorAll('.job-intro-container, .job-intro-container .paragraph, section, [class*="job-intro"], [class*="JobIntro"], [class*="description"], [class*="Description"], [class*="content"], [class*="Content"]'))
            .map(el => clean(el.innerText || el.textContent))
            .filter(Boolean);
          const jdCandidates = sections.filter(item => /职责|要求|职位|岗位|任职|工作内容|Job|Responsibilities/i.test(item));
          const jd = (
            jsonLdDescription
            || primaryIntro
            || jdCandidates.sort((a, b) => b.length - a.length)[0]
            || sections.sort((a, b) => b.length - a.length)[0]
            || text
          ).slice(0, 5000);
          const loginLike = text.includes('登录/注册')
            || text.includes('扫码登录')
            || text.includes('立即登录')
            || text.includes('登录后查看')
            || text.includes('请登录');
          const securityEvidenceStrength = (captchaElementCount || blockingModalCount || urlSignal === 'verification_redirect')
            ? 'strong'
            : blocked ? 'weak' : 'none';
          const loginEvidenceStrength = (loginModalCount || urlSignal === 'login_redirect')
            ? 'strong'
            : loginLike ? 'weak' : 'none';
          return JSON.stringify({
            platform: '猎聘',
            url: location.href,
            title,
            salary,
            jd,
            blocked,
            loginLike,
            captchaElementCount,
            blockingModalCount,
            loginModalCount,
            urlSignal,
            securityEvidenceStrength,
            loginEvidenceStrength,
            waitState,
            text_length: text.length,
            page_title: document.title,
          });
        })()
      `;
      const evaluated = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, sessionId);
      if (evaluated.result && evaluated.result.exceptionDetails) {
        const details = evaluated.result.exceptionDetails;
        await send('Target.closeTarget', { targetId });
        console.log(JSON.stringify({
          platform: '猎聘',
          url: detailUrl,
          status: 'failed',
          error: (details.exception && (details.exception.description || details.exception.value)) || details.text || 'runtime_evaluate_failed',
          failure_type: 'collector_script_error',
          platform_state: 'collector_script_error',
          manual_action_required: false,
          cdp_exception: details,
        }));
        ws.close();
        return;
      }
      const raw = evaluated.result && evaluated.result.result && evaluated.result.result.value
        ? evaluated.result.result.value
        : JSON.stringify({
            platform: '猎聘',
            url: detailUrl,
            status: 'empty',
            failure_type: 'empty_detail',
            error: 'Runtime.evaluate did not return a JSON string',
            cdp_result: evaluated.result || {},
          });
      await send('Target.closeTarget', { targetId });
      console.log(raw);
      ws.close();
    })().catch(error => {
      console.error(error && error.stack ? error.stack : String(error));
      process.exit(1);
    });
    """
    try:
        proc = silent_subprocess_run(
            ["node", "-e", js_code, str(port), url],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
        if proc.returncode != 0:
            return {
                "platform": "猎聘",
                "url": url,
                "error": proc.stderr.strip()[:500],
                "status": "failed",
                "failure_type": "collector_script_error",
                "platform_state": "collector_script_error",
                "manual_action_required": False,
                "recommended_fallback": "static_detail_retry",
            }
        data = json.loads(proc.stdout.strip() or "{}")
        data["diagnostics"] = {
            "schema": "knowledgeradar-recruitment-diagnostics/v1",
            "platform": "liepin",
            "layers": {
                "connection": {"status": "ok", "port": port},
                "navigation": {"status": "ok", "url": data.get("url") or url, "title": data.get("page_title") or ""},
                "params": {"status": "ok", "url": url},
                "parser": {"status": "pending", "content_chars": len(str(data.get("jd") or ""))},
                "evidence": {"status": "pending"},
            },
            "wait_state": data.get("waitState") or {},
        }
        if str(data.get("status") or "") == "failed" and str(data.get("failure_type") or "") in {"collector_script_error", "cdp_runtime_error", "network_timeout"}:
            data.setdefault("platform_state", str(data.get("failure_type") or "collector_script_error"))
            data.setdefault("manual_action_required", False)
            data.setdefault("detail_quality", {"ok": False, "reason": str(data.get("failure_type") or "collector_script_error")})
            data.setdefault("recommended_fallback", "static_detail_retry")
            return data
        jd = str(data.get("jd") or "").strip()
        quality = _job_detail_quality(jd)
        data["detail_quality"] = quality
        data["diagnostics"]["layers"]["parser"] = {"status": "ok" if quality["ok"] else "empty", "content_chars": len(jd)}
        data["diagnostics"]["layers"]["evidence"] = {"status": "detail_text" if quality["ok"] else "none"}
        classified = classify_page_access_state(
            platform="liepin",
            operation="detail",
            blocked_marker=bool(data.get("blocked")),
            login_marker=bool(data.get("loginLike")),
            captcha_element_count=data.get("captchaElementCount"),
            content_chars=len(jd),
            content_readable=bool(quality["ok"]),
            url_signal=str(data.get("urlSignal") or ""),
            security_evidence_strength=str(data.get("securityEvidenceStrength") or ("weak" if data.get("blocked") else "none")),
            login_evidence_strength=str(data.get("loginEvidenceStrength") or ("weak" if data.get("loginLike") else "none")),
            blocking_modal_count=data.get("blockingModalCount"),
            login_modal_count=data.get("loginModalCount"),
            extra_signals={
                "title": data.get("title") or data.get("page_title") or "",
                "url": data.get("url") or url,
                "detail_quality": quality,
            },
        )
        data = merge_page_state(data, classified)
        if data.get("status") == "needs_interaction":
            if data.get("failure_type") == "login_required":
                data["platform_state"] = "login_required_for_detail"
                data["hint"] = "猎聘详情页未提取到完整 JD，页面出现登录入口；请登录猎聘 Profile 后重试。"
            else:
                data["hint"] = "猎聘详情页触发安全验证；请通过统一浏览器人工交互入口完成验证后重试。"
        elif not quality["ok"]:
            data["status"] = "empty"
            data["failure_type"] = "empty_detail"
            data["platform_state"] = "empty_detail"
            data["manual_action_required"] = False
            data["hint"] = "猎聘详情页未提取到完整 JD；可能是页面结构变化或异步内容未加载。"
        else:
            data["status"] = "ok"
            data["failure_type"] = ""
            data["manual_action_required"] = False
        return data
    except subprocess.TimeoutExpired:
        return {
            "platform": "猎聘",
            "url": url,
            "error": "timeout",
            "status": "failed",
            "failure_type": "network_timeout",
            "platform_state": "network_timeout",
            "manual_action_required": False,
            "recommended_fallback": "static_detail_retry",
        }
    except Exception as e:
        return {
            "platform": "猎聘",
            "url": url,
            "error": str(e),
            "status": "failed",
            "failure_type": "collector_script_error",
            "platform_state": "collector_script_error",
            "manual_action_required": False,
            "recommended_fallback": "static_detail_retry",
        }


def _bring_liepin_to_front_for_login() -> None:
    """已弃用：不再自动弹出 Chrome 窗口。登录请使用 request_user_login('liepin')。"""
    log.info("猎聘登录态失效，请使用 request_user_login('liepin') 手动登录")


def _validate_search_results(items: List[Dict]) -> Dict:
    """数据质量检查。"""
    if not items:
        return {"status": "empty", "alert": True}
    valid_count = sum(1 for item in items if item.get("title"))
    validity_rate = valid_count / len(items)
    if validity_rate < 0.5:
        return {"status": "degraded", "alert": True, "rate": validity_rate}
    return {"status": "ok", "alert": False, "rate": validity_rate}


def legacy_search_liepin(keyword: str, city: str = "", limit: int = 10) -> Dict:
    """猎聘搜索入口函数。"""
    log.info(f"search_liepin: {keyword}, city={city}, limit={limit}")
    limit = min(limit, 20)
    trace = CollectionTrace("猎聘", ["chrome_cdp_page"])

    city_resolution = resolve_recruitment_city("liepin", city)
    if city and city_resolution.get("status") == "missing":
        trace.add("chrome_cdp_page", "failed", detail="city_mapping_missing", error_type="city_mapping_missing", retryable=False)
        return _format_search_error("猎聘", {
            "error": "猎聘城市参数缺少可信 dqCode 映射",
            "hint": "该城市未在猎聘官方 dqCode 注册表中验证，已停止搜索以避免假搜或搜错城市。",
            "failure_type": "city_mapping_missing",
            "manual_action_required": False,
            "platform_state": "city_mapping_missing",
            "diagnostics": {
                "schema": "knowledgeradar-recruitment-diagnostics/v1",
                "platform": "liepin",
                "layers": {
                    "connection": {"status": "not_run"},
                    "navigation": {"status": "not_run"},
                    "params": {"status": "city_mapping_missing", "keyword": keyword, "city": city, "city_resolution": city_resolution},
                    "parser": {"status": "not_run"},
                    "evidence": {"status": "none"},
                },
            },
        }, trace=trace, strategy="chrome_cdp_page")

    # 策略门禁检查
    gate = check_search_gate("liepin", keyword=keyword, city=city)
    if not gate["allowed"]:
        log.warning(f"猎聘搜索被门禁拦截: {gate['reason']}")
        return _format_search_error("猎聘", {
            "error": f"搜索被策略门禁拦截: {gate['reason']}",
            "gate_status": gate,
        }, trace=trace, strategy="chrome_cdp_page")

    try:
        _ensure_chrome_debugging("liepin")
    except RuntimeError as e:
        log.error(f"猎聘 Chrome 启动失败: {e}")
        record_search_outcome("liepin", "failed", f"Chrome启动失败: {e}", keyword=keyword, city=city)
        return _format_search_error("猎聘", {"error": f"Chrome 启动失败: {e}"}, trace=trace, strategy="chrome_cdp_page")

    try:
        search_state = liepin_search_via_cdp_state(keyword, city, limit)
        items = list(search_state.get("items") or [])

        if items:
            record_search_outcome("liepin", "ok", keyword=keyword, city=city)
            quality = _validate_search_results(items)
            if quality.get("alert"):
                log.warning(f"猎聘数据质量下降: {quality}")
            trace.add("chrome_cdp_page", "ok", item_count=len(items))
            return _format_search_response("猎聘", items, trace=trace)

        if str(search_state.get("status") or "") == "needs_interaction":
            failure_type = str(search_state.get("failure_type") or "manual_action_required")
            record_search_outcome("liepin", "failed", failure_type, keyword=keyword, city=city)
            trace.add("chrome_cdp_page", "failed", detail=failure_type, error_type=failure_type, retryable=True)
            return _format_search_error("猎聘", {
                "error": "猎聘页面需要登录或安全验证",
                "hint": "请通过统一浏览器人工交互入口完成猎聘登录或安全验证后重试。",
                "failure_type": failure_type,
                "user_action_required": True,
                "manual_action_required": True,
                "manual_confidence": str(search_state.get("manual_confidence") or "confirmed"),
                "platform_state": str(search_state.get("platform_state") or "manual_action_required"),
                "page_state": search_state.get("page_state"),
                "recommended_action": "health_check(mode='request_browser_interaction:liepin:login_or_security_verification')",
            }, trace=trace, strategy="chrome_cdp_page")

        empty_reason = str(search_state.get("failure_type") or "empty_results")
        record_search_outcome("liepin", "failed", empty_reason, keyword=keyword, city=city)
        trace.add("chrome_cdp_page", "failed", detail=empty_reason, error_type=empty_reason, retryable=True)

        return _format_search_error("猎聘", {
            "error": "猎聘搜索无结果",
            "hint": "页面未解析到职位卡片；这可能是选择器变化、关键词无结果或页面加载异常。未自动弹出浏览器。",
            "failure_type": empty_reason,
            "user_action_required": False,
            "manual_action_required": False,
            "manual_confidence": str(search_state.get("manual_confidence") or "none"),
            "platform_state": str(search_state.get("platform_state") or empty_reason),
            "city_filter": search_state.get("city_filter"),
            "page_state": search_state.get("page_state"),
            "recommended_action": "inspect_liepin_page_state",
        }, trace=trace, strategy="chrome_cdp_page")
    finally:
        finish_chrome_automation("liepin", reason="liepin_search")
