"""SerpAPI Google Scholar metadata provider."""

from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, List

import httpx

from .models import AcademicSearchRequest, AcademicWork
from .quota import consume_quota, quota_status


class SerpApiScholarError(Exception):
    pass


class SerpApiScholarAuthError(SerpApiScholarError):
    pass


class SerpApiScholarRateLimitError(SerpApiScholarError):
    pass


class SerpApiScholarProvider:
    name = "serpapi_scholar"

    def __init__(self, endpoint: str = "https://serpapi.com/search.json", timeout: float | None = None) -> None:
        self.endpoint = os.environ.get("SERPAPI_ENDPOINT", endpoint).strip() or endpoint
        self.timeout = timeout if timeout is not None else _float_env("SERPAPI_TIMEOUT_S", 20.0)
        self.api_key = os.environ.get("SERPAPI_API_KEY", "").strip()
        self.monthly_limit = _int_env("KR_SERPAPI_MONTHLY_LIMIT", 250)
        self.daily_limit = _int_env("KR_ACADEMIC_SERPAPI_DAILY_LIMIT", 8)
        self.enabled_for_auto = _truthy(os.environ.get("KR_ACADEMIC_ENABLE_SERPAPI", "true"))
        self.daily_quota = quota_status(self.name, self.daily_limit)

    def status(self) -> Dict[str, Any]:
        return {
            "configured": bool(self.api_key),
            "available": bool(self.api_key) and not self.daily_quota.exhausted,
            "endpoint": self.endpoint,
            "requires_api_key": True,
            "api_key_configured": bool(self.api_key),
            "enabled_for_auto": self.enabled_for_auto,
            "monthly_limit": self.monthly_limit,
            "daily_limit": self.daily_quota.limit,
            "daily_used": self.daily_quota.used,
            "daily_remaining": self.daily_quota.to_dict()["remaining"],
            "daily_exhausted": self.daily_quota.exhausted,
            "usage_counter_path": self.daily_quota.path,
            "source": "Google Scholar via SerpAPI",
            "coverage": "global_scholar_metadata",
            "license_note": "Third-party Scholar SERP API; metadata only, no full-text scraping.",
        }

    def search(self, request: AcademicSearchRequest) -> List[AcademicWork]:
        if not self.api_key:
            raise SerpApiScholarAuthError("SERPAPI_API_KEY is not configured")
        quota_before = quota_status(self.name, self.daily_limit)
        if quota_before.exhausted:
            raise SerpApiScholarRateLimitError(
                f"SerpAPI daily academic limit exhausted ({quota_before.used}/{quota_before.limit})"
            )
        limit = max(1, min(int(request.limit or 5), 20))
        params = {
            "engine": "google_scholar",
            "q": request.query,
            "api_key": self.api_key,
            "num": limit,
            "hl": str(request.options.get("hl") or os.environ.get("SERPAPI_SCHOLAR_HL") or "zh-CN"),
        }
        if request.options.get("as_ylo"):
            params["as_ylo"] = request.options["as_ylo"]
        if request.options.get("as_yhi"):
            params["as_yhi"] = request.options["as_yhi"]
        if request.options.get("start"):
            params["start"] = request.options["start"]
        with httpx.Client(timeout=self.timeout, headers={"User-Agent": "KnowledgeRadar/academic-pilot"}) as client:
            try:
                response = client.get(self.endpoint, params=params)
                if response.status_code in {401, 403}:
                    raise SerpApiScholarAuthError(f"SerpAPI auth failed (HTTP {response.status_code})")
                if response.status_code == 429:
                    raise SerpApiScholarRateLimitError("SerpAPI rate limited (HTTP 429)")
                response.raise_for_status()
                data = response.json()
            except (SerpApiScholarAuthError, SerpApiScholarRateLimitError):
                raise
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise SerpApiScholarError(str(exc)) from exc
            except Exception as exc:
                raise SerpApiScholarError(str(exc)) from exc
        if data.get("error"):
            message = str(data.get("error") or "")
            if "account" in message.lower() or "api key" in message.lower() or "verify" in message.lower():
                raise SerpApiScholarAuthError(message)
            if "limit" in message.lower() or "quota" in message.lower():
                raise SerpApiScholarRateLimitError(message)
            raise SerpApiScholarError(message)
        consume_quota(self.name, self.daily_limit)
        return [self._work_from_result(item) for item in data.get("organic_results") or [] if isinstance(item, dict)]

    def _work_from_result(self, item: Dict[str, Any]) -> AcademicWork:
        publication = item.get("publication_info") if isinstance(item.get("publication_info"), dict) else {}
        resources = item.get("resources") if isinstance(item.get("resources"), list) else []
        resource_url = ""
        for resource in resources:
            if isinstance(resource, dict) and resource.get("link"):
                resource_url = str(resource.get("link") or "")
                break
        inline_links = item.get("inline_links") if isinstance(item.get("inline_links"), dict) else {}
        authors = _authors(publication)
        summary = str(publication.get("summary") or "")
        return AcademicWork(
            title=str(item.get("title") or ""),
            url=str(item.get("link") or resource_url or ""),
            authors=authors[:12],
            year=_year(summary),
            doi="",
            abstract=str(item.get("snippet") or "")[:2000],
            source="Google Scholar via SerpAPI",
            oa_status="unknown",
            license="",
            source_database=self.name,
            access_mode="serp_metadata",
            full_text_status="metadata_only",
            provider_confidence=0.7,
            verification_status="unverified",
            license_scope="unknown",
            raw={
                "result_id": item.get("result_id"),
                "position": item.get("position"),
                "publication_summary": summary,
                "cited_by": inline_links.get("cited_by"),
                "related_pages_link": inline_links.get("related_pages_link"),
                "resources": resources[:3],
                "search_metadata": {
                    "provider": "serpapi",
                    "engine": "google_scholar",
                    "retrieved_at_epoch": time.time(),
                },
            },
        )


def _authors(publication: Dict[str, Any]) -> List[str]:
    raw_authors = publication.get("authors")
    if isinstance(raw_authors, list):
        names = []
        for author in raw_authors:
            if isinstance(author, dict) and author.get("name"):
                names.append(str(author.get("name")))
        return names
    summary = str(publication.get("summary") or "")
    if " - " not in summary:
        return []
    return [name.strip() for name in summary.split(" - ", 1)[0].split(",") if name.strip()]


def _year(value: str) -> int | None:
    match = re.search(r"\b(19|20)\d{2}\b", value or "")
    if not match:
        return None
    try:
        return int(match.group(0))
    except Exception:
        return None


def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


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
