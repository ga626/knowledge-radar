"""
Scrapling-based Xiaohongshu search adapter.

This module is intentionally narrow: it only fetches the search result page and
normalizes note cards into the same shape used by the existing XHS bridge.
"""
from __future__ import annotations

import json
import logging
import re
from runtime.process import silent_subprocess_run
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote
from urllib.request import Request, urlopen

from runtime.chrome_manager import XHS_CHROME_DEBUG_PORT
from runtime.xhs_page_state import LOGIN_PROMPT_PATTERN, RISK_PATTERNS, DETAIL_BLOCK_PATTERNS
from runtime.xhs_candidates import normalize_xhs_search_candidates

logger = logging.getLogger("xhs_scrapling")

XHS_LOGIN_PROMPT_PATTERN = LOGIN_PROMPT_PATTERN.pattern
XHS_VERIFY_PROMPT_PATTERN = "|".join(
    pattern.pattern for pattern in [*RISK_PATTERNS.values(), *DETAIL_BLOCK_PATTERNS.values()]
)


class XhsScraplingError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_type: str = "scrapling_error",
        retryable: bool = True,
        login_required: bool = False,
        detail: str = "",
    ):
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable
        self.login_required = login_required
        self.detail = detail

    def to_dict(self) -> Dict:
        data = {
            "error": str(self),
            "type": self.error_type,
            "retryable": self.retryable,
        }
        if self.login_required:
            data["login_required"] = True
        if self.detail:
            data["detail"] = self.detail
        return data


def _search_url(keyword: str, feed_type: str = "") -> str:
    url = (
        "https://www.xiaohongshu.com/search_result"
        f"?keyword={quote(keyword)}&source=web_search_result_notes"
    )
    if feed_type == "image":
        url += "&type=51"
    elif feed_type == "video":
        url += "&type=video"
    return url


