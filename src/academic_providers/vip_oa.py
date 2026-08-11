"""VIP OA provider."""

from __future__ import annotations

import html
import json
import re
import time
from io import BytesIO
from typing import Any, Dict, List
from urllib.parse import parse_qs, quote, unquote, urlparse, urlunparse
from urllib.request import Request, urlopen

import websocket

from .chinese_open_access import ChineseOpenAccessProvider, OpenAccessPlatformConfig
from .models import AcademicSearchRequest, AcademicWork
from runtime.chrome_manager import _chrome_debug_port, _ensure_chrome_debugging, finish_chrome_automation


VIP_ONLINE_READING_SAMPLE_URL = "https://www.cqvip.com/onlinereading?type=1&lid=7203295942"
VIP_MAIN_SITE_SEARCH_URL = "https://www.cqvip.com/search?k={query}"
VIP_MIN_EXTRACTED_TEXT_CHARS = 500


class VipOpenAccessProvider(ChineseOpenAccessProvider):
    name = "vip_oa"
    config = OpenAccessPlatformConfig(
        name="vip_oa",
        display_name="维普OA",
        homepage="https://oa.cqvip.com/paper",
        status="available",
        available=True,
        auto_enabled=True,
        access_mode="logged_online_reading_pdf_viewer",
        full_text_access="logged_online_reading_pdf_text_confirmed",
        coverage="VIP/CQVIP main-site records with intelligent-reading PDF.js preview text extraction",
        stable=False,
        degraded_reason="",
        failure_category="",
        requires_login=True,
        login_url="https://www.cqvip.com/search?k=ai",
        manual_action=(
            "Run the silent auth probe first. Open the managed browser only when the probe reports NEEDS_INTERACTION, "
            "then rerun the main-site intelligent-reading probe; do not use download buttons."
        ),
        landing_samples=("https://oa.cqvip.com/paper", "https://www.cqvip.com/search?k=ai", VIP_ONLINE_READING_SAMPLE_URL),
        pdf_url_markers=(".pdf", "viewer.html?file=", "onlinereading", "智能阅读"),
    )

    def status(self) -> dict:
        status = super().status()
        status.update(
            {
                "network": "managed_browser_search_and_pdfjs_viewer",
                "notes": (
                    "VIP is admitted through the main-site intelligent-reading route, not the OA download endpoint. "
                    "Search/detail URLs expose resourceId/type; the verifier derives /onlinereading and extracts "
                    "selectable PDF text from the PDF.js file parameter."
                ),
                "validation_reason": "Provider available through logged CQVIP intelligent-reading PDF.js preview extraction.",
                "text_extraction": {
                    "method": "cqvip_detail_resourceid_to_online_reading_pdfjs_file_then_pypdf",
                    "confirmed_sample": VIP_ONLINE_READING_SAMPLE_URL,
                    "minimum_chars": VIP_MIN_EXTRACTED_TEXT_CHARS,
                    "download_button_used": False,
                },
                "quota_boundary": {
                    "download_button_used": False,
                    "reason": "The provider uses online-reading preview resources and does not invoke free-download controls.",
                },
            }
        )
        return status

    def search(self, request: AcademicSearchRequest) -> List[AcademicWork]:
        query = str(request.query or "").strip()
        if not query:
            return []
        limit = max(1, min(int(request.limit or 5), 20))
        records = self._search_with_managed_browser(query=query, limit=limit)
        return [self._work_from_search_result(record, request) for record in records][:limit]

    def search_urls(self, request: AcademicSearchRequest):
        query = quote(str(request.query or "").strip())
        if query:
            return (VIP_MAIN_SITE_SEARCH_URL.format(query=query), f"https://oa.cqvip.com/paper/search?keyword={query}", self.config.homepage)
        return (self.config.homepage,)

    def verify_article_fulltext(self, article_url: str) -> Dict[str, Any]:
        reading_url = vip_reading_url_from_detail_url(article_url) or str(article_url or "")
        if not reading_url:
            return {
                "status": "EXPECTED_DEGRADED",
                "article_url": article_url,
                "reason": "missing_vip_article_or_reading_url",
                "pdf_bytes_confirmed": False,
                "text_extractable": False,
            }
        try:
            summary = self._online_reading_summary(reading_url)
            file_urls = _viewer_file_urls(summary)
            if not file_urls:
                return {
                    "status": "EXPECTED_DEGRADED",
                    "article_url": article_url,
                    "reading_url": reading_url,
                    "reason": "vip_online_reading_viewer_file_missing",
                    "viewer_file_count": 0,
                    "pdf_bytes_confirmed": False,
                    "text_extractable": False,
                }
            pdf_result = _fetch_pdf_text_result(file_urls[0], referer=reading_url, max_pages=5)
            text_result = pdf_result.get("text_extraction") or {}
            text_len = int(text_result.get("sample_text_len") or 0)
            pdf_ok = bool(pdf_result.get("is_pdf"))
            text_ok = pdf_ok and text_len >= VIP_MIN_EXTRACTED_TEXT_CHARS
            parsed_file = urlparse(file_urls[0])
            return {
                "status": "PASS" if text_ok else "EXPECTED_DEGRADED",
                "article_url": article_url,
                "reading_url": reading_url,
                "viewer_file_count": len(file_urls),
                "file_url_host": parsed_file.netloc,
                "file_url_path_tail": parsed_file.path[-120:],
                "pdf_bytes_confirmed": pdf_ok,
                "text_extractable": text_ok,
                "text_probe": text_result,
                "pdf_probe": {
                    "status_code": pdf_result.get("status_code"),
                    "content_type": pdf_result.get("content_type"),
                    "content_length": pdf_result.get("content_length"),
                    "first_bytes_hex": pdf_result.get("first_bytes_hex"),
                },
            }
        except Exception as exc:
            return {
                "status": "EXPECTED_DEGRADED",
                "article_url": article_url,
                "reading_url": reading_url,
                "reason": f"{type(exc).__name__}: {exc}",
                "pdf_bytes_confirmed": False,
                "text_extractable": False,
            }

    def _search_with_managed_browser(self, *, query: str, limit: int) -> List[Dict[str, Any]]:
        if not _ensure_chrome_debugging("vip_oa", visible=False, detach=False):
            return []
        page = _CdpPage(_target_ws_url("vip_oa"))
        try:
            page.call("Runtime.enable")
            page.call("Page.enable")
            page.call("Network.enable")
            page.call("Page.navigate", {"url": VIP_MAIN_SITE_SEARCH_URL.format(query=quote(query))}, timeout=20)
            page.drain(5)
            value = page.eval(_vip_search_results_expression(limit=limit), timeout=30)
            if not isinstance(value, list):
                return []
            records = [record for record in value if isinstance(record, dict) and record.get("detail_url")]
            return records[:limit]
        finally:
            try:
                page.close()
            finally:
                finish_chrome_automation("vip_oa", reason="vip_oa_search")

    def _online_reading_summary(self, reading_url: str) -> Dict[str, Any]:
        if not _ensure_chrome_debugging("vip_oa", visible=False, detach=False):
            return {}
        page = _CdpPage(_target_ws_url("vip_oa"))
        try:
            page.call("Runtime.enable")
            page.call("Page.enable")
            page.call("Network.enable")
            page.call("Page.navigate", {"url": reading_url}, timeout=20)
            page.drain(6)
            value = page.eval(_online_reading_summary_expression(), timeout=25)
            return value if isinstance(value, dict) else {}
        finally:
            try:
                page.close()
            finally:
                finish_chrome_automation("vip_oa", reason="vip_oa_fulltext_verify")

    def _work_from_search_result(self, item: Dict[str, Any], request: AcademicSearchRequest) -> AcademicWork:
        detail_url = str(item.get("detail_url") or "").strip()
        reading_url = vip_reading_url_from_detail_url(detail_url)
        title = _clean_text(item.get("title") or "")
        return AcademicWork(
            title=title or "VIP/CQVIP article",
            url=detail_url or reading_url or self.config.homepage,
            authors=_authors(item.get("authors") or ""),
            year=_year(item.get("metadata") or item.get("snippet") or ""),
            doi=_doi(item.get("metadata") or item.get("snippet") or ""),
            abstract=_clean_text(item.get("snippet") or ""),
            source=_clean_text(item.get("source") or "VIP/CQVIP"),
            oa_status="open",
            source_database="vip_oa",
            access_mode="logged_online_reading_pdf_viewer",
            full_text_status="open_access_article_detail" if reading_url else "metadata_only",
            provider_confidence=0.84 if reading_url else 0.55,
            verification_status="vip_detail_to_online_reading_candidate" if reading_url else "vip_detail_without_reading_id",
            license_scope="platform_online_reading_terms",
            degraded_reason="" if reading_url else "VIP search result did not expose a resourceId/type route.",
            raw={
                "query": request.query,
                "detail_url": detail_url,
                "reading_url": reading_url,
                "resource_id": item.get("resource_id") or "",
                "type": item.get("type") or "",
                "availability_label": item.get("availability_label") or "",
                "download_button_used": False,
            },
        )


