"""CORE API metadata and open-access link provider."""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Iterable, List

import httpx

from .models import AcademicSearchRequest, AcademicWork, normalize_doi
from .quota import consume_quota, quota_status


class CoreError(Exception):
    pass


class CoreAuthError(CoreError):
    pass


class CoreRateLimitError(CoreError):
    pass


class CoreProvider:
    name = "core"

    def __init__(self, endpoint: str = "https://api.core.ac.uk/v3/search/works/", timeout: float | None = None) -> None:
        self.endpoint = os.environ.get("KR_CORE_ENDPOINT", endpoint).strip() or endpoint
        self.timeout = timeout if timeout is not None else _float_env("KR_CORE_TIMEOUT_S", 20.0)
        self.api_key = os.environ.get("KR_CORE_API_KEY", "").strip()
        self.daily_limit = _int_env("KR_ACADEMIC_CORE_DAILY_LIMIT", 50)
        self.daily_quota = quota_status(self.name, self.daily_limit)
        self.last_rate_limit_headers: Dict[str, str] = {}

    def status(self) -> Dict[str, Any]:
        configured = bool(self.api_key)
        return {
            "configured": configured,
            "available": configured and not self.daily_quota.exhausted,
            "endpoint": self.endpoint,
            "requires_api_key": True,
            "api_key_configured": configured,
            "auto_enabled": configured,
            "access_mode": "registered_public_api",
            "daily_limit": self.daily_quota.limit,
            "daily_used": self.daily_quota.used,
            "daily_remaining": self.daily_quota.to_dict()["remaining"],
            "daily_exhausted": self.daily_quota.exhausted,
            "usage_counter_path": self.daily_quota.path,
            "remote_rate_limit_headers": dict(self.last_rate_limit_headers),
            "source": "CORE API",
            "coverage": "global_open_access_metadata_and_links",
            "license_note": "Search and link discovery only; do not systematically harvest CORE-hosted PDF files.",
            "degraded_reason": "" if configured else "Set KR_CORE_API_KEY for CORE registered API access.",
        }

    def search(self, request: AcademicSearchRequest) -> List[AcademicWork]:
        if not self.api_key:
            raise CoreAuthError("KR_CORE_API_KEY is not configured")
        quota_before = quota_status(self.name, self.daily_limit)
        if quota_before.exhausted:
            raise CoreRateLimitError(f"CORE local daily academic limit exhausted ({quota_before.used}/{quota_before.limit})")
        limit = max(1, min(int(request.limit or 5), 20))
        headers = {"Authorization": f"Bearer {self.api_key}", "User-Agent": "KnowledgeRadar/academic-core"}
        params = {"q": request.query, "limit": limit}
        try:
            with httpx.Client(timeout=self.timeout, headers=headers, follow_redirects=True) as client:
                response = client.get(self.endpoint, params=params)
                self.last_rate_limit_headers = _rate_limit_headers(response.headers)
                if response.status_code in {401, 403}:
                    raise CoreAuthError(f"CORE auth failed (HTTP {response.status_code})")
                if response.status_code == 429:
                    raise CoreRateLimitError("CORE rate limited (HTTP 429)")
                response.raise_for_status()
                data = response.json()
        except (CoreAuthError, CoreRateLimitError):
            raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise CoreError(str(exc)) from exc
        except Exception as exc:
            raise CoreError(str(exc)) from exc
        consume_quota(self.name, self.daily_limit)
        items = data.get("results") if isinstance(data, dict) else []
        return [self._work_from_result(item) for item in items or [] if isinstance(item, dict)]

    def _work_from_result(self, item: Dict[str, Any]) -> AcademicWork:
        doi = normalize_doi(_first_string(item.get("doi")) or _doi_from_identifiers(item.get("identifiers")))
        links = item.get("links") if isinstance(item.get("links"), list) else []
        download_url = _first_string(item.get("downloadUrl")) or _link_by_type(links, "download")
        reader_url = _link_by_type(links, "reader")
        display_url = _link_by_type(links, "display")
        source_urls = _strings(item.get("sourceFulltextUrls"))
        full_text = _first_string(item.get("fullText"))
        url = display_url or reader_url or (f"https://doi.org/{doi}" if doi else "") or download_url or _first(source_urls)
        year = _safe_int(item.get("yearPublished")) or _year_from_date(_first_string(item.get("publishedDate")))
        journals = item.get("journals") if isinstance(item.get("journals"), list) else []
        source = _journal_title(journals) or _publisher_name(item.get("publisher")) or "CORE"
        has_fulltext_link = bool(download_url or reader_url or source_urls or full_text)
        return AcademicWork(
            title=_first_string(item.get("title")),
            url=url,
            authors=_authors(item.get("authors"))[:12],
            year=year,
            doi=doi,
            abstract=_clean_text(_first_string(item.get("abstract")))[:4000],
            source=source,
            oa_status="open" if has_fulltext_link else "unknown",
            license="",
            source_database=self.name,
            access_mode="registered_public_api",
            full_text_status="parsed_fulltext_available" if full_text else ("pdf_text_extractable" if download_url else ("oa_available" if has_fulltext_link else "metadata_only")),
            provider_confidence=0.82 if has_fulltext_link else 0.72,
            verification_status="doi_matched" if doi else ("core_id_matched" if item.get("id") else "unverified"),
            license_scope="open" if has_fulltext_link else "unknown",
            raw={
                "core_id": item.get("id"),
                "download_url": download_url,
                "reader_url": reader_url,
                "display_url": display_url,
                "source_fulltext_urls": source_urls[:5],
                "has_full_text": bool(full_text),
                "rate_limit": dict(self.last_rate_limit_headers),
            },
        )


def _rate_limit_headers(headers: httpx.Headers) -> Dict[str, str]:
    return {
        name: value
        for name in ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Retry-After", "Retry-After")
        if (value := headers.get(name))
    }


def _authors(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    names = []
    for author in value:
        if isinstance(author, dict):
            name = _first_string(author.get("name")) or " ".join(part for part in [_first_string(author.get("given")), _first_string(author.get("family"))] if part)
            if name:
                names.append(name)
        elif isinstance(author, str) and author.strip():
            names.append(author.strip())
    return names


def _doi_from_identifiers(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    for item in value:
        if isinstance(item, dict) and str(item.get("type") or "").lower() == "doi":
            return _first_string(item.get("identifier"))
        text = _first_string(item)
        if text.lower().startswith("doi:") or "10." in text:
            return text
    return ""


def _link_by_type(links: Iterable[Any], link_type: str) -> str:
    for link in links:
        if isinstance(link, dict) and str(link.get("type") or "").lower() == link_type:
            url = _first_string(link.get("url"))
            if url:
                return url
    return ""


def _strings(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        text = _first_string(item)
        if text:
            result.append(text)
    return result


def _first(values: List[str]) -> str:
    return values[0] if values else ""


def _first_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _publisher_name(value: Any) -> str:
    if isinstance(value, dict):
        return _first_string(value.get("name")) or _first_string(value.get("title"))
    return _first_string(value)


def _journal_title(value: List[Any]) -> str:
    for item in value:
        if isinstance(item, dict):
            title = _first_string(item.get("title"))
            if title:
                return title
    return ""


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _year_from_date(value: str) -> int | None:
    match = re.search(r"\b(19|20)\d{2}\b", value or "")
    if not match:
        return None
    return _safe_int(match.group(0))


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "").strip() or default)
    except Exception:
        return default


def _float_env(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, "").strip() or default)
    except Exception:
        return default
