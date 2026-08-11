"""SciEngine public journal provider."""

from __future__ import annotations

import html
from io import BytesIO
import re
from typing import Any, Dict, List
from urllib.parse import quote, urljoin

import httpx
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from runtime.asyncio_isolation import run_sync_in_worker_if_asyncio
from runtime.executables import resolve_managed_chrome

from .chinese_open_access import ChineseOpenAccessProvider, DEFAULT_HEADERS, OpenAccessPlatformConfig
from .models import AcademicSearchRequest, AcademicWork, normalize_doi


SCIENGINE_HEADERS = {
    **DEFAULT_HEADERS,
    "Accept": "application/pdf,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
SCIENGINE_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
SCIENGINE_MIN_EXTRACTED_TEXT_CHARS = 500


class SciEngineProvider(ChineseOpenAccessProvider):
    name = "sciengine"
    config = OpenAccessPlatformConfig(
        name="sciengine",
        display_name="SciEngine",
        homepage="https://www.sciengine.com/",
        status="available",
        available=True,
        auto_enabled=True,
        access_mode="anonymous_public_pdf_viewer",
        full_text_access="anonymous_open_or_free_pdf_text_confirmed",
        coverage="Science Press/SciEngine journal platform with open/free PDF access on article pages",
        stable=False,
        degraded_reason="",
        failure_category="",
        pdf_url_markers=(".pdf", "/doi/pdf/", "download.sciengine.com/parse/pdf"),
    )
    timeout_s = 20.0

    def status(self) -> dict:
        status = super().status()
        status.update(
            {
                "network": "public_dynamic_page_and_pdf_endpoint",
                "notes": (
                    "SciEngine article pages expose anonymous open/free PDF controls. "
                    "The browser click opens /doi/pdf/<articleBaseId>?ipInfo=..., which redirects to "
                    "download.sciengine.com/parse/pdf and yields text-extractable PDF bytes."
                ),
                "text_extraction": {
                    "method": "playwright_public_article_page_to_pdf_endpoint_then_pypdf",
                    "minimum_chars": SCIENGINE_MIN_EXTRACTED_TEXT_CHARS,
                },
            }
        )
        return status

    def search(self, request: AcademicSearchRequest) -> List[AcademicWork]:
        query = str(request.query or "").strip()
        if not query:
            return []
        limit = max(1, min(int(request.limit or 5), 10))
        candidates = search_sciengine_article_candidates(query, limit=limit, timeout_s=self.timeout_s)
        works: List[AcademicWork] = []
        seen = set()
        for candidate in candidates:
            article_url = candidate.get("article_url") or candidate.get("url") or ""
            doi = normalize_doi(str(candidate.get("doi") or _doi_from_url(article_url)))
            if not article_url or article_url in seen:
                continue
            seen.add(article_url)
            works.append(
                AcademicWork(
                    title=_clean_text(candidate.get("title") or "SciEngine article"),
                    url=article_url,
                    authors=_authors_from_text(candidate.get("authors") or ""),
                    year=_year_from_text(candidate.get("meta") or candidate.get("text") or ""),
                    doi=doi,
                    source=_clean_text(candidate.get("source") or "SciEngine"),
                    oa_status="open",
                    source_database="sciengine",
                    access_mode="anonymous_public_pdf_viewer",
                    full_text_status="open_access_article_detail",
                    provider_confidence=0.82,
                    verification_status="search_result_pdf_control_candidate",
                    license_scope="open_or_platform_terms",
                    raw={
                        "query": request.query,
                        "article_url": article_url,
                        "access_badges": candidate.get("access_badges") or [],
                        "has_download_pdf": bool(candidate.get("has_download_pdf")),
                    },
                )
            )
            if len(works) >= limit:
                break
        return works

    def search_urls(self, request: AcademicSearchRequest):
        query = quote(str(request.query or "").strip())
        if query:
            return (f"https://www.sciengine.com/search/search?queryField_a={query}", self.config.homepage)
        return (self.config.homepage,)

    def verify_article_fulltext(self, article_url: str) -> Dict[str, Any]:
        return verify_sciengine_article_fulltext(article_url, timeout_s=self.timeout_s)

    def verify_pdf_text_url(self, pdf_url: str, *, referer: str = "https://www.sciengine.com/") -> Dict[str, Any]:
        return verify_sciengine_pdf_text_url(pdf_url, referer=referer, timeout_s=self.timeout_s)


def search_sciengine_article_candidates(query: str, *, limit: int = 5, timeout_s: float = 20.0) -> List[Dict[str, Any]]:
    """Search SciEngine public pages and return article-page candidates."""
    return run_sync_in_worker_if_asyncio(
        lambda: _search_sciengine_article_candidates_sync(query, limit=limit, timeout_s=timeout_s),
        timeout_s=max(30.0, timeout_s * 4),
        thread_name_prefix="kr-sciengine",
    )


def _search_sciengine_article_candidates_sync(query: str, *, limit: int = 5, timeout_s: float = 20.0) -> List[Dict[str, Any]]:
    search_url = f"https://www.sciengine.com/search/search?queryField_a={quote(str(query or '').strip())}"
    candidates: List[Dict[str, Any]] = []
    with sync_playwright() as playwright:
        browser = _launch_sciengine_browser(playwright)
        try:
            page = browser.new_page(
                viewport={"width": 1600, "height": 1000},
                locale="zh-CN",
                user_agent=SCIENGINE_USER_AGENT,
            )
            page.goto(search_url, wait_until="domcontentloaded", timeout=int(timeout_s * 1000) * 3)
            try:
                page.wait_for_load_state("networkidle", timeout=int(timeout_s * 1000))
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(2500)
            candidates = page.evaluate(
                """(maxItems) => {
                    const body = document.body;
                    if (!body) return [];
                    const nodes = Array.from(body.querySelectorAll('a[href*="/doi/"], a[href*="doi="]'));
                    const out = [];
                    const seen = new Set();
                    for (const node of nodes) {
                        const href = node.href || '';
                        if (!href || seen.has(href)) continue;
                        const text = (node.innerText || node.textContent || '').trim();
                        const container = node.closest('.resultItem, .listItem, .articleList, li, .item, .media, .row, div') || node.parentElement || node;
                        const blockText = (container.innerText || container.textContent || '').trim();
                        if (!/\\/doi\\//.test(href) && !/doi=/.test(href)) continue;
                        seen.add(href);
                        const doiMatch = href.match(/(?:articleIndex\\/|doi=)(10\\.[^&#?\\s]+)/i) || blockText.match(/10\\.\\d{4,9}\\/[\\w.()/:;-]+/i);
                        out.push({
                          url: href,
                          article_url: href,
                          title: text || blockText.split('\\n').find(Boolean) || 'SciEngine article',
                          text: blockText.slice(0, 1200),
                          meta: blockText,
                          doi: doiMatch ? decodeURIComponent(doiMatch[1]).replace(/[。，,;；\\s]+$/, '') : '',
                          has_download_pdf: /下载\\s*PDF|Download\\s*PDF/i.test(blockText),
                          access_badges: Array.from(new Set((blockText.match(/开放获取|免费获取/g) || [])))
                        });
                        if (out.length >= maxItems) break;
                    }
                    return out;
                }""",
                limit,
            )
        finally:
            browser.close()
    return candidates[:limit]


def verify_sciengine_article_fulltext(article_url: str, *, timeout_s: float = 20.0) -> Dict[str, Any]:
    url = normalize_sciengine_article_url(article_url)
    if not url:
        return _verification_error("unsupported_url", str(article_url or ""), "not_a_sciengine_article_url")

    return run_sync_in_worker_if_asyncio(
        lambda: _verify_sciengine_article_fulltext_sync(url, timeout_s=timeout_s),
        timeout_s=max(30.0, timeout_s * 5),
        thread_name_prefix="kr-sciengine",
    )


def _verify_sciengine_article_fulltext_sync(url: str, *, timeout_s: float = 20.0) -> Dict[str, Any]:
    with sync_playwright() as playwright:
        browser = _launch_sciengine_browser(playwright)
        try:
            page = browser.new_page(
                viewport={"width": 1600, "height": 1000},
                locale="zh-CN",
                accept_downloads=True,
                user_agent=SCIENGINE_USER_AGENT,
            )
            page.add_init_script(
                """
                (() => {
                  window.__kr_opened = [];
                  window.open = function(url, target, features) {
                    window.__kr_opened.push({url: String(url || ''), target: String(target || ''), features: String(features || '')});
                    return null;
                  };
                })();
                """
            )
            init_articles: List[Dict[str, Any]] = []
            ip_info = ""

            def on_response(response):
                nonlocal ip_info
                response_url = response.url
                if "/sciMetrics/initArticle" in response_url:
                    try:
                        payload = response.json()
                    except Exception:
                        payload = {}
                    if isinstance(payload, dict):
                        init_articles.append(payload)
                elif "/doi/getIpInfo" in response_url:
                    try:
                        payload = response.json()
                    except Exception:
                        payload = {}
                    if isinstance(payload, dict):
                        ip_info = str(payload.get("ip") or payload.get("data") or payload.get("ipInfo") or "")

            page.on("response", on_response)
            page.goto(url, wait_until="domcontentloaded", timeout=int(timeout_s * 1000) * 3)
            try:
                page.wait_for_load_state("networkidle", timeout=int(timeout_s * 1000))
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(2500)
            body = _safe_body_text(page)
            page_url = page.url
            title = page.title()
            pdf_url = _opened_pdf_url_from_click(page, page_url, timeout_s=timeout_s)
            if not pdf_url:
                article_base_id = _first_nested_value(init_articles, "articleBaseId")
                if article_base_id:
                    pdf_url = urljoin(page_url, f"/doi/pdf/{article_base_id}")
                    if ip_info:
                        pdf_url = f"{pdf_url}?ipInfo={quote(ip_info, safe='.:')}"
            pdf_probe = verify_sciengine_pdf_text_url(pdf_url, referer=page_url, timeout_s=timeout_s) if pdf_url else {}
            pdf_confirmed = bool(pdf_probe.get("pdf_bytes_confirmed"))
            text_confirmed = bool(pdf_probe.get("text_extractable"))
            return {
                "status": "PASS" if pdf_confirmed and text_confirmed else "EXPECTED_DEGRADED",
                "article_url": url,
                "page_url": page_url,
                "title": title,
                "has_open_access": "开放获取" in body or "Open Access" in body,
                "has_free_access": "免费获取" in body,
                "has_download_pdf": bool(re.search(r"下载\s*PDF|Download\s*PDF", body, re.I)),
                "pdf_url": pdf_url,
                "pdf_bytes_confirmed": pdf_confirmed,
                "text_extractable": text_confirmed,
                "text_probe": pdf_probe.get("text_probe") or _empty_text_probe(),
                "pdf_probe": pdf_probe.get("pdf_probe") or {},
                "init_article_fields": _summarize_init_articles(init_articles),
            }
        finally:
            browser.close()


def verify_sciengine_pdf_text_url(pdf_url: str, *, referer: str = "https://www.sciengine.com/", timeout_s: float = 20.0) -> Dict[str, Any]:
    url = str(pdf_url or "").strip()
    if not url.startswith(("http://", "https://")):
        return {
            "status": "unsupported_url",
            "pdf_url": url,
            "pdf_bytes_confirmed": False,
            "text_extractable": False,
            "text_probe": _empty_text_probe(error="not_http_url"),
            "pdf_probe": {},
        }
    with httpx.Client(timeout=timeout_s, follow_redirects=True, headers={**SCIENGINE_HEADERS, "Referer": referer}) as client:
        response = client.get(url)
    prefix = response.content[:8]
    pdf_confirmed = response.status_code == 200 and prefix.startswith(b"%PDF-")
    text_probe = _empty_text_probe()
    if pdf_confirmed:
        try:
            text_result = extract_pdf_text(response.content)
            extracted_text = text_result["text"]
            text_probe = {
                "extractable": len(extracted_text) >= SCIENGINE_MIN_EXTRACTED_TEXT_CHARS,
                "text_length": len(extracted_text),
                "page_count": text_result["page_count"],
                "sample": extracted_text[:240],
                "error": "",
            }
        except Exception as exc:
            text_probe = _empty_text_probe(error=f"{type(exc).__name__}: {exc}")
    text_confirmed = bool(text_probe.get("extractable"))
    return {
        "status": "PASS" if pdf_confirmed and text_confirmed else "EXPECTED_DEGRADED",
        "pdf_url": url,
        "final_url": str(response.url),
        "pdf_bytes_confirmed": pdf_confirmed,
        "text_extractable": text_confirmed,
        "text_probe": text_probe,
        "pdf_probe": {
            "status_code": response.status_code,
            "first_bytes_hex": prefix.hex(),
            "content_type": response.headers.get("content-type", ""),
            "content_disposition": response.headers.get("content-disposition", ""),
            "content_length": response.headers.get("content-length", ""),
        },
    }


def extract_pdf_text(pdf_bytes: bytes, *, max_pages: int = 5) -> Dict[str, Any]:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(pdf_bytes))
    text_chunks = []
    for page in list(reader.pages)[: max(1, int(max_pages or 1))]:
        text_chunks.append(page.extract_text() or "")
    text = re.sub(r"\s+", " ", "\n".join(text_chunks)).strip()
    return {"text": text, "page_count": len(reader.pages)}


