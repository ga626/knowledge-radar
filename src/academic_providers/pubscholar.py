"""PubScholar public academic platform provider."""

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

from .chinese_open_access import ChineseOpenAccessProvider, DEFAULT_HEADERS, OpenAccessPlatformConfig
from .models import AcademicSearchRequest, AcademicWork, normalize_doi


PUBSCHOLAR_HEADERS = {
    **DEFAULT_HEADERS,
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": "https://pubscholar.cn",
    "Referer": "https://pubscholar.cn/",
}
PUBSCHOLAR_USER_ID = "d78d0ec50d059fe95b075f42c24f4a0a"
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
                "notes": "Article search and full-text PDF text extraction work anonymously; login is only for optional personalized/AI assistant functions.",
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

        limit = max(1, min(int(request.limit or 5), 20))
        fetch_limit = max(limit, min(20, limit * 3))
        try:
            data = self._search_with_httpx(query=query, limit=fetch_limit)
        except Exception:
            data = self._search_with_browser(query=query, limit=fetch_limit)

        items = data.get("content") if isinstance(data, dict) else []
        if not isinstance(items, list):
            return []
        works = [self._work_from_article(item, request) for item in items if isinstance(item, dict)]
        text_candidates = [work for work in works if work.raw.get("pdf_url")]
        return text_candidates[:limit]

    def _search_with_httpx(self, *, query: str, limit: int) -> Dict[str, Any]:
        payload = {
            "page": 1,
            "size": limit,
            "order_field": "default",
            "order_direction": "desc",
            "user_id": PUBSCHOLAR_USER_ID,
            "lang": "zh",
            "query": query,
            "open_access": "openAccess",
        }
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
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1600, "height": 1000}, locale="zh-CN")

                def on_response(response):
                    if response.url != ARTICLE_SEARCH_URL:
                        return
                    request = response.request
                    post_data = request.post_data or ""
                    if '"open_access":"openAccess"' not in post_data.replace(" ", ""):
                        return
                    try:
                        captured.append(response.json())
                    except Exception:
                        return

                page.on("response", on_response)
                page.goto(self.config.homepage, wait_until="domcontentloaded", timeout=int(self.timeout_s * 1000) * 3)
                search_box = page.locator('input[placeholder="发现你感兴趣的内容..."]')
                search_box.wait_for(state="visible", timeout=12000)
                search_box.fill(query, timeout=8000)
                page.get_by_role("button", name="检索", exact=True).click(timeout=8000)
                page.wait_for_timeout(2500)
                page.locator('div.base-switch[role="switch"]').first.click(timeout=8000)
                page.wait_for_timeout(4000)
                if not captured:
                    raise PlaywrightTimeoutError("PubScholar open-access article response was not captured")
                data = captured[-1]
                if isinstance(data, dict) and limit != 10 and len(data.get("content") or []) > limit:
                    data = {**data, "content": list(data.get("content") or [])[:limit]}
                return data
            finally:
                browser.close()

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
        full_text_status = "open_access_article_detail"
        if pdf_url:
            full_text_status = "pdf_text_extractable"
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
            "open_access_filter": True,
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
            oa_status="open",
            source_database="pubscholar",
            access_mode="anonymous_open_access_pdf_viewer",
            full_text_status=full_text_status,
            provider_confidence=0.88 if article_url else 0.74,
            verification_status="open_access_filtered_result",
            license_scope="open_or_platform_terms",
            raw=raw,
        )

    def verify_article_fulltext(self, article_url: str) -> Dict[str, Any]:
        """Verify that a PubScholar article detail page exposes an anonymous PDF viewer/file."""
        return verify_pubscholar_article_fulltext(article_url, timeout_s=self.timeout_s)

    def verify_pdf_text_url(self, file_url: str, *, referer: str = "https://pubscholar.cn/") -> Dict[str, Any]:
        return verify_pubscholar_pdf_text_url(file_url, referer=referer, timeout_s=self.timeout_s)


def build_pubscholar_article_payload(query: str, *, limit: int = 10, open_access: bool = True) -> Dict[str, Any]:
    """Build the observed PubScholar article search payload."""
    payload: Dict[str, Any] = {
        "page": 1,
        "size": max(1, min(int(limit or 10), 20)),
        "order_field": "default",
        "order_direction": "desc",
        "user_id": PUBSCHOLAR_USER_ID,
        "lang": "zh",
        "query": str(query or "").strip(),
    }
    if open_access:
        payload["open_access"] = "openAccess"
    return payload


def verify_pubscholar_article_fulltext(article_url: str, *, timeout_s: float = 15.0) -> Dict[str, Any]:
    url = str(article_url or "").strip()
    if _looks_like_pubscholar_pdf_candidate(url):
        status = verify_pubscholar_pdf_text_url(url, referer="https://pubscholar.cn/", timeout_s=timeout_s)
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
        lambda: _verify_pubscholar_article_fulltext_sync(url, timeout_s=timeout_s),
        timeout_s=max(30.0, timeout_s * 5),
        thread_name_prefix="kr-pubscholar",
    )


