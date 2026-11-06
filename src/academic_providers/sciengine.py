"""SciEngine public journal provider."""

from __future__ import annotations

from generic_web.browser_runtime import owned_sync_browser

import html
import re
from typing import Any, Dict, List
from urllib.parse import quote, urljoin

from runtime.asyncio_isolation import run_sync_in_worker_if_asyncio

from .chinese_open_access import ChineseOpenAccessProvider, DEFAULT_HEADERS, OpenAccessPlatformConfig, public_json_search, json_response, clean_api_text
from .models import AcademicSearchRequest, AcademicWork, normalize_doi


def sync_playwright():
    from playwright.sync_api import sync_playwright as start
    return start()



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
                "network": "public_search_api_and_dynamic_article_page",
                "notes": (
                    "Public search records do not prove full-text access. Eligible article pages expose PDF controls. "
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
        # The current site uses /plat/search and a public multipart search API.
        # /search/search now mounts an empty journal shell and is not a search route.
        def fetch(client):
            fields = dict.fromkeys(("title", "journalId", "fullnameStr", "pubYearOrder", "pubYearFilter",
                "journalTitleFilter", "articleTypeFilter", "bookTitleStr", "fullnameFilter", "searchFromYear",
                "searchToYear", "themeSearch", "searchType", "resourseCode", "startDate", "endDate",
                "searchScope", "queryType", "bookName", "journalTitleShow", "themeShow", "bookTitleShow"), "")
            fields.update(page="1", size=str(max(1, min(request.limit, 20))), queryField_a=request.query.strip())
            payload = json_response(client.post(
                "https://www.sciengine.com/sci-open/api/v1/open/SciSearch/searchNew",
                files={key: (None, value) for key, value in fields.items()},
                headers={"Referer": self.search_urls(request)[0]}))
            if payload.get("success") is not True:
                return None
            return (payload.get("data") or {}).get("records")

        def work(row):
            entries = row.get("entries") or []
            entry = next((e for e in entries if isinstance(e, dict) and str(e.get("language", "")).startswith("zh")),
                         next((e for e in entries if isinstance(e, dict)), {}))
            title = clean_api_text(entry.get("title") or row.get("titleZh") or row.get("titleEn"))
            doi = normalize_doi(str(row.get("doi") or ""))
            is_book = str(row.get("documentType") or "").upper() == "BOOK"
            if doi:
                url = "https://www.sciengine.com/doi/" + quote(doi, safe="/.:-_()")
            elif is_book and row.get("bookId") and row.get("bookPublisherAbb"):
                url = "https://www.sciengine.com/publisher/" + quote(str(row["bookPublisherAbb"]), safe="") + "/book/" + quote(str(row["bookId"]), safe="")
            else:
                return None
            resource = clean_api_text((entry.get("resource") or {}).get("resource") or row.get("resourceZh"))
            year = str(row.get("pubYear") or "")
            return AcademicWork(title=title, url=url, doi=doi,
                authors=[clean_api_text(a.get("author")) for a in entry.get("authors", []) if isinstance(a, dict) and a.get("author")],
                year=int(year) if year.isdigit() else None,
                abstract=clean_api_text(entry.get("abstractInfo") or row.get("abstractZh")),
                source=clean_api_text(entry.get("journalTitle") or row.get("journalTitleZh") or row.get("publisher") or "SciEngine"),
                source_database=self.name, oa_status="open" if resource in {"开放获取", "Open Access"} else "",
                document_type='book' if is_book else str(row.get('documentType') or 'unknown'),
                access_mode="public_search_api", full_text_status="article_landing_unverified",
                verification_status="public_search_record", raw={"query": request.query,
                    "document_type": row.get("documentType"), "access_label": resource,
                    "fulltext_verified": False})
        return public_json_search(self, request, fetch, work)

    def search_urls(self, request: AcademicSearchRequest):
        query = quote(str(request.query or "").strip())
        if query:
            return (f"https://www.sciengine.com/plat/search?queryField_a={query}", self.config.homepage)
        return (self.config.homepage,)

    def verify_article_fulltext(self, article_url: str, *, include_ocr=None, document_options=None) -> Dict[str, Any]:
        return verify_sciengine_article_fulltext(article_url, timeout_s=self.timeout_s, include_ocr=include_ocr, document_options=document_options)

    def verify_pdf_text_url(self, pdf_url: str, *, referer: str = "https://www.sciengine.com/", include_ocr=None, document_options=None) -> Dict[str, Any]:
        return verify_sciengine_pdf_text_url(pdf_url, referer=referer, timeout_s=self.timeout_s, include_ocr=include_ocr, document_options=document_options)


def verify_sciengine_article_fulltext(article_url: str, *, timeout_s: float = 20.0, include_ocr=None, document_options=None) -> Dict[str, Any]:
    url = normalize_sciengine_article_url(article_url)
    if not url:
        return _verification_error("unsupported_url", str(article_url or ""), "not_a_sciengine_article_url")

    return run_sync_in_worker_if_asyncio(
        lambda: _verify_sciengine_article_fulltext_sync(url, timeout_s=timeout_s, include_ocr=include_ocr, document_options=document_options),
        timeout_s=max(30.0, timeout_s * 5),
        thread_name_prefix="kr-sciengine",
    )


def _verify_sciengine_article_fulltext_sync(url: str, *, timeout_s: float = 20.0, include_ocr=None, document_options=None) -> Dict[str, Any]:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    with sync_playwright() as playwright, owned_sync_browser(playwright) as browser:
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
        navigation = page.goto(url, wait_until="domcontentloaded", timeout=int(min(timeout_s, 45) * 1000))
        if navigation and navigation.status in {401,403,429,451}:
            return _verification_error('access_denied', url, f'http_{navigation.status}')
        visible = _safe_body_text(page)
        if any(marker in visible for marker in ('Security Verification', '访问验证', '验证码', '403 Forbidden')):
            return _verification_error('access_verification_required', url, 'site_verification_required')
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
        pdf_probe = verify_sciengine_pdf_text_url(pdf_url, referer=page_url, timeout_s=timeout_s, include_ocr=include_ocr, document_options=document_options) if pdf_url else {}
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
            "input_sha256":pdf_probe.get("input_sha256", ""),
            "pdf_processing":pdf_probe.get("pdf_processing", {}),
            "init_article_fields": _summarize_init_articles(init_articles),
        }


def verify_sciengine_pdf_text_url(pdf_url: str, *, referer: str = "https://www.sciengine.com/", timeout_s: float = 600.0, include_ocr=None, document_options=None) -> Dict[str, Any]:
    from runtime.pdf_text import fetch_pdf_document
    return fetch_pdf_document(pdf_url, referer=referer, timeout_s=timeout_s, include_ocr=include_ocr, document_options=document_options)


def extract_pdf_text(pdf_bytes: bytes, *, max_pages: int | None = None) -> Dict[str, Any]:
    from runtime.pdf_text import extract_pdf_text as extract
    return extract(pdf_bytes, max_pages=max_pages)


def normalize_sciengine_article_url(article_url: str) -> str:
    url = str(article_url or "").strip()
    if not url:
        return ""
    from urllib.parse import urlsplit
    host = (urlsplit(url).hostname or '').lower()
    if host != 'sciengine.com' and not host.endswith('.sciengine.com'):
        return ""
    doi = _doi_from_url(url)
    if doi:
        return f"https://www.sciengine.com/doi/articleIndex/{quote(doi, safe='/.:-_()')}"
    if "/doi/" in url or "articleIndex" in url:
        return url
    return ""


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