def _resolve_cdp_url(cdp_url: str) -> str:
    if cdp_url.startswith(("ws://", "wss://")):
        return cdp_url
    if not cdp_url.startswith(("http://", "https://")):
        return cdp_url
    try:
        req = Request(f"{cdp_url.rstrip('/')}/json/version", headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        req = Request(f"{cdp_url.rstrip('/')}/json", headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=5) as resp:
            tabs = json.loads(resp.read().decode("utf-8"))
        page_ws_url = ""
        for tab in tabs:
            if tab.get("type") == "page" and "xiaohongshu.com" in (tab.get("url") or ""):
                page_ws_url = tab.get("webSocketDebuggerUrl", "")
                break
        if page_ws_url:
            return page_ws_url
        browser_ws_url = data.get("webSocketDebuggerUrl", "")
        if browser_ws_url:
            return browser_ws_url
    except Exception as exc:
        raise XhsScraplingError(
            "无法读取 Chrome CDP websocket URL",
            error_type="cdp_unavailable",
            detail=str(exc),
        ) from exc
    raise XhsScraplingError(
        "Chrome CDP 未返回 websocket URL",
        error_type="cdp_unavailable",
        detail=cdp_url,
    )


def _cdp_json(page_ws_url: str, expression: str, *, timeout: int = 15) -> Dict:
    script = r"""
    (async () => {
      const wsUrl = process.argv[1];
      const expression = process.argv[2];
      const ws = new WebSocket(wsUrl);
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
      const send = (method, params) => new Promise(resolve => {
        const id = ++seq;
        pending.set(id, resolve);
        ws.send(JSON.stringify({ id, method, params: params || {} }));
      });
      await send('Runtime.enable');
      const evaluated = await send('Runtime.evaluate', {
        expression,
        awaitPromise: true,
        returnByValue: true
      });
      const value = evaluated.result && evaluated.result.result
        ? evaluated.result.result.value
        : '{}';
      console.log(value || '{}');
      ws.close();
    })().catch(error => {
      console.error(error && error.stack ? error.stack : String(error));
      process.exit(1);
    });
    """
    proc = silent_subprocess_run(
        ["node", "-e", script, page_ws_url, expression],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise XhsScraplingError(
            "当前小红书页面 CDP 探针失败",
            error_type="request_failed",
            detail=proc.stderr[-500:],
        )
    try:
        return json.loads(proc.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise XhsScraplingError(
            "当前小红书页面 CDP 探针 JSON 解析失败",
            error_type="parse_failed",
            detail=proc.stdout[-500:],
        ) from exc


def _find_xhs_page_ws_url(cdp_url: str) -> str:
    req = Request(f"{cdp_url.rstrip('/')}/json", headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=5) as resp:
        tabs = json.loads(resp.read().decode("utf-8"))
    for tab in tabs:
        if tab.get("type") == "page" and "xiaohongshu.com" in (tab.get("url") or ""):
            page_ws_url = tab.get("webSocketDebuggerUrl", "")
            if page_ws_url:
                return page_ws_url
    return ""


def _ensure_xhs_page_ws_url(cdp_url: str) -> str:
    if cdp_url.startswith(("ws://", "wss://")) and "/devtools/page/" in cdp_url:
        return cdp_url
    if cdp_url.startswith(("ws://", "wss://")):
        return cdp_url
    if not cdp_url.startswith(("http://", "https://")):
        return cdp_url

    page_ws_url = _find_xhs_page_ws_url(cdp_url)
    if page_ws_url:
        return page_ws_url

    req = Request(
        f"{cdp_url.rstrip('/')}/json/new?https://www.xiaohongshu.com/explore",
        headers={"User-Agent": "Mozilla/5.0"},
        method="PUT",
    )
    with urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    page_ws_url = data.get("webSocketDebuggerUrl", "")
    if page_ws_url:
        time.sleep(2)
        return page_ws_url

    raise XhsScraplingError(
        "无法在 Chrome CDP 中创建小红书页面",
        error_type="cdp_unavailable",
        detail=cdp_url,
    )


def probe_login_state(cdp_url: Optional[str] = None) -> Dict:
    if cdp_url is None:
        cdp_url = f"http://127.0.0.1:{XHS_CHROME_DEBUG_PORT}"
    page_ws_url = _ensure_xhs_page_ws_url(cdp_url)
    expression = r"""
    (async () => {
      const result = {
        ok: false,
        ui_authenticated: false,
        guest: true,
        status: 0,
        code: null,
        msg: '',
        nickname: '',
        user_id: '',
        url: location.href,
        title: document.title || ''
      };
      try {
        const resp = await fetch('https://edith.xiaohongshu.com/api/sns/web/v2/user/me', {
          credentials: 'include'
        });
        result.status = resp.status;
        const data = await resp.json();
        result.code = data.code;
        result.msg = data.msg || '';
        const user = data.data || {};
        result.guest = Boolean(user.guest);
        result.nickname = user.nickname || '';
        result.user_id = user.user_id || '';
        const text = document.body ? (document.body.innerText || '') : '';
        const has_login_prompt = /登录后查看搜索结果|请先登录|扫码登录|手机号登录|微信登录|验证后可见|登录后可见|登录后浏览/i.test(text);
        const has_verify_prompt = /APP扫码查看|扫码查看|打开小红书APP|小红书App扫码|小红书APP扫码|请使用小红书|请用小红书|验证|滑块|安全|异常|captcha|verify/i.test(text);
        // `user/me` is sometimes rejected with HTTP 406 even in a browser
        // profile that has a valid interactive session.  Do not turn that
        // transport response into a false "please log in" conclusion.  The
        // UI proof is deliberately conservative: it needs the personal-route
        // affordance plus the normal signed-in controls, and no login/verify
        // marker.  It confirms usable session state only; raw user identity is
        // still obtained exclusively from the API when it is available.
        const controls = Array.from(document.querySelectorAll('a,button'))
          .map((element) => (element.innerText || element.getAttribute('aria-label') || '').trim());
        const has_profile_route = Array.from(document.querySelectorAll('a[href]'))
          .some((element) => /^(\/user\/profile|\/user\/me|\/profile)/.test(element.getAttribute('href') || ''));
        const has_signed_in_controls = controls.includes('通知') && controls.includes('我');
        result.has_login_prompt = has_login_prompt;
        result.has_verify_prompt = has_verify_prompt;
        result.ui_authenticated = Boolean(
          has_profile_route && has_signed_in_controls && !has_login_prompt && !has_verify_prompt
        );
        result.ok = data.code === 0 && Boolean(result.user_id);
      } catch (error) {
        result.error = String(error);
      }
      return JSON.stringify(result);
    })()
    """
    return _cdp_json(page_ws_url, expression, timeout=15)


def _probe_login_state_confirmed(cdp_url: Optional[str] = None, retries: int = 2, delay_s: float = 1.2) -> Dict:
    """Retry login-state probe to avoid transient false negatives."""
    if cdp_url is None:
        cdp_url = f"http://127.0.0.1:{XHS_CHROME_DEBUG_PORT}"
    last_state: Dict = {}
    for attempt in range(max(1, retries)):
        last_state = probe_login_state(cdp_url)
        if last_state.get("ok"):
            last_state["confirmed"] = True
            last_state["probe_attempts"] = attempt + 1
            return last_state
        if attempt + 1 < retries:
            time.sleep(delay_s)
    last_state["confirmed"] = False
    last_state["probe_attempts"] = retries
    return last_state


def _read_xhs_cookie_expiry(cdp_url: Optional[str] = None) -> Tuple[int, Optional[float], Optional[str], int]:
    """Best-effort read of XHS cookie expiry from the browser profile.

    Returns:
        (cookie_count, soonest_expiry_ts, soonest_cookie_name, persistent_cookie_count)
    """
    if cdp_url is None:
        cdp_url = f"http://127.0.0.1:{XHS_CHROME_DEBUG_PORT}"
    cookie_count = 0
    soonest_expiry_ts: Optional[float] = None
    soonest_cookie_name: Optional[str] = None
    persistent_cookie_count = 0
    try:
        req = Request(f"{cdp_url.rstrip('/')}/json/version", headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=5) as resp:
            version = json.loads(resp.read().decode("utf-8"))
        browser_ws = version.get("webSocketDebuggerUrl", "")
        if not browser_ws:
            return cookie_count, soonest_expiry_ts, soonest_cookie_name, persistent_cookie_count

        script = r"""
        (async () => {
          const wsUrl = process.argv[1];
          const ws = new WebSocket(wsUrl);
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
          const targets = await fetch(new URL('/json/list', 'http://127.0.0.1:' + process.argv[2]).toString()).then(r => r.json());
          const page = targets.find(t => (t.url || '').includes('xiaohongshu.com')) || targets.find(t => t.type === 'page');
          if (!page) {
            console.log(JSON.stringify({ cookies: [] }));
            ws.close();
            return;
          }
          const attached = await send('Target.attachToTarget', { targetId: page.id, flatten: true });
          const sessionId = attached.result.sessionId;
          await send('Network.enable', {}, sessionId);
          const result = await send('Network.getAllCookies', {}, sessionId);
          console.log(JSON.stringify({ cookies: result.result.cookies || [] }));
          ws.close();
        })().catch(error => {
          console.error(error && error.stack ? error.stack : String(error));
          process.exit(1);
        });
        """
        proc = silent_subprocess_run(
            ["node", "-e", script, browser_ws, str(cdp_url.rsplit(":", 1)[-1].split("/")[0])],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
        )
        if proc.returncode != 0:
            return cookie_count, soonest_expiry_ts, soonest_cookie_name, persistent_cookie_count
        payload = json.loads(proc.stdout.strip() or "{}")
        cookies = [c for c in payload.get("cookies", []) if "xiaohongshu.com" in c.get("domain", "")]
        cookie_count = len(cookies)
        long_lived_login_cookie_names = {"web_session", "id_token", "a1", "webId", "gid"}
        for cookie in cookies:
            cookie_name = cookie.get("name") or ""
            if cookie_name not in long_lived_login_cookie_names:
                continue
            expires_utc = cookie.get("expires", cookie.get("expires_utc"))
            if not expires_utc:
                continue
            try:
                expires_num = float(expires_utc)
            except Exception:
                continue
            if expires_num <= 0:
                continue
            persistent_cookie_count += 1
            expiry_ts = expires_num if expires_num < 1e10 else expires_num / 1_000_000 - 11_644_473_600
            if soonest_expiry_ts is None or expiry_ts < soonest_expiry_ts:
                soonest_expiry_ts = expiry_ts
                soonest_cookie_name = cookie_name
        return cookie_count, soonest_expiry_ts, soonest_cookie_name, persistent_cookie_count
    except Exception:
        return cookie_count, soonest_expiry_ts, soonest_cookie_name, persistent_cookie_count


def inspect_xhs_login_health(cdp_url: Optional[str] = None, warn_within_hours: int = 72) -> Dict:
    """Best-effort XHS login health probe.

    XHS does not expose a stable cookie expiry contract like Zhihu, so we
    combine current CDP login state with a cookie-jar count check. If the
    browser is already logged in, this reports ok. If the login probe is
    downgraded or the cookie jar is sparse, it reports degraded early.
    """
    if cdp_url is None:
        cdp_url = f"http://127.0.0.1:{XHS_CHROME_DEBUG_PORT}"
    try:
        state = _probe_login_state_confirmed(cdp_url, retries=2, delay_s=1.2)
    except Exception as exc:
        return {
            "status": "degraded",
            "detail": f"小红书登录态探针异常: {exc}",
            "platform": "xiaohongshu",
            "login_required": False,
            "retryable": True,
        }

    ok = bool(state.get("ok"))
    guest = bool(state.get("guest", True))
    cookie_count, soonest_expiry_ts, soonest_cookie_name, persistent_cookie_count = _read_xhs_cookie_expiry(cdp_url)
    if ok and cookie_count:
        remaining_h = None
        if soonest_expiry_ts:
            remaining_s = round(soonest_expiry_ts - time.time(), 0)
            remaining_h = round(remaining_s / 3600, 1)
            if remaining_s <= 0:
                return {
                    "status": "degraded",
                    "detail": f"小红书 Cookie 已过期或即将失效: {soonest_cookie_name}",
                    "platform": "xiaohongshu",
                    "login_required": True,
                    "retryable": True,
                    "cookie_count": cookie_count,
                    "persistent_cookie_count": persistent_cookie_count,
                    "login_state": "expired_or_rejected",
                    "expires_in_s": remaining_s,
                    "expires_in_h": remaining_h,
                    "expires_at_unix": soonest_expiry_ts,
                }
            if remaining_s <= warn_within_hours * 3600:
                return {
                    "status": "degraded",
                    "detail": f"小红书 Cookie 即将过期: {soonest_cookie_name}，剩余约 {remaining_h} 小时",
                    "platform": "xiaohongshu",
                    "login_required": False,
                    "retryable": True,
                    "cookie_count": cookie_count,
                    "persistent_cookie_count": persistent_cookie_count,
                    "login_state": "authenticated",
                    "expires_in_s": remaining_s,
                    "expires_in_h": remaining_h,
                    "expires_at_unix": soonest_expiry_ts,
                }
        return {
            "status": "ok",
            "detail": "小红书当前登录态可用" if not guest else "小红书当前登录态可用（接口仍返回 guest=true，但 user_id 已存在）",
            "platform": "xiaohongshu",
            "login_required": False,
            "cookie_count": cookie_count,
            "persistent_cookie_count": persistent_cookie_count,
            "login_state": "authenticated",
            "expires_in_h": remaining_h,
            "expires_in_s": None if remaining_h is None else round(remaining_h * 3600, 0),
            "expires_at_unix": soonest_expiry_ts,
        }

    return {
        "status": "degraded",
        "detail": "小红书登录态探针未通过，建议及时重新登录",
        "platform": "xiaohongshu",
        "login_required": True,
        "retryable": True,
        "cookie_count": cookie_count,
        "persistent_cookie_count": persistent_cookie_count,
        "login_state": "guest_or_expired" if guest else "unknown",
        "probe": state,
        "confirmed": bool(state.get("confirmed")),
        "probe_attempts": state.get("probe_attempts", 1),
        "warn_within_hours": warn_within_hours,
    }


def _is_really_login_required(cdp_url: str, detail: str = "") -> bool:
    try:
        state = _probe_login_state_confirmed(cdp_url, retries=2, delay_s=1.0)
        if state.get("ok") or (state.get("user_id") and not state.get("has_verify_prompt")):
            logger.info(
                "XHS login probe passed despite login markers: user=%s",
                state.get("nickname") or state.get("user_id") or "<unknown>",
            )
            return False
        if state.get("user_id") and not state.get("has_login_prompt"):
            logger.info(
                "XHS login probe treating user_id as authenticated despite guest flag: user=%s",
                state.get("nickname") or state.get("user_id") or "<unknown>",
            )
            return False
        logger.warning("XHS login probe failed after %s attempts: %s", state.get("probe_attempts", 1), state)
        return True
    except Exception as exc:
        logger.warning("XHS login probe error, treating as login_required: %s | detail=%s", exc, detail[:200])
        return True


def _search_current_page_via_cdp(
    page_ws_url: str,
    keyword: str,
    *,
    limit: int,
    feed_type: str,
    cdp_url: str = "",
    probe_mode: bool = False,
    timeout_s: int = 35,
) -> List[Dict]:
    wait_ms = 2500 if probe_mode else 7000
    max_items = max(limit * 2, 5) if probe_mode else max(limit * 2, 20)
    script = r"""
    (async () => {
      const wsUrl = process.argv[1];
      const keyword = process.argv[2];
      const feedType = process.argv[3] || '';
      const limit = Number(process.argv[4] || 10);
      const waitMs = Number(process.argv[5] || 7000);
      const maxItems = Number(process.argv[6] || Math.max(limit * 2, 20));
      const ws = new WebSocket(wsUrl);
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
      const send = (method, params) => new Promise(resolve => {
        const id = ++seq;
        pending.set(id, resolve);
        ws.send(JSON.stringify({ id, method, params: params || {} }));
      });
      await send('Runtime.enable');
      await send('Page.enable');
      let url = 'https://www.xiaohongshu.com/search_result?keyword=' + encodeURIComponent(keyword) + '&source=web_search_result_notes';
      if (feedType === 'image') url += '&type=51';
      if (feedType === 'video') url += '&type=video';
      await send('Page.navigate', { url });
      await new Promise(resolve => setTimeout(resolve, waitMs));
      const expression = `
        (() => {
          const text = document.body ? document.body.innerText || '' : '';
          const items = [];
          const byId = new Map();
          function clean(value) { return (value || '').replace(/\\s+/g, ' ').trim(); }
          function parseNumber(value) {
            const raw = clean(value);
            const match = raw.match(/([0-9.]+)\\s*(万|k|K)?/);
            if (!match) return 0;
            const num = Number(match[1]);
            if (!Number.isFinite(num)) return 0;
            return match[2] === '万' ? Math.round(num * 10000) : Math.round(num);
          }
          function push(link, root) {
            const href = link.href || link.getAttribute('href') || '';
            const idMatch = href.match(/\\/(?:explore|search_result)\\/([^/?#]+)/);
            if (!idMatch) return;
            const node = root || link.closest('[class*="note-item"], section, [data-v-]') || link;
            const tokenMatch = href.match(/[?&]xsec_token=([^&#]+)/);
            const titleNode = node.querySelector('[class*="title"], [class*="desc"], .title, .desc');
            const authorNode = node.querySelector('[class*="author"], [class*="user"], .name, .username');
            const likeNode = node.querySelector('[class*="like"], [class*="count"]');
            let title = clean(link.getAttribute('title') || link.getAttribute('aria-label'));
            if (!title && titleNode) title = clean(titleNode.textContent);
            if (!title) title = clean(link.textContent);
            const existing = byId.get(idMatch[1]);
            if ((!title || title.length < 2 || title.length > 120) && !existing) return;
            const next = {
              title,
              desc: '',
              author: authorNode ? clean(authorNode.textContent) : '',
              likes: likeNode ? parseNumber(likeNode.textContent) : 0,
              url: href,
              noteId: idMatch[1],
              xsecToken: tokenMatch ? tokenMatch[1] : ''
            };
            if (existing) {
              if (!existing.xsecToken && next.xsecToken) {
                Object.assign(existing, next, {
                  title: next.title || existing.title,
                  author: next.author || existing.author,
                  likes: next.likes || existing.likes
                });
              }
              return;
            }
            byId.set(idMatch[1], next);
            items.push(next);
          }
          const links = Array.from(document.querySelectorAll('a[href*="/explore/"], a[href*="/search_result/"]'));
          links.sort((a, b) => ((a.href || '').includes('xsec_token=') ? 0 : 1) - ((b.href || '').includes('xsec_token=') ? 0 : 1));
          links.forEach(link => push(link));
          return JSON.stringify({
            items: items.slice(0, ${maxItems}),
            total: items.length,
            hasLoginPrompt: /登录后查看搜索结果|请先登录|扫码登录|登录后可见|登录后浏览/i.test(text),
            hasVerifyPrompt: /APP扫码查看|扫码查看|打开小红书APP|小红书App扫码|小红书APP扫码|请使用小红书|请用小红书|验证|滑块|安全|异常|captcha|verify/i.test(text),
            textSample: text.slice(0, 500),
            url: location.href,
            title: document.title || ''
          });
        })()
      `;
      const evaluated = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
      console.log(evaluated.result.result.value || '{"items":[]}');
      ws.close();
    })().catch(error => {
      console.error(error && error.stack ? error.stack : String(error));
      process.exit(1);
    });
    """
    proc = silent_subprocess_run(
        ["node", "-e", script, page_ws_url, keyword, feed_type, str(limit), str(wait_ms), str(max_items)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
    )
    if proc.returncode != 0:
        raise XhsScraplingError(
            "当前小红书页面 CDP 搜索失败",
            error_type="request_failed",
            detail=proc.stderr[-500:],
        )
    try:
        data = json.loads(proc.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise XhsScraplingError(
            "当前小红书页面 CDP 搜索 JSON 解析失败",
            error_type="parse_failed",
            detail=proc.stdout[-500:],
        ) from exc
    if data.get("hasLoginPrompt") and (not cdp_url or _is_really_login_required(cdp_url, data.get("textSample", ""))):
        raise XhsScraplingError(
            "小红书登录态失效",
            error_type="login_required",
            login_required=True,
            retryable=True,
            detail=data.get("textSample", ""),
        )
    items = _normalize_items(data.get("items", []), feed_type)
    if not items:
        if data.get("hasVerifyPrompt"):
            raise XhsScraplingError(
                "小红书触发验证页",
                error_type="verification_required",
                retryable=True,
                detail=data.get("textSample", ""),
            )
        if cdp_url and _is_really_login_required(cdp_url, data.get("textSample", "")):
            raise XhsScraplingError(
                "小红书登录态失效",
                error_type="login_required",
                login_required=True,
                retryable=True,
                detail=data.get("textSample", ""),
            )
        raise XhsScraplingError(
            "当前小红书页面 CDP 搜索返回空结果",
            error_type="empty_results",
            retryable=True,
            detail=data.get("textSample", ""),
        )
    return items[:limit]


def _extract_json_from_response(response) -> Dict:
    text = str(getattr(response, "text", "") or "")
    match = re.search(
        r"<script[^>]+id=[\"']kr-xhs-result[\"'][^>]*>(.*?)</script>",
        text,
        re.S | re.I,
    )
    if not match:
        fallback_items = []
        for link_match in re.finditer(
            r"<a\b[^>]*href=[\"']([^\"']*(?:/explore/|/search_result/)[^\"']*)[\"'][^>]*>(.*?)</a>",
            text,
            re.S | re.I,
        ):
            href = link_match.group(1)
            raw_title = re.sub(r"<[^>]+>", " ", link_match.group(2))
            title = re.sub(r"\s+", " ", raw_title).strip()
            if title:
                fallback_items.append({"url": href, "title": title, "desc": title})
        if fallback_items:
            return {
                "items": fallback_items,
                "total": len(fallback_items),
                "hasLoginPrompt": False,
                "hasVerifyPrompt": False,
                "textSample": text[:500],
            }
        snippet = text[:500].replace("\n", " ")
        raise XhsScraplingError(
            "Scrapling 页面脚本未返回搜索结果",
            error_type="parse_failed",
            detail=snippet,
        )
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise XhsScraplingError(
            "Scrapling 搜索结果 JSON 解析失败",
            error_type="parse_failed",
            detail=str(exc),
        ) from exc


def _normalize_items(items: List[Dict], feed_type: str = "") -> List[Dict]:
    return normalize_xhs_search_candidates(items, feed_type=feed_type, source="scrapling")


def _json_from_body(body: bytes) -> Optional[Dict]:
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _extract_items_from_xhr(response, feed_type: str = "", *, cdp_url: str = "") -> List[Dict]:
    items: List[Dict] = []
    login_markers = []
    for xhr in getattr(response, "captured_xhr", []) or []:
        url = getattr(xhr, "url", "")
        data = _json_from_body(getattr(xhr, "body", b""))
        if not data:
            continue
        if (
            "edith.xiaohongshu.com/api/sns/web/" in url
            and data.get("code") == -101
        ):
            login_markers.append(data.get("msg") or "无登录信息")
            continue
        if "login/qrcode" in url:
            login_markers.append("页面触发登录二维码")
            continue
        if "search/notes" not in url and "search/recommend" not in url:
            continue
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        candidates = (
            payload.get("items")
            or payload.get("notes")
            or payload.get("list")
            or payload.get("result")
            or []
        )
        if isinstance(candidates, dict):
            candidates = candidates.get("items") or candidates.get("list") or []
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            note_card = candidate.get("note_card") or candidate.get("note") or candidate
            user = note_card.get("user") or candidate.get("user") or {}
            note_id = note_card.get("note_id") or note_card.get("id") or candidate.get("id") or ""
            xsec_token = candidate.get("xsec_token") or note_card.get("xsec_token") or ""
            title = note_card.get("display_title") or note_card.get("title") or note_card.get("desc") or ""
            if note_id and title:
                items.append(
                    {
                        "note_id": note_id,
                        "xsec_token": xsec_token,
                        "title": title,
                        "desc": note_card.get("desc") or title,
                        "author": user.get("nickname") or "",
                        "likes": note_card.get("liked_count") or note_card.get("likes") or 0,
                        "type": feed_type,
                    }
                )
    if login_markers and not items:
        marker_detail = "; ".join(login_markers[:3])
        if cdp_url and not _is_really_login_required(cdp_url, marker_detail):
            logger.warning("Ignoring XHS login markers because CDP login probe is valid: %s", marker_detail)
            return []
        raise XhsScraplingError(
            "小红书登录态失效",
            error_type="login_required",
            login_required=True,
            retryable=True,
            detail=marker_detail,
        )
    return _normalize_items(items, feed_type)


def search(
    keyword: str,
    *,
    limit: int = 10,
    feed_type: str = "",
    cdp_url: Optional[str] = None,
    timeout_ms: int = 45000,
    probe_mode: bool = False,
) -> List[Dict]:
    """Search Xiaohongshu through Scrapling using an existing Chrome CDP endpoint."""
    if cdp_url is None:
        cdp_url = f"http://127.0.0.1:{XHS_CHROME_DEBUG_PORT}"
    try:
        from scrapling.fetchers import DynamicFetcher
    except Exception as exc:
        raise XhsScraplingError(
            "Scrapling DynamicFetcher 不可用",
            error_type="dependency_missing",
            detail=str(exc),
        ) from exc

    started = time.time()
    url = _search_url(keyword, feed_type)
    resolved_cdp_url = _ensure_xhs_page_ws_url(cdp_url)
    if "/devtools/page/" in resolved_cdp_url:
        items = _search_current_page_via_cdp(
            resolved_cdp_url,
            keyword,
            limit=limit,
            feed_type=feed_type,
            cdp_url=cdp_url,
            probe_mode=probe_mode,
            timeout_s=max(8, min(20, int(timeout_ms / 1000) + 6)),
        )
        for item in items:
            item["source"] = "scrapling-cdp-page"
        return items

    def page_action(page):
        try:
            page.wait_for_timeout(2500 if probe_mode else 5000)
            data = page.evaluate(
                """(maxItems) => {
              const text = document.body ? document.body.innerText || '' : '';
              const loginPattern = /登录后查看搜索结果|扫码登录|登录后可见|登录后浏览|请先登录|登录|注册|login/i;
              const verifyPattern = /APP扫码查看|扫码查看|打开小红书APP|小红书App扫码|小红书APP扫码|请使用小红书|请用小红书|验证|滑块|安全|异常|captcha|verify/i;
              const items = [];
              const byId = new Map();

              function clean(value) {
                return (value || '').replace(/\\s+/g, ' ').trim();
              }

              function parseNumber(value) {
                const raw = clean(value);
                if (!raw) return 0;
                const match = raw.match(/([0-9.]+)\\s*(万|k|K)?/);
                if (!match) return 0;
                const num = Number(match[1]);
                if (!Number.isFinite(num)) return 0;
                return match[2] === '万' ? Math.round(num * 10000) : Math.round(num);
              }

              function pushFromLink(link, fallbackRoot) {
                const href = link.href || link.getAttribute('href') || '';
                const idMatch = href.match(/\\/(?:explore|search_result)\\/([^/?#]+)/);
                if (!idMatch) return;
                const root = fallbackRoot || link.closest('[class*="note-item"], section, [data-v-]') || link;
                const tokenMatch = href.match(/[?&]xsec_token=([^&#]+)/);
                const titleNode = root.querySelector('[class*="title"], [class*="desc"], .title, .desc');
                const authorNode = root.querySelector('[class*="author"], [class*="user"], .name, .username');
                const likeNode = root.querySelector('[class*="like"], [class*="count"]');
                let title = clean(link.getAttribute('title') || link.getAttribute('aria-label'));
                if (!title && titleNode) title = clean(titleNode.textContent);
                if (!title) title = clean(link.textContent).split(' ')[0] || '';
                const existing = byId.get(idMatch[1]);
                if ((!title || title.length < 2 || title.length > 120) && !existing) return;
                const next = {
                  title,
                  desc: title,
                  author: authorNode ? clean(authorNode.textContent) : '',
                  likes: likeNode ? parseNumber(likeNode.textContent) : 0,
                  url: href,
                  noteId: idMatch[1],
                  xsecToken: tokenMatch ? tokenMatch[1] : ''
                };
                if (existing) {
                  if (!existing.xsecToken && next.xsecToken) {
                    Object.assign(existing, next, {
                      title: next.title || existing.title,
                      author: next.author || existing.author,
                      likes: next.likes || existing.likes
                    });
                  }
                  return;
                }
                byId.set(idMatch[1], next);
                items.push(next);
              }

              const links = Array.from(document.querySelectorAll('a[href*="/explore/"], a[href*="/search_result/"]'));
              links.sort((a, b) => ((a.href || '').includes('xsec_token=') ? 0 : 1) - ((b.href || '').includes('xsec_token=') ? 0 : 1));
              links.forEach((link) => pushFromLink(link));

              if (items.length === 0) {
                document.querySelectorAll('[class*="note-item"], section').forEach((card) => {
                  const link = card.querySelector('a[href*="/explore/"], a[href*="/search_result/"]');
                  if (link) pushFromLink(link, card);
                });
              }

              return {
                items: items.slice(0, maxItems),
                total: items.length,
                hasLoginPrompt: loginPattern.test(text),
                hasVerifyPrompt: verifyPattern.test(text),
                textSample: text.slice(0, 500),
                url: location.href,
                title: document.title || ''
              };
            }""",
                max(limit * 2, 5) if probe_mode else max(limit * 2, 20),
            )
            page.evaluate(
                """(data) => {
              const old = document.getElementById('kr-xhs-result');
              if (old) old.remove();
              const script = document.createElement('script');
              script.id = 'kr-xhs-result';
              script.type = 'application/json';
              script.textContent = JSON.stringify(data);
              document.documentElement.appendChild(script);
            }""",
                data,
            )
        except Exception as exc:
            page.evaluate(
                """(message) => {
                  const script = document.createElement('script');
                  script.id = 'kr-xhs-result';
                  script.type = 'application/json';
                  script.textContent = JSON.stringify({
                    items: [],
                    total: 0,
                    hasLoginPrompt: /登录|注册|扫码|login/i.test(document.body ? document.body.innerText || '' : ''),
                    hasVerifyPrompt: /APP扫码查看|扫码查看|打开小红书APP|小红书App扫码|小红书APP扫码|请使用小红书|请用小红书|验证|滑块|captcha|verify/i.test(document.body ? document.body.innerText || '' : ''),
                    textSample: (document.body ? document.body.innerText || '' : '').slice(0, 500),
                    actionError: message,
                    url: location.href,
                    title: document.title || ''
                  });
                  document.documentElement.appendChild(script);
                }""",
                str(exc),
            )

    try:
        response = DynamicFetcher.fetch(
            url,
            cdp_url=resolved_cdp_url,
            headless=False,
            network_idle=True,
            timeout=timeout_ms,
            wait=500 if probe_mode else 1000,
            page_action=page_action,
            google_search=False,
            disable_resources=probe_mode,
            capture_xhr=r".*edith\.xiaohongshu\.com/api/sns/web/.*",
            extra_headers={"Referer": "https://www.xiaohongshu.com/"},
        )
    except Exception as exc:
        raise XhsScraplingError(
            "Scrapling 小红书搜索请求失败",
            error_type="request_failed",
            detail=str(exc),
        ) from exc

    xhr_items = _extract_items_from_xhr(response, feed_type, cdp_url=cdp_url)
    if xhr_items:
        return xhr_items[:limit]
    data = _extract_json_from_response(response)
    if data.get("hasLoginPrompt"):
        if not _is_really_login_required(cdp_url, data.get("textSample", "")):
            logger.warning("Ignoring XHS login prompt because CDP login probe is valid")
        else:
            raise XhsScraplingError(
                "小红书登录态失效",
                error_type="login_required",
                login_required=True,
                retryable=True,
                detail=data.get("textSample", ""),
            )
    if data.get("hasVerifyPrompt") and not data.get("items"):
        raise XhsScraplingError(
            "小红书触发验证页",
            error_type="verification_required",
            retryable=True,
            detail=data.get("textSample", ""),
        )

    items = _normalize_items(data.get("items", []), feed_type)
    logger.info(
        "Scrapling XHS search finished: keyword=%r feed=%s items=%s elapsed=%.1fs",
        keyword,
        feed_type or "all",
        len(items),
        time.time() - started,
    )
    if not items:
        raise XhsScraplingError(
            "Scrapling 小红书搜索返回空结果",
            error_type="empty_results",
            retryable=True,
            detail=data.get("textSample", ""),
        )
    return items[:limit]