def vip_file_url_from_viewer_url(viewer_url: str) -> str:
    """Return the signed PDF URL embedded in CQVIP's PDF.js viewer URL."""

    parsed = urlparse(str(viewer_url or ""))
    file_values = parse_qs(parsed.query).get("file") or []
    if not file_values:
        return ""
    return file_values[0]


def vip_reading_url_from_detail_url(detail_url: str) -> str:
    """Derive CQVIP intelligent-reading URL from a main-site detail URL."""

    parsed = urlparse(str(detail_url or ""))
    if not parsed.scheme or not parsed.netloc:
        return ""
    if "/onlinereading" in parsed.path:
        return str(detail_url)
    query = parse_qs(parsed.query)
    resource_id = (query.get("resourceId") or query.get("resourceid") or [""])[0]
    resource_type = (query.get("type") or ["1"])[0] or "1"
    if not resource_id:
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 3 and parts[-2] in {"journal", "thesis", "conference", "standard"}:
            resource_id = parts[-1]
    if not resource_id:
        return ""
    return f"https://www.cqvip.com/onlinereading?type={quote(str(resource_type))}&lid={quote(str(resource_id))}"


def vip_ascii_url(url: str) -> str:
    """Convert CQVIP signed PDF IRI paths with Chinese filenames into request-safe URIs."""

    parsed = urlparse(str(url or ""))
    if not parsed.scheme or not parsed.netloc:
        return str(url or "")
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            quote(unquote(parsed.path), safe="/%"),
            parsed.params,
            quote(parsed.query, safe="=&?/%:+"),
            parsed.fragment,
        )
    )