def normalize_sciengine_article_url(article_url: str) -> str:
    url = str(article_url or "").strip()
    if not url:
        return ""
    if "sciengine.com" not in url:
        return ""
    doi = _doi_from_url(url)
    if doi:
        return f"https://www.sciengine.com/doi/articleIndex/{quote(doi, safe='/.:-_()')}"
    if "/doi/" in url or "articleIndex" in url:
        return url
    return ""


def _launch_sciengine_browser(playwright):
    options: Dict[str, Any] = {"headless": True}
    chrome = _find_chrome_exe()
    if chrome:
        options["executable_path"] = chrome
    return playwright.chromium.launch(**options)


def _find_chrome_exe() -> str:
    selection = resolve_managed_chrome()
    return selection.path if selection else ""


def _opened_pdf_url_from_click(page, base_url: str, *, timeout_s: float) -> str:
    selectors = ["a:has-text('预览 PDF')", "a:has-text('Preview PDF')", "a.pdfButton", "div.downloadPDF", "text=下载 PDF", "text=Download PDF"]
    for selector in selectors:
        try:
            before = page.evaluate("() => (window.__kr_opened || []).length")
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=min(6000, int(timeout_s * 1000)))
            locator.click(timeout=min(6000, int(timeout_s * 1000)), force=True)
            page.wait_for_timeout(1200)
            opened = page.evaluate("() => window.__kr_opened || []")
            for item in opened[before:]:
                opened_url = str(item.get("url") or "")
                if "/doi/pdf/" in opened_url:
                    return urljoin(base_url, opened_url)
        except Exception:
            continue
    return ""


