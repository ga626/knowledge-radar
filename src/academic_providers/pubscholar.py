"""PubScholar public academic platform provider."""

from __future__ import annotations

from generic_web.browser_runtime import owned_sync_browser

import html
import json
import time
import re
from typing import Any, Dict, List
from urllib.parse import quote, urljoin

import httpx
from runtime.asyncio_isolation import run_sync_in_worker_if_asyncio

from .chinese_open_access import ChineseOpenAccessProvider, DEFAULT_HEADERS, OpenAccessPlatformConfig
from .models import AcademicSearchRequest, AcademicWork, normalize_doi


def sync_playwright():
    from playwright.sync_api import sync_playwright as start
    return start()



PUBSCHOLAR_HEADERS = {
    **DEFAULT_HEADERS,
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": "https://pubscholar.cn",
    "Referer": "https://pubscholar.cn/",
}
PUBSCHOLAR_USER_ID = ""
ARTICLE_SEARCH_URL = "https://pubscholar.cn/hky/open/resources/api/v1/articles"
ARTICLE_AGGREGATIONS_URL = "https://pubscholar.cn/hky/open/resources/api/v1/articles/aggregations"
PUBSCHOLAR_MIN_EXTRACTED_TEXT_CHARS = 500


class PubScholarProvider(ChineseOpenAccessProvider):
    name = "pubscholar"
    config = OpenAccessPlatformConfig(
        name="pubscholar",
        display_name="PubScholar",
        homepage="https://pubscholar.cn/",
        status="available",
        available=True,
        auto_enabled=True,
        full_text_access="anonymous_open_access_pdf_text_confirmed",
        coverage="CAS public academic search and open/full-text article aggregation",
        stable=False,
        degraded_reason="",
        failure_category="",
        login_url="https://pubscholar.cn/",
        manual_action="",
    )
    timeout_s = 15.0

    def status(self) -> dict:
        status = super().status()
        status.update(
            {
                "network": "public_json_api_and_pdf_viewer",
                "notes": "Anonymous discovery and eligible PDF paths are implemented; current per-article availability and permissions must be verified. A metadata hit is not proof of readable full text.",
                "text_extraction": {
                    "method": "pypdf_from_anonymous_pdf_file",
                    "minimum_chars": PUBSCHOLAR_MIN_EXTRACTED_TEXT_CHARS,
                },
                "resource_types": {
                    "article": {
                        "endpoint": "/hky/open/resources/api/v1/articles",
                        "open_access_filter": {"open_access": "openAccess"},
                        "full_text_access": "direct_pdf_url_or_pdfjs_viewer_text_extractable",
                        "enabled": True,
                    },
                    "patent": {
                        "endpoint": "/hky/open/resources/api/v1/patents",
                        "open_access_filter": {"open_access": "openAccess"},
                        "enabled": False,
                        "reason": "patent content is outside the academic paper full-text provider scope",
                    },
                    "book": {
                        "endpoint": "/hky/open/resources/api/v1/books",
                        "open_access_filter": {"open_access": "openAccess"},
                        "enabled": False,
                        "reason": "book records need separate copyright/source handling",
                    },
                    "sciencedata": {"enabled": False, "reason": "dataset discovery, not paper full text"},
                    "software": {"enabled": False, "reason": "software discovery, not paper full text"},
                    "facility": {"enabled": False, "reason": "facility discovery, not paper full text"},
                },
            }
        )
        return status

    def search(self, request: AcademicSearchRequest) -> List[AcademicWork]:
        query = str(request.query or "").strip()
        if not query:
            return []

        self._open_access_only = bool(request.options.get('open_access_only', False))
        self._source_offset = int(request.options.get('source_offset', 0))
        limit = max(1, min(int(request.limit or 5), 20))
        fetch_limit = limit
        if self._source_offset % limit:
            raise ValueError('source_offset_must_be_multiple_of_limit; keep pagination limit unchanged')
        try:
            data = self._search_with_httpx(query=query, limit=fetch_limit)
        except Exception as exc:
            from runtime.execution_context import ExecutionStopped, check_execution
            if isinstance(exc, ExecutionStopped):
                raise
            check_execution()
            data = self._search_with_browser(query=query, limit=fetch_limit)

        from .chinese_open_access import OpenAccessResults
        items = data.get("content") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return OpenAccessResults([], [{'type': 'parse_error', 'message': 'PubScholar 响应缺少 content 列表；不是零结果。', 'retryable': False}])
        works = [self._work_from_article(item, request) for item in items if isinstance(item, dict)]
        text_candidates = [work for work in works if work.raw.get("pdf_url")]
        diagnostics = [] if items else [{'type': 'no_match',
            'message': '接口明确返回空列表', 'retryable': False}]
        # Search discovery is useful even when a separate article visit is needed
        # to resolve its viewer. Do not discard all metadata as an empty search.
        ordered = text_candidates + [work for work in works if not work.raw.get("pdf_url")]
        result = OpenAccessResults(ordered[:limit], diagnostics)
        result.continuation = {'source_offset':getattr(self,'_source_offset',0)+len(result)} if len(result)==limit else None
        return result

    def _search_with_httpx(self, *, query: str, limit: int) -> Dict[str, Any]:
        payload = build_pubscholar_article_payload(query, limit=limit,
            open_access=getattr(self, '_open_access_only', False))
        payload['page'] = getattr(self, '_source_offset', 0) // limit + 1
        with httpx.Client(timeout=self.timeout_s, follow_redirects=True, headers=PUBSCHOLAR_HEADERS) as client:
            response = client.post(ARTICLE_SEARCH_URL, json=payload)
            response.raise_for_status()
            return response.json()

    def _search_with_browser(self, *, query: str, limit: int) -> Dict[str, Any]:
        """Search via the anonymous PubScholar SPA so its request signer runs."""
        return run_sync_in_worker_if_asyncio(
            lambda: self._search_with_browser_sync(query=query, limit=limit),
            timeout_s=max(30.0, self.timeout_s * 5),
            thread_name_prefix="kr-pubscholar",
        )

    def _search_with_browser_sync(self, *, query: str, limit: int) -> Dict[str, Any]:
        captured: List[Dict[str, Any]] = []
        from runtime.execution_context import check_execution
        if getattr(self, '_source_offset', 0):
            raise ValueError('pubscholar_browser_paging_not_supported')
        check_execution()
        with sync_playwright() as playwright, owned_sync_browser(playwright) as browser:
            page = browser.new_page(viewport={"width": 1600, "height": 1000}, locale="zh-CN")

            def on_response(response):
                if response.url != ARTICLE_SEARCH_URL:
                    return
                request = response.request
                post_data = request.post_data or ""
                try:
                    submitted = json.loads(post_data)
                except (TypeError, ValueError):
                    return
                if submitted.get('query') != query:
                    return
                if getattr(self, '_open_access_only', False) and submitted.get('open_access') != 'openAccess':
                    return
                try:
                    captured.append(response.json())
                except Exception:
                    return

            page.on("response", on_response)
            page.goto(self.config.homepage, wait_until="domcontentloaded", timeout=int(min(self.timeout_s,45) * 1000))
            search_box = page.locator('input[placeholder="发现你感兴趣的内容..."]')
            search_box.wait_for(state="visible", timeout=12000)
            search_box.fill(query, timeout=8000)
            page.get_by_role("button", name="检索", exact=True).click(timeout=8000)
            if getattr(self, '_open_access_only', False):
                page.locator('div.base-switch[role="switch"]').first.click(timeout=8000)
            until = time.monotonic() + min(self.timeout_s, 30)
            while not captured and time.monotonic() < until:
                check_execution()
                page.wait_for_timeout(200)
            if not captured:
                raise TimeoutError("PubScholar article response was not captured")
            data = captured[-1]
            if isinstance(data, dict) and limit != 10 and len(data.get("content") or []) > limit:
                data = {**data, "content": list(data.get("content") or [])[:limit]}
            return data

    def search_urls(self, request: AcademicSearchRequest):
        query = quote(str(request.query or "").strip())
        if query:
            return (f"https://pubscholar.cn/explore?query={query}", "https://pubscholar.cn/resource")
        return ("https://pubscholar.cn/resource", self.config.homepage)

    def _work_from_article(self, item: Dict[str, Any], request: AcademicSearchRequest) -> AcademicWork:
        title = _clean_pubscholar_html(item.get("title") or item.get("titles") or "")
        article_url = _article_url(item)
        pdf_url = _first_pdf_file_url(item)
        source_url = pdf_url or article_url or str(item.get("link") or "")
        full_text_status = "open_access_article_detail" if item.get('open_access') else "article_detail_candidate"
        if pdf_url:
            full_text_status = "pdf_link_found"
        elif item.get("local_links") or item.get("providers"):
            full_text_status = "open_pdf_viewer_candidate"

        raw = {
            "id": item.get("id") or "",
            "type": item.get("type") or "",
            "cn_type": item.get("cn_type") or "",
            "article_url": article_url,
            "pdf_url": pdf_url,
            "local_links_count": len(item.get("local_links") or []),
            "providers_count": len(item.get("providers") or []),
            "open_access_filter":getattr(self, "_open_access_only", False),
            "query": request.query,
        }
        return AcademicWork(
            title=title or "PubScholar article",
            url=source_url,
            authors=_authors(item),
            year=_year(item),
            doi=normalize_doi(str(item.get("doi") or "")),
            abstract=_clean_pubscholar_html(item.get("abstracts") or item.get("abstract") or ""),
            source=_clean_pubscholar_html(item.get("journal") or item.get("source") or "PubScholar"),
            oa_status="open" if pdf_url or item.get("open_access") else "unknown",
            source_database="pubscholar",
            document_type=str(item.get('type') or item.get('cn_type') or 'unknown'),
            access_mode="anonymous_open_access_pdf_viewer",
            full_text_status=full_text_status,
            provider_confidence=0.88 if article_url else 0.74,
            verification_status="open_access_filtered_result" if getattr(self,"_open_access_only",False) else "discovery_only",
            license_scope="unknown",
            raw=raw,
        )

    def verify_article_fulltext(self, article_url: str, *, include_ocr=None, document_options=None) -> Dict[str, Any]:
        """Verify that a PubScholar article detail page exposes an anonymous PDF viewer/file."""
        return verify_pubscholar_article_fulltext(article_url, timeout_s=self.timeout_s, include_ocr=include_ocr, document_options=document_options)

    def verify_pdf_text_url(self, file_url: str, *, referer: str = "https://pubscholar.cn/", include_ocr=None, document_options=None) -> Dict[str, Any]:
        return verify_pubscholar_pdf_text_url(file_url, referer=referer, timeout_s=self.timeout_s, include_ocr=include_ocr, document_options=document_options)