def _verify_pubscholar_article_fulltext_sync(url: str, *, timeout_s: float = 15.0) -> Dict[str, Any]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1600, "height": 1000}, locale="zh-CN")
            resource_urls: List[str] = []

            def on_response(response):
                response_url = response.url
                if "/child/pdf-view/" in response_url or "/files?" in response_url or "/api/v2/article/" in response_url:
                    resource_urls.append(response_url)

            page.on("response", on_response)
            page.goto(url, wait_until="domcontentloaded", timeout=int(timeout_s * 1000) * 4)
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
                with httpx.Client(timeout=timeout_s, follow_redirects=True, headers={**DEFAULT_HEADERS, "Referer": url}) as client:
                    response = client.get(file_url)
                    prefix = response.content[:8]
                    pdf_confirmed = response.status_code == 200 and prefix.startswith(b"%PDF-")
                    pdf_probe = {
                        "status_code": response.status_code,
                        "first_bytes_hex": prefix.hex(),
                        "content_type": response.headers.get("content-type", ""),
                        "content_length": response.headers.get("content-length", ""),
                    }
                    if pdf_confirmed:
                        try:
                            text_result = extract_pdf_text(response.content)
                            extracted_text = text_result["text"]
                            text_probe = {
                                "extractable": len(extracted_text) >= PUBSCHOLAR_MIN_EXTRACTED_TEXT_CHARS,
                                "text_length": len(extracted_text),
                                "page_count": text_result["page_count"],
                                "sample": extracted_text[:240],
                                "error": "",
                            }
                        except Exception as exc:
                            text_probe = {
                                "extractable": False,
                                "text_length": 0,
                                "page_count": 0,
                                "sample": "",
                                "error": f"{type(exc).__name__}: {exc}",
                            }
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
                "resource_counts": {
                    "article_api": len([item for item in resource_urls if "/api/v2/article/" in item]),
                    "viewer": len(viewer_urls),
                    "file": len(file_urls),
                },
            }
        finally:
            browser.close()


def verify_pubscholar_pdf_text_url(file_url: str, *, referer: str = "https://pubscholar.cn/", timeout_s: float = 15.0) -> Dict[str, Any]:
    url = str(file_url or "").strip()
    if not url.startswith(("http://", "https://")):
        return {
            "status": "unsupported_url",
            "file_url": url,
            "pdf_bytes_confirmed": False,
            "text_extractable": False,
            "text_probe": {"extractable": False, "text_length": 0, "page_count": 0, "sample": "", "error": "not_http_url"},
        }
    with httpx.Client(timeout=timeout_s, follow_redirects=True, headers={**DEFAULT_HEADERS, "Referer": referer}) as client:
        response = client.get(url)
    prefix = response.content[:8]
    pdf_confirmed = response.status_code == 200 and prefix.startswith(b"%PDF-")
    text_probe: Dict[str, Any] = {"extractable": False, "text_length": 0, "page_count": 0, "sample": "", "error": ""}
    if pdf_confirmed:
        try:
            text_result = extract_pdf_text(response.content)
            extracted_text = text_result["text"]
            text_probe = {
                "extractable": len(extracted_text) >= PUBSCHOLAR_MIN_EXTRACTED_TEXT_CHARS,
                "text_length": len(extracted_text),
                "page_count": text_result["page_count"],
                "sample": extracted_text[:240],
                "error": "",
            }
        except Exception as exc:
            text_probe["error"] = f"{type(exc).__name__}: {exc}"
    text_confirmed = bool(text_probe.get("extractable"))
    return {
        "status": "PASS" if pdf_confirmed and text_confirmed else "EXPECTED_DEGRADED",
        "file_url": url,
        "pdf_bytes_confirmed": pdf_confirmed,
        "text_extractable": text_confirmed,
        "text_probe": text_probe,
        "pdf_probe": {
            "status_code": response.status_code,
            "first_bytes_hex": prefix.hex(),
            "content_type": response.headers.get("content-type", ""),
            "content_length": response.headers.get("content-length", ""),
        },
    }


def extract_pdf_text(pdf_bytes: bytes, *, max_pages: int = 5) -> Dict[str, Any]:
    """Extract selectable PDF text for PubScholar full-text validation."""
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(pdf_bytes))
    page_count = len(reader.pages)
    text_chunks = []
    for page in list(reader.pages)[: max(1, int(max_pages or 1))]:
        text_chunks.append(page.extract_text() or "")
    text = re.sub(r"\s+", " ", "\n".join(text_chunks)).strip()
    return {"text": text, "page_count": page_count}


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