def extract_pdf_text(pdf_bytes: bytes, *, max_pages: int = 3) -> str:
    """Extract text from CQVIP preview PDF bytes without relying on browser downloads."""

    return _extract_pdf_text_and_count(pdf_bytes, max_pages=max_pages)["text"]


def extract_pdf_text_result(pdf_bytes: bytes, *, max_pages: int = 3) -> Dict[str, Any]:
    """Extract text plus page metadata from CQVIP preview PDF bytes."""

    result = _extract_pdf_text_and_count(pdf_bytes, max_pages=max_pages)
    text = str(result["text"])
    return {
        "page_count": result["page_count"],
        "sample_text_len": len(text),
        "sample_text": " ".join(text.split())[:500],
    }


def _extract_pdf_text_and_count(pdf_bytes: bytes, *, max_pages: int = 3) -> Dict[str, Any]:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(pdf_bytes))
    pages = reader.pages[: max(1, int(max_pages or 1))]
    text = "\n".join(page.extract_text() or "" for page in pages)
    return {
        "text": text,
        "page_count": len(reader.pages),
    }


class _CdpPage:
    def __init__(self, ws_url: str) -> None:
        self.ws = websocket.create_connection(ws_url, timeout=20, suppress_origin=True)
        self.seq = 1

    def close(self) -> None:
        self.ws.close()

    def call(self, method: str, params: Dict[str, Any] | None = None, *, timeout: float = 45) -> Dict[str, Any]:
        msg_id = self.seq
        self.seq += 1
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = json.loads(self.ws.recv())
            if data.get("id") == msg_id:
                if data.get("error"):
                    raise RuntimeError(json.dumps(data["error"], ensure_ascii=False))
                return data.get("result") or {}
        raise TimeoutError(f"CDP call timed out: {method}")

    def drain(self, seconds: float = 2) -> None:
        deadline = time.time() + seconds
        old_timeout = self.ws.gettimeout()
        self.ws.settimeout(0.4)
        try:
            while time.time() < deadline:
                try:
                    self.ws.recv()
                except Exception:
                    pass
        finally:
            self.ws.settimeout(old_timeout)

    def eval(self, expression: str, *, timeout: float = 60) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
                "timeout": int(timeout * 1000),
            },
            timeout=timeout + 5,
        )
        return (result.get("result") or {}).get("value")


def _target_ws_url(platform: str) -> str:
    port = _chrome_debug_port(platform)
    with urlopen(f"http://127.0.0.1:{port}/json", timeout=8) as response:
        tabs = json.loads(response.read().decode("utf-8"))
    pages = [tab for tab in tabs if tab.get("type") == "page" and tab.get("webSocketDebuggerUrl")]
    for tab in pages:
        if "cqvip.com" in str(tab.get("url") or ""):
            return str(tab["webSocketDebuggerUrl"])
    if pages:
        return str(pages[0]["webSocketDebuggerUrl"])
    raise RuntimeError(f"No page target on CDP port {port}")