def build_pubscholar_article_payload(query: str, *, limit: int = 10, open_access: bool = False) -> Dict[str, Any]:
    """Build the observed PubScholar article search payload."""
    payload: Dict[str, Any] = {
        "page": 1,
        "size": max(1, min(int(limit or 10), 20)),
        "order_field": "default",
        "order_direction": "desc",
        "lang": "zh",
        "query": str(query or "").strip(),
    }
    if open_access:
        payload["open_access"] = "openAccess"
    return payload


def verify_pubscholar_article_fulltext(article_url: str, *, timeout_s: float = 15.0, include_ocr=None, document_options=None) -> Dict[str, Any]:
    url = str(article_url or "").strip()
    if _looks_like_pubscholar_pdf_candidate(url):
        status = verify_pubscholar_pdf_text_url(url, referer="https://pubscholar.cn/", timeout_s=timeout_s, include_ocr=include_ocr, document_options=document_options)
        status["article_url"] = url
        status.setdefault("viewer_url", "")
        return status
    if not url.startswith("https://pubscholar.cn/articles/"):
        return {
            "status": "unsupported_url",
            "article_url": url,
            "viewer_url": "",
            "file_url": "",
            "pdf_bytes_confirmed": False,
            "reason": "not_a_pubscholar_article_url",
        }

    return run_sync_in_worker_if_asyncio(
        lambda: _verify_pubscholar_article_fulltext_sync(url, timeout_s=timeout_s, include_ocr=include_ocr, document_options=document_options),
        timeout_s=max(30.0, timeout_s * 5),
        thread_name_prefix="kr-pubscholar",
    )


