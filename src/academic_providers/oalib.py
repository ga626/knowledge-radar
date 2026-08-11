"""OALib Chinese/English OA provider."""

from __future__ import annotations

import re
from typing import List
from urllib.parse import urlencode, urljoin

import httpx

from .chinese_open_access import ChineseOpenAccessProvider, DEFAULT_HEADERS, OpenAccessPlatformConfig
from .models import AcademicSearchRequest, AcademicWork


OALIB_HEADERS = {
    **DEFAULT_HEADERS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Connection": "keep-alive",
    "Referer": "https://www.oalib.com/",
    "Upgrade-Insecure-Requests": "1",
}


class OalibProvider(ChineseOpenAccessProvider):
    name = "oalib"
    config = OpenAccessPlatformConfig(
        name="oalib",
        display_name="OALib",
        homepage="https://www.oalib.com/",
        status="degraded",
        available=False,
        auto_enabled=False,
        full_text_access="session_search_pdf_landing_not_direct_pdf",
        coverage="OA journals and articles, mixed English/Chinese coverage",
        stable=False,
        degraded_reason=(
            "homepage session search can work with browser-like headers, but direct homepage probes can return anti-automation "
            "HTML and search results resolve to external PaperDownload.aspx landing pages rather than verified PDF bytes"
        ),
        failure_category="pdf_landing_not_direct_pdf",
        pdf_url_markers=(".pdf", "download"),
    )

    def search(self, request: AcademicSearchRequest) -> List[AcademicWork]:
        query = str(request.query or "").strip()
        if not query:
            return super().search(request)

        limit = max(1, min(int(request.limit or 5), 20))
        seen = set()
        with httpx.Client(timeout=self.timeout_s, follow_redirects=True, headers=OALIB_HEADERS) as client:
            search_url = self._resolve_session_search_url(client)
            if search_url:
                url = f"{search_url}?{urlencode({'kw': query})}"
                results = self._collect_from_url(client, url, request, seen, limit)
                if results:
                    return results[:limit]
            return super().search(request)

    def search_urls(self, request: AcademicSearchRequest):
        return (self.config.homepage,)

    def _make_work(self, *, title: str, url: str, request: AcademicSearchRequest, full_text_status: str, confidence: float, raw: dict) -> AcademicWork:
        if title.strip().lower() in {"[pdf]", "pdf"}:
            title = "OALib PDF full-text result"
        if "paperdownload.aspx" in url.lower():
            full_text_status = "open_landing_page"
        return super()._make_work(
            title=title,
            url=url,
            request=request,
            full_text_status=full_text_status,
            confidence=confidence,
            raw=raw,
        )

    def _resolve_session_search_url(self, client: httpx.Client) -> str:
        """OALib search requires the jsessionid-bearing form action from the homepage."""
        try:
            response = client.get(self.config.homepage)
            response.raise_for_status()
        except Exception:
            return ""
        match = re.search(r"<form\b[^>]*\bname=[\"']quicksearch[\"'][^>]*\baction=[\"']([^\"']+)[\"']", response.text, re.I)
        if not match:
            match = re.search(r"<form\b[^>]*\baction=[\"']([^\"']*search[^\"']*)[\"']", response.text, re.I)
        if not match:
            return ""
        action = match.group(1).strip()
        if action.startswith("./"):
            base = str(response.url)
            if not base.endswith("/"):
                base += "/"
            return base + action[2:]
        return urljoin(str(response.url), action)
