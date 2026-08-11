"""Zhihu collection helpers migrated out of server.py."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from runtime.process import silent_subprocess_run
import threading
import time
from typing import Dict, List, Optional

import httpx

from kr_core.collection import CollectionTrace, format_search_error, format_search_response
from runtime.chrome_manager import ZHIHU_CHROME_DEBUG_PORT, _ensure_chrome_debugging, _managed_chrome_profile_dir, finish_chrome_automation

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
log = logging.getLogger("mcp-server")
_NODE_PATH = None
_ZHIHU_HTTP_LOCK = threading.Lock()
_ZHIHU_HTTP_CLIENT: httpx.Client | None = None


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


def read_zhihu_cookies_from_profile() -> Optional[str]:
    user_data_dir = _managed_chrome_profile_dir("zhihu")
    profile_dir = os.path.join(user_data_dir, "Default")
    cookie_db_path = os.path.join(profile_dir, "Network", "Cookies")
    # Local State is always at the root of user-data-dir, NOT inside Default/
    local_state_path = os.path.join(user_data_dir, "Local State")

    if not os.path.isfile(cookie_db_path):
        log.warning(f"知乎 Cookie 数据库不存在: {cookie_db_path}")
        return None

    try:
        import ctypes
        import ctypes.wintypes
        import shutil
        import sqlite3
        import tempfile

        aes_key = None
        if os.path.isfile(local_state_path):
            try:
                with open(local_state_path, "r", encoding="utf-8") as f:
                    local_state = json.load(f)
                encrypted_key_b64 = local_state.get("os_crypt", {}).get("encrypted_key", "")
                if encrypted_key_b64:
                    encrypted_key = base64.b64decode(encrypted_key_b64)
                    if encrypted_key[:5] == b"DPAPI":
                        class DATA_BLOB(ctypes.Structure):
                            _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

                        key_data = encrypted_key[5:]
                        blob_in = DATA_BLOB(len(key_data), ctypes.create_string_buffer(key_data, len(key_data)))
                        blob_out = DATA_BLOB()

                        if ctypes.windll.crypt32.CryptUnprotectData(
                            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
                        ):
                            raw_key = ctypes.string_at(blob_out.pbData, blob_out.cbData)
                            aes_key = raw_key
                            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
            except Exception as e:
                log.debug(f"Local State 读取失败: {e}")

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_path = tmp.name
        shutil.copy2(cookie_db_path, tmp_path)

        conn = sqlite3.connect(tmp_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name, encrypted_value, value FROM cookies WHERE host_key LIKE '%zhihu.com%'"
        )
        rows = cursor.fetchall()
        conn.close()
        os.unlink(tmp_path)

        if not rows:
            log.warning("知乎 Cookie 数据库中无 zhihu.com 条目")
            return None

        def _decrypt_cookie_v10(encrypted_value: bytes, key: bytes) -> Optional[str]:
            if len(encrypted_value) < 15:
                return None
            payload = encrypted_value[3:]
            nonce = payload[:12]
            ciphertext_and_tag = payload[12:]
            try:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM

                aesgcm = AESGCM(key)
                plaintext = aesgcm.decrypt(nonce, ciphertext_and_tag, None)
                return plaintext.decode("utf-8")
            except Exception:
                return None

        cookie_parts = []
        for name, encrypted_value, value in rows:
            if value:
                cookie_parts.append(f"{name}={value}")
            elif encrypted_value and aes_key:
                decrypted = _decrypt_cookie_v10(encrypted_value, aes_key)
                if decrypted:
                    cookie_parts.append(f"{name}={decrypted}")

        if not cookie_parts:
            log.warning("无法解密任何知乎 Cookie（可能缺少 cryptography 库或密钥解密失败）")
            return None

        cookie_str = "; ".join(cookie_parts)
        log.info(f"从 Chrome Profile 读取到 {len(cookie_parts)} 个知乎 Cookie")
        return cookie_str
    except Exception as e:
        log.error(f"读取知乎 Cookie 失败: {e}")
        return None


def _read_zhihu_cookies_from_cdp_node(port: Optional[int] = None) -> Optional[str]:
    port = int(port or ZHIHU_CHROME_DEBUG_PORT)
    js_code = r"""
    (async () => {
      const port = process.argv[1];
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
      const targets = await fetch(`http://127.0.0.1:${port}/json/list`).then(r => r.json());
      const page = targets.find(t => (t.url || '').includes('zhihu.com')) || targets.find(t => t.type === 'page');
      if (!page) {
        console.log(JSON.stringify({ cookies: [] }));
        ws.close();
        return;
      }
      const attached = await send('Target.attachToTarget', { targetId: page.id, flatten: true });
      const sessionId = attached.result.sessionId;
      await send('Network.enable', {}, sessionId);
      const result = await send('Network.getAllCookies', {}, sessionId);
      const cookies = (result.result.cookies || []).filter(c => (c.domain || '').includes('zhihu.com'));
      console.log(JSON.stringify({ cookies }));
      ws.close();
    })().catch(error => {
      console.error(error && error.stack ? error.stack : String(error));
      process.exit(1);
    });
    """
    try:
        proc = silent_subprocess_run(
            ["node", "-e", js_code, str(port)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if proc.returncode != 0:
            log.debug(f"Read Zhihu cookies from CDP via Node failed: {proc.stderr.strip()}")
            return None
        data = json.loads(proc.stdout.strip() or "{}")
        parts = [
            f"{cookie['name']}={cookie['value']}"
            for cookie in data.get("cookies", [])
            if "zhihu.com" in cookie.get("domain", "")
        ]
        if not parts:
            log.warning(f"Chrome CDP:{port} returned no Zhihu cookies via Node")
            return None
        log.info(f"Read {len(parts)} Zhihu cookies from Chrome CDP:{port} via Node")
        return "; ".join(parts)
    except Exception as e:
        log.debug(f"Read Zhihu cookies from CDP via Node failed: {e}")
        return None


def read_zhihu_cookies_from_cdp(port: Optional[int] = None) -> Optional[str]:
    node_cookie = _read_zhihu_cookies_from_cdp_node(port)
    if node_cookie:
        return node_cookie
    try:
        port = int(port or ZHIHU_CHROME_DEBUG_PORT)
        version_url = f"http://127.0.0.1:{port}/json/version"
        with httpx.Client(timeout=5) as client:
            version = client.get(version_url).json()
        browser_ws = version.get("webSocketDebuggerUrl")
        if not browser_ws:
            return None
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(browser_ws)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            cookies = context.cookies(["https://www.zhihu.com", "https://zhuanlan.zhihu.com"])
        parts = [f"{cookie['name']}={cookie['value']}" for cookie in cookies if "zhihu.com" in cookie.get("domain", "")]
        if not parts:
            return None
        log.info(f"Read {len(parts)} Zhihu cookies from Chrome CDP:{port}")
        return "; ".join(parts)
    except Exception as e:
        log.debug(f"Read Zhihu cookies from CDP failed: {e}")
        return None


def zhihu_sign(url: str, cookies: str) -> Dict:
    global _NODE_PATH
    if not globals().get("_NODE_PATH"):
        _NODE_PATH = "node"
    js_path = os.path.join(PROJECT_ROOT, "libs", "zhihu.js")
    with open(js_path, mode="r", encoding="utf-8-sig") as f:
        js_source = f.read()
    input_json = json.dumps({"url": url, "cookies": cookies})
    js_code = f"""
    var data = JSON.parse(process.argv[1]);
    {js_source}
    var result = get_sign(data.url, data.cookies);
    console.log(JSON.stringify(result));
    """
    try:
        r = silent_subprocess_run(
            [_NODE_PATH, "-e", js_code, input_json],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if r.returncode == 0:
            return json.loads(r.stdout.strip())
        raise RuntimeError(f"Node sign failed: {r.stderr}")
    except Exception as e:
        raise RuntimeError(f"知乎签名失败: {e}")


def zhihu_search_api(keyword: str, cookie_str: str, limit: int = 20) -> List[Dict]:
    from urllib.parse import urlencode

    uri = "/api/v4/search_v3"
    params = {
        "gk_version": "gz-gaokao",
        "t": "general",
        "q": keyword,
        "correction": 1,
        "offset": 0,
        "limit": min(limit, 20),
        "filter_fields": "",
        "lc_idx": 0,
        "show_all_topics": 0,
        "search_source": "Filter",
        "time_interval": "",
        "sort": "",
        "vertical": "",
    }
    full_uri = uri + "?" + urlencode(params)
    sign_res = zhihu_sign(full_uri, cookie_str)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.zhihu.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cookie": cookie_str,
        "Priority": "u=1, i",
        "X-Api-Version": "3.0.91",
        "X-App-Za": "OS=Web",
        "X-Requested-With": "fetch",
        "X-Zse-93": "101_3_3.0",
        "X-Zse-96": sign_res.get("x-zse-96", ""),
        "X-Zst-81": sign_res.get("x-zst-81", ""),
    }
    api_url = f"https://www.zhihu.com/api/v4/search_v3?{urlencode(params)}"
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            resp = _zhihu_http_client().get(api_url, headers=headers)
            break
        except (httpx.ConnectError, httpx.ConnectTimeout, OSError) as exc:
            last_exc = exc
            if attempt == 0:
                log.warning(f"知乎 API 连接异常，短退避后重试: {exc}")
                _reset_zhihu_http_client()
                time.sleep(1.2)
                continue
            raise
    else:
        raise last_exc or RuntimeError("知乎 API 连接异常")
    if resp.status_code in (400, 401, 403):
        raise RuntimeError(f"知乎 API 鉴权失败 (HTTP {resp.status_code})，Cookie 可能已过期")
    resp.raise_for_status()
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(f"知乎 API 错误: {data['error'].get('message', '')}")
    items = []
    for entry in data.get("data", []):
        obj = entry.get("object") or entry.get("target") or entry
        content = obj.get("content") or obj.get("excerpt") or obj.get("description") or ""
        question = obj.get("question") or {}
        title = obj.get("title") or question.get("title") or entry.get("highlight", {}).get("title", "")
        author = obj.get("author") or obj.get("member") or {}
        url = obj.get("url") or obj.get("content_url") or obj.get("question_url") or entry.get("url") or ""
        if url.startswith("http://api.zhihu.com/"):
            url = url.replace("http://api.zhihu.com/", "https://www.zhihu.com/")
        elif url.startswith("https://api.zhihu.com/"):
            url = url.replace("https://api.zhihu.com/", "https://www.zhihu.com/")
        item = {
            "title": re.sub(r"<[^>]+>", "", str(title or "")),
            "desc": re.sub(r"<[^>]+>", "", str(content or ""))[:200],
            "author": author.get("name", "") if isinstance(author, dict) else "",
            "url": url,
            "votes": obj.get("voteup_count") or obj.get("vote_count") or 0,
            "type": obj.get("type") or entry.get("type") or "",
            "platform": "知乎",
        }
        if item["title"] or item["desc"] or item["url"]:
            items.append(item)
    return items


def _zhihu_http_client() -> httpx.Client:
    global _ZHIHU_HTTP_CLIENT
    with _ZHIHU_HTTP_LOCK:
        if _ZHIHU_HTTP_CLIENT is None:
            _ZHIHU_HTTP_CLIENT = httpx.Client(
                timeout=httpx.Timeout(15.0),
                follow_redirects=True,
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2, keepalive_expiry=20.0),
                trust_env=True,
            )
        return _ZHIHU_HTTP_CLIENT


def _reset_zhihu_http_client() -> None:
    global _ZHIHU_HTTP_CLIENT
    with _ZHIHU_HTTP_LOCK:
        client = _ZHIHU_HTTP_CLIENT
        _ZHIHU_HTTP_CLIENT = None
    if client is not None:
        try:
            client.close()
        except Exception:
            pass


def zhihu_search_via_cdp_page(keyword: str, limit: int = 20) -> List[Dict]:
    port = int(ZHIHU_CHROME_DEBUG_PORT)
    js_code = r"""
    (async () => {
      const port = process.argv[1];
      const keyword = process.argv[2];
      const limit = Number(process.argv[3] || 20);
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
      let page = targets.find(t => t.type === 'page' && (t.url || '').includes('zhihu.com/search'))
        || targets.find(t => t.type === 'page' && (t.url || '').includes('zhihu.com'))
        || targets.find(t => t.type === 'page')
        || targets[0];
      if (!page) {
        console.log(JSON.stringify({ items: [] }));
        ws.close();
        return;
      }
      const attached = await send('Target.attachToTarget', { targetId: page.id, flatten: true });
      const sessionId = attached.result.sessionId;
      await send('Page.enable', {}, sessionId);
      await send('Runtime.enable', {}, sessionId);
      const url = 'https://www.zhihu.com/search?type=content&q=' + encodeURIComponent(keyword);
      await send('Page.navigate', { url }, sessionId);
      await sleep(9000);
      const expression = `
        (() => {
          const seen = new Set();
          const items = [];
          const text = document.body ? document.body.innerText || '' : '';
          const hasLoginPrompt = /登录\/注册|立即登录|登录知乎|扫码登录|验证码登录|密码登录/i.test(text);
          const anchors = Array.from(document.querySelectorAll('a[href]')).filter(a => {
            const href = a.href || '';
            return href.includes('/question/') || href.includes('/answer/') || href.includes('zhuanlan.zhihu.com/p/') || href.includes('/p/') || href.includes('/zvideo/') || href.includes('/people/');
          });
          for (const a of anchors) {
            const href = a.href || '';
            const text = (a.innerText || a.textContent || a.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ');
            if (!href || !text || text.length < 4 || seen.has(href)) continue;
            if (href.includes('/search?') || href.includes('/signin')) continue;
            seen.add(href);
            const card = a.closest('[class*="SearchResult"], [class*="List-item"], [data-za-detail-view-path-module], article, div') || a.parentElement;
            const desc = card ? (card.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 220) : '';
            items.push({ title: text.slice(0, 120), desc, author: '', url: href, platform: '知乎', type: 'web' });
            if (items.length >= ${limit}) break;
          }
          return JSON.stringify({
            items,
            hasLoginPrompt,
            url: location.href,
            title: document.title || '',
            textSample: text.slice(0, 800)
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
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
        )
        if proc.returncode != 0:
            log.debug(f"Zhihu CDP page search failed: {proc.stderr.strip()}")
            return []
        data = json.loads(proc.stdout.strip() or "{}")
        items = data.get("items", [])
        if items:
            log.info(f"Zhihu CDP page search returned {len(items)} items")
        return items[:limit]
    except Exception as e:
        log.debug(f"Zhihu CDP page search failed: {e}")
        return []


def _read_cookies_with_retry(max_retries: int = 3, delay_s: float = 2.0) -> Optional[str]:
    """Read Zhihu cookies with CDP retry. Chrome may need time to load after launch."""
    for attempt in range(max_retries):
        cookie = read_zhihu_cookies_from_cdp()
        if cookie:
            return cookie
        if attempt < max_retries - 1:
            log.info(f"CDP Cookie 读取失败，等待 {delay_s}s 后重试 ({attempt+1}/{max_retries})")
            time.sleep(delay_s)
    # Last resort: try DPAPI (may work outside sandbox)
    return read_zhihu_cookies_from_profile()


def _bring_zhihu_to_front_for_login() -> None:
    """已弃用：不再自动弹出 Chrome 窗口。登录请使用 request_user_login('zhihu')。"""
    log.info("知乎登录态失效，请使用 request_user_login('zhihu') 手动登录")


def legacy_search_zhihu(keyword: str, limit: int = 10) -> Dict:
    log.info(f"search_zhihu: {keyword}, limit={limit}")
    limit = min(limit, 20)
    trace = CollectionTrace("知乎", ["persistent_profile_cookie", "signed_api", "chrome_cdp_page_fallback"])
    try:
        _ensure_chrome_debugging("zhihu")
        cookie_str = _read_cookies_with_retry()
        if not cookie_str:
            trace.add("persistent_profile_cookie", "failed", detail="cookie_missing", error_type="login_required", retryable=True)
            return _format_search_error("知乎", {
                "error": "知乎 Cookie 不存在或无法读取",
                "hint": "未自动弹出浏览器。请通过统一浏览器人工交互入口拉起知乎 Profile，扫码/登录完成并复验后重试。",
                "user_action_required": True,
                "platform_state": "login_required",
                "recommended_action": "request_browser_interaction('zhihu', 'login_required')",
            }, trace=trace, strategy="persistent_profile_cookie")
        trace.add("persistent_profile_cookie", "ok", detail="cookie_readable")
        try:
            items = zhihu_search_api(keyword, cookie_str, limit)
            log.info(f"  -> {len(items)} 条结果")
            if not items:
                trace.add("signed_api", "failed", detail="empty_results", error_type="empty_results", retryable=True)
                fallback_items = zhihu_search_via_cdp_page(keyword, limit)
                if fallback_items:
                    trace.add("chrome_cdp_page_fallback", "ok", item_count=len(fallback_items))
                    return _format_search_response("知乎", fallback_items, trace=trace)
                trace.add("chrome_cdp_page_fallback", "failed", detail="empty_results", error_type="empty_results", retryable=True)
                return format_search_response("知乎", [], trace=trace, metadata={"note": "知乎搜索无结果，可能是关键词未匹配或内容被屏蔽"})
            trace.add("signed_api", "ok", item_count=len(items))
            return _format_search_response("知乎", items, trace=trace)
        except RuntimeError as e:
            err_msg = str(e)
            log.error(f"知乎 API 调用失败: {err_msg}")
            if "403" in err_msg or "400" in err_msg or "鉴权" in err_msg:
                trace.add("signed_api", "failed", detail=err_msg, error_type="login_required", retryable=True)
                fallback_items = zhihu_search_via_cdp_page(keyword, limit)
                if fallback_items:
                    trace.add("chrome_cdp_page_fallback", "ok", item_count=len(fallback_items))
                    return _format_search_response("知乎", fallback_items, trace=trace)
                trace.add("chrome_cdp_page_fallback", "failed", detail="empty_results_after_auth_failure", error_type="empty_results", retryable=True)
                return _format_search_error("知乎", {
                    "error": f"知乎鉴权失败: {err_msg}",
                    "hint": "未自动弹出浏览器。请通过统一浏览器人工交互入口拉起知乎 Profile，扫码/登录完成并复验后重试。",
                    "user_action_required": True,
                    "platform_state": "login_required",
                    "recommended_action": "request_browser_interaction('zhihu', 'auth_failed')",
                }, trace=trace, strategy="signed_api")
            trace.add("signed_api", "failed", detail=err_msg, error_type="request_failed", retryable=True)
            return _format_search_error("知乎", {"error": f"知乎搜索失败: {err_msg}"}, trace=trace, strategy="signed_api")
        except httpx.TimeoutException:
            trace.add("signed_api", "failed", detail="timeout", error_type="request_failed", retryable=True)
            return _format_search_error("知乎", {"error": "知乎 API 请求超时（>15s），请稍后重试", "retryable": True}, trace=trace, strategy="signed_api")
        except Exception as e:
            log.error(f"知乎搜索异常: {e}")
            trace.add("signed_api", "failed", detail=str(e), error_type="request_failed", retryable=True)
            fallback_items = zhihu_search_via_cdp_page(keyword, limit)
            if fallback_items:
                trace.add("chrome_cdp_page_fallback", "ok", item_count=len(fallback_items))
                return _format_search_response("知乎", fallback_items, trace=trace)
            trace.add("chrome_cdp_page_fallback", "failed", detail="empty_results_after_exception", error_type="empty_results", retryable=True)
            return _format_search_error("知乎", {"error": f"知乎搜索异常: {str(e)}"}, trace=trace, strategy="signed_api")
    finally:
        finish_chrome_automation("zhihu", reason="zhihu_search")


def _confirm_zhihu_cookie_state(retries: int = 2, delay_s: float = 1.2) -> str:
    """Retry cookie read once or twice to avoid transient CDP/profile read misses."""
    last_cookie = ""
    for attempt in range(max(1, retries)):
        last_cookie = read_zhihu_cookies_from_cdp() or read_zhihu_cookies_from_profile() or ""
        if last_cookie:
            return last_cookie
        if attempt + 1 < retries:
            time.sleep(delay_s)
    return last_cookie


def inspect_zhihu_cookie_health(warn_within_hours: int = 72) -> Dict:
    profile_dir = os.path.join(_managed_chrome_profile_dir("zhihu"), "Default")
    cookie_db_path = os.path.join(profile_dir, "Network", "Cookies")
    cookie_str = _confirm_zhihu_cookie_state(retries=2, delay_s=1.0)
    if cookie_str:
        return {
            "status": "ok",
            "detail": "知乎当前登录态可读",
            "platform": "zhihu",
            "login_required": False,
            "cookie_source": "cdp" if read_zhihu_cookies_from_cdp() else "profile",
            "cookie_count": len(cookie_str.split("; ")),
        }

    if not os.path.isfile(cookie_db_path):
        return {
            "status": "degraded",
            "detail": f"知乎 Cookie 数据库不存在: {cookie_db_path}",
            "platform": "zhihu",
            "login_required": True,
            "retryable": True,
        }

    try:
        import shutil
        import sqlite3
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_path = tmp.name
        shutil.copy2(cookie_db_path, tmp_path)

        conn = sqlite3.connect(tmp_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name, value, encrypted_value, expires_utc, host_key FROM cookies "
            "WHERE host_key LIKE '%zhihu.com%'"
        )
        rows = cursor.fetchall()
        conn.close()
        os.unlink(tmp_path)

        if not rows:
            return {
                "status": "degraded",
                "detail": "知乎 Cookie 数据库中无 zhihu.com 条目，且 CDP 未读到有效登录态",
                "platform": "zhihu",
                "login_required": True,
                "retryable": True,
            }

        now_ts = time.time()
        expiry_points = []
        has_session_cookie = False
        for name, value, encrypted_value, expires_utc, host_key in rows:
            if not expires_utc or float(expires_utc) <= 0:
                has_session_cookie = True
                continue
            expiry_ts = float(expires_utc) / 1_000_000 - 11_644_473_600
            expiry_points.append((expiry_ts, name, host_key))

        if not expiry_points:
            return {
                "status": "ok",
                "detail": "知乎 Cookie 可读，未找到可计算的过期时间",
                "platform": "zhihu",
                "login_required": False,
                "cookie_source": "profile",
                "cookie_mode": "session",
            }

        soonest_ts, soonest_name, soonest_host = min(expiry_points, key=lambda item: item[0])
        remaining_s = round(soonest_ts - now_ts, 0)
        remaining_h = round(remaining_s / 3600, 1)
        if remaining_s <= 0:
            return {
                "status": "degraded",
                "detail": f"知乎 Cookie 已过期或即将失效: {soonest_name}@{soonest_host}",
                "platform": "zhihu",
                "login_required": True,
                "retryable": True,
                "cookie_source": "profile",
                "expires_in_s": remaining_s,
                "expires_in_h": remaining_h,
                "expires_at_unix": soonest_ts,
            }
        if remaining_s <= warn_within_hours * 3600:
            return {
                "status": "degraded",
                "detail": f"知乎 Cookie 即将过期: {soonest_name}@{soonest_host}，剩余约 {remaining_h} 小时",
                "platform": "zhihu",
                "login_required": False,
                "retryable": True,
                "cookie_source": "profile",
                "expires_in_s": remaining_s,
                "expires_in_h": remaining_h,
                "expires_at_unix": soonest_ts,
            }
        return {
            "status": "ok",
            "detail": f"知乎 Cookie 有效，最早过期项剩余约 {remaining_h} 小时",
            "platform": "zhihu",
            "login_required": False,
            "cookie_source": "profile",
            "expires_in_s": remaining_s,
            "expires_in_h": remaining_h,
            "expires_at_unix": soonest_ts,
            "session_cookie_present": has_session_cookie,
        }
    except Exception as e:
        return {
            "status": "degraded",
            "detail": f"知乎 Cookie 健康检查异常: {e}",
            "platform": "zhihu",
            "login_required": False,
            "retryable": True,
        }