def _verify_pubscholar_article_fulltext_sync(url: str, *, timeout_s: float = 15.0, include_ocr=None, document_options=None) -> Dict[str, Any]:
    with sync_playwright() as playwright, owned_sync_browser(playwright) as browser:
        page = browser.new_page(viewport={"width": 1600, "height": 1000}, locale="zh-CN")
        resource_urls: List[str] = []

        def on_response(response):
            response_url = response.url
            if "/child/pdf-view/" in response_url or "/files?" in response_url or "/api/v2/article/" in response_url:
                resource_urls.append(response_url)

        page.on("response", on_response)
        page.goto(url, wait_until="domcontentloaded", timeout=int(min(timeout_s, 45) * 1000))
        page.wait_for_timeout(4000)
        body = _clean_pubscholar_html(page.locator("body").inner_text(timeout=8000))
        file_urls = [item for item in resource_urls if "/files?" in item]
        viewer_urls = [item for item in resource_urls if "/child/pdf-view/" in item or "/pdf-view/" in item]
        file_url = file_urls[-1] if file_urls else ""
        pdf_confirmed = False
        pdf_probe: Dict[str, Any] = {}
        text_probe: Dict[str, Any] = {
            "extractable": False,
            "text_length": 0,
            "page_count": 0,
            "sample": "",
            "error": "",
        }
        if file_url:
            resolved = verify_pubscholar_pdf_text_url(file_url, referer=url, timeout_s=timeout_s, include_ocr=include_ocr, document_options=document_options)
            pdf_confirmed = resolved['pdf_bytes_confirmed']
            pdf_probe = resolved.get('pdf_probe', {})
            text_probe = resolved['text_probe']
        text_confirmed = bool(text_probe.get("extractable"))
        return {
            "status": "PASS" if pdf_confirmed and text_confirmed else "EXPECTED_DEGRADED",
            "article_url": url,
            "title": page.title(),
            "has_fulltext_section": "全文浏览" in body,
            "viewer_url": viewer_urls[0] if viewer_urls else "",
            "file_url": file_url,
            "pdf_bytes_confirmed": pdf_confirmed,
            "text_extractable": text_confirmed,
            "text_probe": text_probe,
            "pdf_probe": pdf_probe,
            "input_sha256": resolved.get("input_sha256", "") if file_url else "",
            "pdf_processing": resolved.get("pdf_processing", {}) if file_url else {},
            "resource_counts": {
                "article_api": len([item for item in resource_urls if "/api/v2/article/" in item]),
                "viewer": len(viewer_urls),
                "file": len(file_urls),
            },
        }