def _safe_body_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=8000)
    except Exception:
        return ""


def _verification_error(status: str, article_url: str, reason: str) -> Dict[str, Any]:
    return {
        "status": status,
        "article_url": article_url,
        "pdf_url": "",
        "pdf_bytes_confirmed": False,
        "text_extractable": False,
        "reason": reason,
        "text_probe": _empty_text_probe(error=reason),
        "pdf_probe": {},
    }


def _empty_text_probe(error: str = "") -> Dict[str, Any]:
    return {"extractable": False, "text_length": 0, "page_count": 0, "sample": "", "error": error}


def _doi_from_url(url: str) -> str:
    text = html.unescape(str(url or ""))
    for pattern in (r"articleIndex/([^?#]+)", r"[?&]doi=([^&#]+)", r"/doi/(10\.[^?#]+)", r"(10\.\d{4,9}/[^\s&#?]+)"):
        match = re.search(pattern, text, re.I)
        if match:
            value = re.sub(r"[。；;，,\s]+$", "", match.group(1))
            return normalize_doi(value)
    return ""


def _first_nested_value(values: List[Dict[str, Any]], key: str) -> str:
    for value in values:
        found = _nested_value(value, key)
        if found:
            return str(found)
    return ""


def _nested_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value and value[key]:
            return value[key]
        for item in value.values():
            found = _nested_value(item, key)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _nested_value(item, key)
            if found:
                return found
    return ""


def _summarize_init_articles(values: List[Dict[str, Any]]) -> Dict[str, str]:
    fields = {}
    for key in ("articleBaseId", "pdfPath", "pdfMarkPath", "doi", "title", "enTitle"):
        found = _first_nested_value(values, key)
        if found:
            fields[key] = found
    return fields


def _authors_from_text(value: str) -> List[str]:
    text = _clean_text(value)
    if not text:
        return []
    for marker in ("作者：", "作者 :", "Authors:", "Author:"):
        if marker in text:
            text = text.split(marker, 1)[1]
            break
    text = text.split("\n", 1)[0]
    return [_clean_text(part) for part in re.split(r"[,;，；、]\s*", text) if _clean_text(part)][:12]


def _year_from_text(value: str) -> int | None:
    match = re.search(r"(19|20)\d{2}", str(value or ""))
    return int(match.group(0)) if match else None


def _clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" \t\r\n-|")
    return text