def _vip_search_results_expression(*, limit: int) -> str:
    limit_int = max(1, min(int(limit or 5), 20))
    return rf"""
(() => {{
  const norm = value => String(value || '').replace(/\s+/g, ' ').trim();
  const out = [];
  const seen = new Set();
  for (const link of [...document.querySelectorAll('a[href]')]) {{
    const href = new URL(link.getAttribute('href'), location.href).href;
    if (!/cqvip\.com\/doc\//i.test(href) || seen.has(href)) continue;
    seen.add(href);
    const container = link.closest('.result-list,.list-item,.detail-list,.paperItem,li,div') || link.parentElement || link;
    const text = norm(container.innerText || link.innerText || link.textContent);
    const title = norm(link.innerText || link.textContent) || text.split(' 作者')[0] || text.slice(0, 120);
    const url = new URL(href);
    const params = new URLSearchParams(url.search);
    out.push({{
      title,
      detail_url: href,
      resource_id: params.get('resourceId') || '',
      type: params.get('type') || '1',
      snippet: text.slice(0, 800),
      authors: text.match(/作者[:：]?\s*([^。；;]+)/)?.[1] || '',
      source: text.match(/来源[:：]?\s*([^。；;]+)/)?.[1] || '',
      metadata: text,
      availability_label: text.includes('OA开放获取') ? 'OA开放获取' : (text.includes('有全文') ? '有全文' : '')
    }});
    if (out.length >= {limit_int}) break;
  }}
  return out;
}})()
"""


def _online_reading_summary_expression() -> str:
    return r"""
(() => {
  const norm = value => String(value || '').replace(/\s+/g, ' ').trim();
  const bodyText = norm(document.body ? document.body.innerText : '');
  return {
    url: location.href,
    title: document.title,
    body_len: bodyText.length,
    body_sample: bodyText.slice(0, 800),
    iframes: [...document.querySelectorAll('iframe')].map(frame => frame.src).filter(Boolean),
    embeds: [...document.querySelectorAll('embed, object')].map(item => item.src || item.data || '').filter(Boolean),
    perf_resources: performance.getEntriesByType('resource')
      .map(item => ({name: item.name, initiatorType: item.initiatorType}))
      .filter(item => /pdf|viewer|onlinereading|imgv3|read|preview/i.test(item.name))
      .slice(-80)
  };
})()
"""


def _viewer_file_urls(summary: Dict[str, Any]) -> List[str]:
    urls: List[str] = []
    for value in list(summary.get("iframes") or []) + list(summary.get("embeds") or []):
        text = str(value or "")
        if "viewer.html" in text and "file=" in text:
            file_url = vip_file_url_from_viewer_url(text)
            if file_url:
                urls.append(file_url)
        elif ".pdf" in text.lower():
            urls.append(text)
    return urls


def _fetch_pdf_text_result(file_url: str, *, referer: str, max_pages: int = 5) -> Dict[str, Any]:
    request = Request(
        vip_ascii_url(file_url),
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": referer,
            "Accept": "application/pdf,*/*",
        },
    )
    with urlopen(request, timeout=90) as response:
        pdf_bytes = response.read()
        status_code = response.status
        content_type = response.headers.get("content-type") or ""
        content_length = response.headers.get("content-length") or str(len(pdf_bytes))
    text_result = extract_pdf_text_result(pdf_bytes, max_pages=max_pages)
    return {
        "status_code": status_code,
        "content_type": content_type,
        "content_length": content_length,
        "first_bytes_hex": pdf_bytes[:5].hex(),
        "is_pdf": pdf_bytes[:5] == b"%PDF-",
        "text_extraction": text_result,
    }


def _clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _authors(value: Any) -> List[str]:
    text = _clean_text(value)
    if not text:
        return []
    return [_clean_text(part) for part in re.split(r"[;,，；\s]+", text) if _clean_text(part)][:20]


def _year(value: Any) -> int | None:
    match = re.search(r"(19|20)\d{2}", str(value or ""))
    return int(match.group(0)) if match else None


def _doi(value: Any) -> str:
    match = re.search(r"10\.\d{4,9}/[^\s，。；;]+", str(value or ""), flags=re.I)
    return match.group(0).rstrip(").,，。；;") if match else ""