def verify_pubscholar_pdf_text_url(file_url: str, *, referer: str = "https://pubscholar.cn/", timeout_s: float = 600.0, include_ocr=None, document_options=None) -> Dict[str, Any]:
    from runtime.pdf_text import fetch_pdf_document
    return fetch_pdf_document(file_url, referer=referer, timeout_s=timeout_s, include_ocr=include_ocr, document_options=document_options)


def extract_pdf_text(pdf_bytes: bytes, *, max_pages: int | None = None) -> Dict[str, Any]:
    from runtime.pdf_text import extract_pdf_text as parse
    return parse(pdf_bytes, max_pages=max_pages)


def pubscholar_file_url_from_viewer_url(viewer_url: str) -> str:
    match = re.search(r"[?&]file=([^&]+)", str(viewer_url or ""))
    if not match:
        return ""
    return html.unescape(match.group(1))


def _article_url(item: Dict[str, Any]) -> str:
    for key in ("article_url", "url"):
        value = str(item.get(key) or "").strip()
        if value.startswith(("http://", "https://")):
            return value
    identifier = str(item.get("id") or "").strip()
    if identifier:
        return f"https://pubscholar.cn/articles/{quote(identifier, safe='')}"
    return ""


def _first_pdf_file_url(item: Dict[str, Any]) -> str:
    candidates = []
    for key in ("attachments", "local_links", "providers", "links"):
        value = item.get(key)
        if isinstance(value, list):
            candidates.extend(candidate for candidate in value if isinstance(candidate, (dict, str)))
    for candidate in candidates:
        if isinstance(candidate, str):
            url = candidate.strip()
            if _looks_like_pubscholar_pdf_candidate(url):
                return urljoin("https://pubscholar.cn/", url)
            continue
        for key in ("url", "link", "href", "pdf_url", "download_url", "file_link"):
            url = str(candidate.get(key) or "").strip()
            if not url:
                continue
            if key == "file_link" and "://" not in url and "/" in url:
                return f"https://pubscholar.cn/files?fastdfspath={quote(url, safe='/')}"
            if _looks_like_pubscholar_pdf_candidate(url):
                return urljoin("https://pubscholar.cn/", url)
        fastdfs = str(candidate.get("fastdfspath") or candidate.get("fastdfsPath") or "").strip()
        if fastdfs:
            return f"https://pubscholar.cn/files?fastdfspath={quote(fastdfs, safe='/')}"
    return ""


def _looks_like_pubscholar_pdf_candidate(url: str) -> bool:
    lowered = str(url or "").lower()
    return (
        "fastdfspath=" in lowered
        or lowered.endswith(".pdf")
        or ".pdf?" in lowered
        or "preview2?file=" in lowered
        or "/files?" in lowered
    )


def _authors(item: Dict[str, Any]) -> List[str]:
    values = item.get("author") or item.get("authors") or item.get("authorList") or []
    if isinstance(values, str):
        return [_clean_pubscholar_html(part) for part in re.split(r"[,;，；]", values) if _clean_pubscholar_html(part)]
    if isinstance(values, list):
        authors = []
        for value in values:
            if isinstance(value, dict):
                name = value.get("name") or value.get("fullname") or value.get("authorName")
            else:
                name = value
            cleaned = _clean_pubscholar_html(name)
            if cleaned:
                authors.append(cleaned)
        return authors
    return []


def _year(item: Dict[str, Any]) -> int | None:
    for key in ("year", "date", "publish_year", "pub_year"):
        match = re.search(r"(19|20)\d{2}", str(item.get(key) or ""))
        if match:
            return int(match.group(0))
    return None


def _clean_pubscholar_html(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" \t\r\n-")
    return text
