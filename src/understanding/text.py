"""Text understanding helpers for detail extraction."""

from __future__ import annotations

import json
import logging
import re
from runtime.process import silent_subprocess_run
from typing import Dict, Optional

from runtime.chrome_manager import ZHIHU_CHROME_DEBUG_PORT, _ensure_chrome_debugging

log = logging.getLogger("mcp-server")


def strip_html_text(value: str) -> str:
    """Convert simple HTML-rich platform content into readable plain text."""
    if not value:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    import html as _html
    text = _html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def looks_like_zhihu_not_found(html: str) -> bool:
    text = strip_html_text(html or "")
    return "你似乎来到了没有知识存在的荒原" in text or "404 - 知乎" in text


def extract_zhihu_article_from_html(html: str, *, url: str) -> Optional[Dict]:
    """Fallback for Zhihu article pages when js-initialData is absent."""
    if not html or looks_like_zhihu_not_found(html):
        return None

    title = ""
    for pattern in (
        r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']',
        r'<title[^>]*>(.*?)</title>',
        r'<h1[^>]*>(.*?)</h1>',
    ):
        match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if match:
            title = strip_html_text(match.group(1))
            title = re.sub(r"\s*-\s*知乎\s*$", "", title).strip()
            break

    content_candidates = []
    for pattern in (
        r'<div[^>]+class=["\'][^"\']*RichText[^"\']*["\'][^>]*>(.*?)</div>\s*</div>',
        r'<article[^>]*>(.*?)</article>',
        r'<div[^>]+class=["\'][^"\']*Post-RichText[^"\']*["\'][^>]*>(.*?)</div>',
    ):
        for match in re.finditer(pattern, html, re.IGNORECASE | re.DOTALL):
            text = strip_html_text(match.group(1))
            if len(text) > 80:
                content_candidates.append(text)

    if not content_candidates:
        try:
            from readability import Document  # type: ignore
            doc = Document(html)
            content = strip_html_text(doc.summary(html_partial=True))
            if len(content) > 80:
                content_candidates.append(content)
                title = title or strip_html_text(doc.short_title())
        except Exception:
            pass

    if not content_candidates:
        return None

    content = max(content_candidates, key=len)
    return {
        "title": title or "知乎文章",
        "desc": content[:300],
        "content": content,
        "author": "",
        "votes": 0,
        "normalized_url": url,
    }


def extract_zhihu_article_via_cdp(url: str) -> Optional[Dict]:
    """Extract a Zhihu article through the logged-in Chrome CDP session."""
    try:
        if not _ensure_chrome_debugging("zhihu"):
            return None

        port = int(ZHIHU_CHROME_DEBUG_PORT)
        js_code = r"""
        (async () => {
          const port = process.argv[1];
          const targetUrl = process.argv[2];
          const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
          const request = async (endpoint, init) => {
            const res = await fetch(`http://127.0.0.1:${port}${endpoint}`, init);
            if (!res.ok) throw new Error(`${endpoint} HTTP ${res.status}`);
            return await res.json();
          };

          let targets = await request('/json/list');
          let page = targets.find(t => t.type === 'page' && (t.url || '').includes('zhihu.com'));
          if (!page) {
            try {
              page = await request(`/json/new?${encodeURIComponent('about:blank')}`, { method: 'PUT' });
            } catch {
              page = await request(`/json/new?${encodeURIComponent('about:blank')}`);
            }
          }
          const wsUrl = page.webSocketDebuggerUrl;
          const ws = new WebSocket(wsUrl);
          await new Promise((resolve, reject) => {
            ws.onopen = resolve;
            ws.onerror = reject;
          });
          let seq = 0;
          const pending = new Map();
          ws.onmessage = event => {
            const message = JSON.parse(event.data);
            if (message.id && pending.has(message.id)) {
              pending.get(message.id)(message);
              pending.delete(message.id);
            }
          };
          const send = (method, params = {}) => new Promise((resolve, reject) => {
            const id = ++seq;
            const timer = setTimeout(() => {
              pending.delete(id);
              reject(new Error(`${method} timeout`));
            }, 12000);
            pending.set(id, message => {
              clearTimeout(timer);
              resolve(message);
            });
            ws.send(JSON.stringify({ id, method, params }));
          });

          await send('Page.enable');
          await send('Runtime.enable');
          await send('Page.navigate', { url: targetUrl });
          await sleep(5500);
          const expression = `
            (() => {
              const clean = value => (value || '').replace(/\\s+/g, ' ').trim();
              const title =
                clean(document.querySelector('meta[property="og:title"]')?.content) ||
                clean(document.querySelector('h1')?.textContent) ||
                clean(document.title || '').replace(/\\s*-\\s*知乎\\s*$/, '');
              const author =
                clean(document.querySelector('[class*="AuthorInfo"] [class*="name"]')?.textContent) ||
                clean(document.querySelector('[class*="author"] [class*="name"]')?.textContent) ||
                clean(document.querySelector('meta[name="author"]')?.content);
              const nodes = Array.from(document.querySelectorAll(
                '.Post-RichText, .RichText, article, [class*="RichText"], [class*="Post-content"]'
              ));
              const candidates = nodes
                .map(node => clean(node.innerText || node.textContent || ''))
                .filter(text => text.length > 80);
              let content = candidates.sort((a, b) => b.length - a.length)[0] || '';
              if (!content) {
                const body = clean(document.body ? document.body.innerText || '' : '');
                const markers = ['发布于', '编辑于', '赞同', '添加评论'];
                content = body;
                for (const marker of markers) {
                  const idx = content.indexOf(marker);
                  if (idx > 120) {
                    content = content.slice(0, idx);
                    break;
                  }
                }
              }
              return JSON.stringify({
                title,
                desc: content.slice(0, 300),
                content,
                author,
                votes: 0,
                normalized_url: location.href,
                body_sample: clean(document.body ? document.body.innerText || '' : '').slice(0, 200)
              });
            })()
          `;
          const evaluated = await send('Runtime.evaluate', {
            expression,
            awaitPromise: true,
            returnByValue: true,
          });
          ws.close();
          const value = evaluated.result && evaluated.result.result && evaluated.result.result.value;
          console.log(value || '{}');
        })().catch(error => {
          console.error(error && error.stack ? error.stack : String(error));
          process.exit(1);
        });
        """
        proc = silent_subprocess_run(
            ["node", "-e", js_code, str(port), url],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=25,
        )
        if proc.returncode != 0:
            log.warning(f"知乎文章 CDP 兜底失败: {proc.stderr.strip()}")
            return None
        data = json.loads(proc.stdout.strip() or "{}")
        content = strip_html_text(str(data.get("content") or ""))
        title = strip_html_text(str(data.get("title") or ""))
        if not content or len(content) < 80 or looks_like_zhihu_not_found(content):
            return None
        return {
            "title": title or "知乎文章",
            "desc": content[:300],
            "content": content,
            "author": strip_html_text(str(data.get("author") or "")),
            "votes": int(data.get("votes") or 0),
            "normalized_url": str(data.get("normalized_url") or url),
        }
    except Exception as e:
        log.warning(f"知乎文章 CDP 兜底异常: {e}")
        return None
