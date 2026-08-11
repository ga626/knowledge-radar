"""ar5iv HTML full-text enhancer for arXiv works."""

from __future__ import annotations

import re
from typing import Any, Dict, List

import httpx

from .arxiv import ArxivError, ArxivProvider
from .models import AcademicSearchRequest, AcademicWork


class Ar5ivError(Exception):
    pass


class Ar5ivProvider:
    name = "ar5iv"

    def __init__(self, endpoint: str = "https://ar5iv.labs.arxiv.org/html", timeout: float = 8.0, arxiv_provider: ArxivProvider | None = None) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.arxiv_provider = arxiv_provider or ArxivProvider()

    def status(self) -> Dict[str, Any]:
        return {
            "configured": True,
            "available": True,
            "endpoint": self.endpoint,
            "requires_api_key": False,
            "auto_enabled": True,
            "access_mode": "public_html_enhancer",
            "degraded_reason": "Best-effort arXiv HTML conversion; individual papers may be missing, stale, or slow.",
        }

    def search(self, request: AcademicSearchRequest) -> List[AcademicWork]:
        direct_id = _arxiv_id_from_text(request.query)
        if direct_id:
            html_url = f"{self.endpoint}/{direct_id}"
            if _html_available(html_url, self.timeout):
                return [_direct_ar5iv_work(html_url, direct_id)]
        try:
            arxiv_items = self.arxiv_provider.search(request)
        except ArxivError as exc:
            raise Ar5ivError(str(exc)) from exc
        works: List[AcademicWork] = []
        for item in arxiv_items:
            arxiv_id = _arxiv_id(item)
            if not arxiv_id:
                continue
            html_url = f"{self.endpoint}/{arxiv_id}"
            available = _html_available(html_url, self.timeout)
            if not available:
                continue
            works.append(_with_ar5iv_html(item, html_url, arxiv_id))
        return works


def _with_ar5iv_html(item: AcademicWork, html_url: str, arxiv_id: str) -> AcademicWork:
    raw = dict(item.raw or {})
    raw["arxiv_id"] = arxiv_id
    raw["ar5iv_html_url"] = html_url
    raw["source_provider"] = item.source_database or "arxiv"
    return AcademicWork(
        title=item.title,
        url=html_url,
        authors=list(item.authors),
        year=item.year,
        doi=item.doi,
        abstract=item.abstract,
        source=item.source or "arXiv via ar5iv",
        oa_status=item.oa_status or "green",
        license=item.license,
        source_database="ar5iv",
        access_mode="public_html_enhancer",
        full_text_status="html_fulltext",
        provider_confidence=min(0.9, max(float(item.provider_confidence or 0.0), 0.78)),
        verification_status=item.verification_status,
        citation_export_formats=list(item.citation_export_formats),
        license_scope=item.license_scope or "open",
        raw=raw,
    )


def _direct_ar5iv_work(html_url: str, arxiv_id: str) -> AcademicWork:
    return AcademicWork(
        title=f"arXiv:{arxiv_id}",
        url=html_url,
        authors=[],
        source="arXiv via ar5iv",
        oa_status="green",
        source_database="ar5iv",
        access_mode="public_html_enhancer",
        full_text_status="html_fulltext",
        provider_confidence=0.78,
        verification_status="arxiv_id_matched",
        license_scope="open",
        raw={"arxiv_id": arxiv_id, "ar5iv_html_url": html_url, "source_provider": "ar5iv_direct_id"},
    )


def _arxiv_id(item: AcademicWork) -> str:
    for value in [item.url, (item.raw or {}).get("pdf_url"), item.title]:
        arxiv_id = _arxiv_id_from_text(str(value or ""))
        if arxiv_id:
            return arxiv_id
    return ""


def _arxiv_id_from_text(text: str) -> str:
    match = re.search(r"(?:arxiv:|arxiv\.org/(?:abs|pdf)/)?(\d{4}\.\d{4,5})(?:v\d+)?", str(text or ""), flags=re.IGNORECASE)
    if match:
        return match.group(1)
    return ""


def _html_available(url: str, timeout: float) -> bool:
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": "KnowledgeRadar/academic-ar5iv"}) as client:
            response = client.get(url, headers={"User-Agent": "KnowledgeRadar/academic-ar5iv", "Range": "bytes=0-2048"})
    except Exception:
        return False
    content_type = str(response.headers.get("content-type") or "").lower()
    return response.status_code in {200, 206} and "html" in content_type
