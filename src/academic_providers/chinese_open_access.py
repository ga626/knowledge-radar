"""Chinese open full-text academic provider helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
import base64
import html
import re
from typing import Iterable, List, Sequence
from urllib.parse import quote, urljoin

import httpx

from .models import AcademicSearchRequest, AcademicWork


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
}


@dataclass(frozen=True)
class OpenAccessPlatformConfig:
    name: str
    display_name: str
    homepage: str
    status: str = "degraded"
    available: bool = False
    auto_enabled: bool = False
    access_mode: str = "open_full_text"
    full_text_access: str = "unknown"
    coverage: str = ""
    stable: bool = False
    degraded_reason: str = ""
    failure_category: str = ""
    requires_login: bool = False
    login_url: str = ""
    manual_action: str = ""
    direct_pdf_samples: Sequence[str] = field(default_factory=tuple)
    landing_samples: Sequence[str] = field(default_factory=tuple)
    pdf_url_markers: Sequence[str] = field(default_factory=lambda: (".pdf", "type=pdf", "attachtype=pdf", "download"))


class ChineseOpenAccessProvider:
    """Best-effort HTML/PDF provider for open Chinese academic sources."""

    name = ""
    config: OpenAccessPlatformConfig
    timeout_s = 8.0
    max_pages = 3

    def status(self) -> dict:
        config = self.config
        return {
            "configured": True,
            "available": config.available,
            "requires_api_key": False,
            "network": "public_web",
            "status": config.status,
            "auto_enabled": config.auto_enabled,
            "access_mode": config.access_mode,
            "full_text_access": config.full_text_access,
            "coverage": config.coverage,
            "stable": config.stable,
            "homepage": config.homepage,
            "degraded_reason": config.degraded_reason,
            "failure_category": config.failure_category,
            "requires_login": config.requires_login,
            "login_url": config.login_url,
            "manual_action": config.manual_action,
        }

    def search(self, request: AcademicSearchRequest) -> List[AcademicWork]:
        limit = max(1, min(int(request.limit or 5), 20))
        works: List[AcademicWork] = []
        seen = set()
        with httpx.Client(timeout=self.timeout_s, follow_redirects=True, headers=DEFAULT_HEADERS) as client:
            for url in list(self.search_urls(request))[: self.max_pages]:
                works.extend(self._collect_from_url(client, url, request, seen, limit))
                if len(works) >= limit:
                    return works[:limit]
            if self.config.available:
                for url in self.config.direct_pdf_samples:
                    work = self._make_work(
                        title=f"{self.config.display_name} open full-text sample",
                        url=url,
                        request=request,
                        full_text_status="direct_pdf",
                        confidence=0.72,
                        raw={"fallback_sample": True},
                    )
                    if work.url not in seen:
                        works.append(work)
                        seen.add(work.url)
                    if len(works) >= limit:
                        break
        return works[:limit]

    def search_urls(self, request: AcademicSearchRequest) -> Sequence[str]:
        return (self.config.homepage,)

    def _collect_from_url(
        self,
        client: httpx.Client,
        url: str,
        request: AcademicSearchRequest,
        seen: set,
        limit: int,
    ) -> List[AcademicWork]:
        try:
            response = client.get(url)
            response.raise_for_status()
        except Exception:
            return []

        content_type = response.headers.get("content-type", "").lower()
        if "pdf" in content_type or response.content[:5] == b"%PDF-":
            if response.url and str(response.url) not in seen:
                seen.add(str(response.url))
                return [
                    self._make_work(
                        title=f"{self.config.display_name} PDF",
                        url=str(response.url),
                        request=request,
                        full_text_status="direct_pdf",
                        confidence=0.9,
                        raw={"source_url": url, "content_type": content_type},
                    )
                ]
            return []

        text = response.text
        results: List[AcademicWork] = []
        for link_url, label in self._extract_links(text, str(response.url)):
            if link_url in seen:
                continue
            if not self._looks_like_full_text_link(link_url, label):
                continue
            seen.add(link_url)
            results.append(
                self._make_work(
                    title=label or f"{self.config.display_name} full-text link",
                    url=link_url,
                    request=request,
                    full_text_status="direct_pdf" if self._looks_like_pdf_url(link_url, label) else "open_landing_page",
                    confidence=0.78,
                    raw={"source_url": url, "link_label": label},
                )
            )
            if len(results) >= limit:
                break
        return results

    def _extract_links(self, text: str, base_url: str) -> Iterable[tuple[str, str]]:
        pattern = re.compile(r"<a\b[^>]*?href=[\"'](?P<href>[^\"']+)[\"'][^>]*>(?P<label>.*?)</a>", re.I | re.S)
        for match in pattern.finditer(text):
            href = html.unescape(match.group("href")).strip()
            if not href or href.startswith(("javascript:", "#", "mailto:")):
                continue
            label = re.sub(r"<[^>]+>", " ", match.group("label"))
            label = html.unescape(re.sub(r"\s+", " ", label)).strip()
            yield urljoin(base_url, href), label

    def _looks_like_full_text_link(self, url: str, label: str) -> bool:
        lowered_label = label.lower()
        if any(marker in lowered_label for marker in ["pdf", "full paper", "全文", "下载"]):
            return True
        if re.search(r"\bdownload\b", lowered_label):
            return True
        return self._looks_like_pdf_url(url, label)

    def _looks_like_pdf_url(self, url: str, label: str = "") -> bool:
        lowered = f"{url} {label}".lower()
        return any(marker.lower() in lowered for marker in self.config.pdf_url_markers)

    def _make_work(
        self,
        *,
        title: str,
        url: str,
        request: AcademicSearchRequest,
        full_text_status: str,
        confidence: float,
        raw: dict,
    ) -> AcademicWork:
        return AcademicWork(
            title=_clean_title(title) or f"{self.config.display_name} result",
            url=url,
            source=self.config.display_name,
            oa_status="open" if full_text_status in {"direct_pdf", "open_landing_page"} else "",
            source_database=self.config.name,
            access_mode=self.config.access_mode,
            full_text_status=full_text_status,
            provider_confidence=confidence,
            verification_status="provider_link_extracted",
            license_scope="open_or_platform_terms",
            degraded_reason="" if self.config.available else self.config.degraded_reason,
            raw={"query": request.query, **raw},
        )


def encoded_nssd_search_url(query: str) -> str:
    q = str(query or "").strip()
    expression = f'(IKTE="{q}" OR IKPYTE="{q}" OR IKST="{q}" OR IKET="{q}" OR IKSE="{q}")'
    search = base64.b64encode(expression.encode("utf-8")).decode("ascii")
    search_name = base64.b64encode(f'题名/关键词="{q}"'.encode("utf-8")).decode("ascii")
    return (
        "https://www.ncpssd.org/Literature/articlelist?"
        f"sType=0&search={quote(search)}&searchname={quote(search_name)}&nav=0&showBack=true"
    )


def _clean_title(value: str) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"\s+", " ", text).strip(" -|")
    if len(text) > 180:
        return text[:177].rstrip() + "..."
    return text
