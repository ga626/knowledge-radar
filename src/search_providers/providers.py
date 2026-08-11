"""Concrete generic web search providers."""

from __future__ import annotations

import os
from typing import Any, Dict, List

import httpx

from .models import SearchProviderResult, WebSearchRequest, utc_now_iso
from .http_client import request_json


class SearchProviderError(Exception):
    def __init__(self, provider: str, message: str, *, error_type: str = "provider_error") -> None:
        super().__init__(message)
        self.provider = provider
        self.message = message
        self.error_type = error_type

    def to_dict(self) -> Dict[str, str]:
        return {"provider": self.provider, "type": self.error_type, "message": self.message}


class BaseSearchProvider:
    name = "base"

    def available(self) -> bool:
        return bool(self.api_key) or self.endpoint_explicit

    def status(self) -> Dict[str, Any]:
        return {"configured": self.available(), "available": self.available()}

    def search(self, request: WebSearchRequest) -> List[SearchProviderResult]:
        raise NotImplementedError


def _clean_limit(limit: int) -> int:
    return max(1, min(int(limit or 5), 20))


def _freshness_to_tavily_range(freshness: str) -> str:
    value = (freshness or "").strip().lower()
    mapping = {
        "day": "day",
        "d": "day",
        "week": "week",
        "w": "week",
        "month": "month",
        "m": "month",
        "year": "year",
        "y": "year",
    }
    return mapping.get(value, "")


class TavilySearchProvider(BaseSearchProvider):
    name = "tavily"

    def __init__(self, api_key: str | None = None, timeout: float = 15.0) -> None:
        self.api_key = api_key or os.environ.get("TAVILY_API_KEY", "")
        self.timeout = timeout
        self.endpoint = os.environ.get("TAVILY_SEARCH_ENDPOINT", "https://api.tavily.com/search")

    def available(self) -> bool:
        return bool(self.api_key)

    def status(self) -> Dict[str, Any]:
        return {
            "configured": bool(self.api_key),
            "available": bool(self.api_key),
            "endpoint": self.endpoint,
        }

    def search(self, request: WebSearchRequest) -> List[SearchProviderResult]:
        if not self.available():
            raise SearchProviderError(self.name, "TAVILY_API_KEY is not configured", error_type="not_configured")

        limit = _clean_limit(request.limit)
        payload: Dict[str, Any] = {
            "api_key": self.api_key,
            "query": request.query,
            "max_results": limit,
            "search_depth": request.options.get("search_depth") or "basic",
            "include_answer": False,
            "include_raw_content": bool(request.include_raw_content),
        }
        time_range = _freshness_to_tavily_range(request.freshness)
        if time_range:
            payload["time_range"] = time_range
        topic = request.options.get("topic")
        if topic:
            payload["topic"] = topic
        include_domains = request.options.get("include_domains")
        if include_domains:
            payload["include_domains"] = include_domains
        exclude_domains = request.options.get("exclude_domains")
        if exclude_domains:
            payload["exclude_domains"] = exclude_domains

        try:
            data = request_json("POST", self.endpoint, timeout=self.timeout, json=payload)
        except Exception as exc:
            raise SearchProviderError(self.name, str(exc), error_type="request_failed") from exc

        now = utc_now_iso()
        body = data.get("data") if isinstance(data.get("data"), dict) else data
        result_items = body.get("results") or body.get("items")

        results = []
        for item in result_items or []:
            url = str(item.get("url") or "")
            title = str(item.get("title") or "")
            if not url or not title:
                continue
            raw = {k: v for k, v in item.items() if k not in {"raw_content"}}
            if request.include_raw_content and item.get("raw_content"):
                raw["raw_content"] = item.get("raw_content")
            score = item.get("score")
            try:
                score_value = float(score) if score is not None else None
            except (TypeError, ValueError):
                score_value = None
            results.append(
                SearchProviderResult(
                    title=title,
                    url=url,
                    snippet=str(item.get("content") or ""),
                    source_provider=self.name,
                    published_at=str(item.get("published_date") or item.get("publishedDate") or ""),
                    retrieved_at=now,
                    score=score_value,
                    raw=raw,
                )
            )
        return results


class BraveSearchProvider(BaseSearchProvider):
    name = "brave"

    def __init__(self, api_key: str | None = None, timeout: float = 12.0) -> None:
        self.api_key = api_key or os.environ.get("BRAVE_SEARCH_API_KEY", "")
        self.timeout = timeout
        self.endpoint = os.environ.get("BRAVE_SEARCH_ENDPOINT", "https://api.search.brave.com/res/v1/web/search")

    def available(self) -> bool:
        return bool(self.api_key)

    def status(self) -> Dict[str, Any]:
        configured = bool(self.api_key)
        return {
            "configured": configured,
            "available": configured,
            "endpoint": self.endpoint,
            "status": "available" if configured else "not_configured_optional",
            "role": "optional_quality_provider",
            "degraded_ok": not configured,
            "notes": "BRAVE_SEARCH_API_KEY is not configured; Brave is an optional quality provider, so explicit smoke failures without a key are EXPECTED_DEGRADED.",
        }

    def search(self, request: WebSearchRequest) -> List[SearchProviderResult]:
        if not self.available():
            raise SearchProviderError(self.name, "BRAVE_SEARCH_API_KEY is not configured", error_type="not_configured")

        params: Dict[str, Any] = {
            "q": request.query,
            "count": _clean_limit(request.limit),
            "text_decorations": "false",
            "summary": "false",
        }
        if request.language:
            params["search_lang"] = request.language
        freshness = (request.freshness or "").strip().lower()
        freshness_map = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}
        if freshness in freshness_map:
            params["freshness"] = freshness_map[freshness]

        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self.api_key,
        }
        try:
            data = request_json("GET", self.endpoint, timeout=self.timeout, headers=headers, params=params)
        except Exception as exc:
            raise SearchProviderError(self.name, str(exc), error_type="request_failed") from exc

        now = utc_now_iso()
        results = []
        for item in (data.get("web") or {}).get("results") or []:
            url = str(item.get("url") or "")
            title = str(item.get("title") or "")
            if not url or not title:
                continue
            results.append(
                SearchProviderResult(
                    title=title,
                    url=url,
                    snippet=str(item.get("description") or ""),
                    source_provider=self.name,
                    published_at=str(item.get("age") or ""),
                    retrieved_at=now,
                    raw=dict(item),
                )
            )
        return results


class ExaSearchProvider(BaseSearchProvider):
    name = "exa"

    def __init__(self, api_key: str | None = None, timeout: float = 15.0) -> None:
        self.api_key = api_key or os.environ.get("EXA_API_KEY", "")
        self.timeout = timeout
        self.endpoint = os.environ.get("EXA_SEARCH_ENDPOINT", "https://api.exa.ai/search")

    def available(self) -> bool:
        return bool(self.api_key)

    def status(self) -> Dict[str, Any]:
        configured = bool(self.api_key)
        return {
            "configured": configured,
            "available": configured,
            "endpoint": self.endpoint,
            "status": "available" if configured else "not_configured_optional",
            "role": "optional_quality_provider",
            "degraded_ok": not configured,
            "notes": "EXA_API_KEY is not configured; Exa is an optional quality provider, so explicit smoke failures without a key are EXPECTED_DEGRADED.",
        }

    def search(self, request: WebSearchRequest) -> List[SearchProviderResult]:
        if not self.available():
            raise SearchProviderError(self.name, "EXA_API_KEY is not configured", error_type="not_configured")

        payload: Dict[str, Any] = {
            "query": request.query,
            "numResults": _clean_limit(request.limit),
            "type": request.options.get("type") or "auto",
            "contents": {"text": bool(request.include_raw_content)},
        }
        include_domains = request.options.get("include_domains")
        if include_domains:
            payload["includeDomains"] = include_domains
        exclude_domains = request.options.get("exclude_domains")
        if exclude_domains:
            payload["excludeDomains"] = exclude_domains

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
        }
        try:
            data = request_json("POST", self.endpoint, timeout=self.timeout, headers=headers, json=payload)
        except Exception as exc:
            raise SearchProviderError(self.name, str(exc), error_type="request_failed") from exc

        now = utc_now_iso()
        result_items = data.get("results") or data.get("items")
        if not result_items and isinstance(data.get("data"), dict):
            result_items = data["data"].get("results") or data["data"].get("items")

        results = []
        for item in result_items or []:
            url = str(item.get("url") or "")
            title = str(item.get("title") or "")
            if not url or not title:
                continue
            raw = dict(item)
            snippet = str(item.get("text") or item.get("summary") or "")
            if not request.include_raw_content:
                raw.pop("text", None)
            score = item.get("score")
            try:
                score_value = float(score) if score is not None else None
            except (TypeError, ValueError):
                score_value = None
            results.append(
                SearchProviderResult(
                    title=title,
                    url=url,
                    snippet=snippet[:800],
                    source_provider=self.name,
                    published_at=str(item.get("publishedDate") or ""),
                    retrieved_at=now,
                    score=score_value,
                    raw=raw,
                )
            )
        return results


class AnySearchProvider(BaseSearchProvider):
    name = "anysearch"

    def __init__(self, api_key: str | None = None, timeout: float = 15.0) -> None:
        self.api_key = api_key or os.environ.get("ANYSEARCH_API_KEY", "")
        self.timeout = timeout
        self.endpoint = os.environ.get("ANYSEARCH_SEARCH_ENDPOINT", "https://api.anysearch.com/v1/search")
        self.endpoint_explicit = "ANYSEARCH_SEARCH_ENDPOINT" in os.environ

    def available(self) -> bool:
        return bool(self.api_key) or self.endpoint_explicit

    def status(self) -> Dict[str, Any]:
        configured = bool(self.api_key) or self.endpoint_explicit
        return {
            "configured": configured,
            "available": configured,
            "endpoint": self.endpoint,
            "anonymous_access": not bool(self.api_key),
            "status": "configured_unverified" if configured else "not_configured_optional",
            "role": "optional_fallback",
            "degraded_ok": True,
            "notes": "AnySearch is an optional/custom fallback; explicit provider smoke may be EXPECTED_DEGRADED when the endpoint is absent, refused, or circuit-open.",
        }

    def search(self, request: WebSearchRequest) -> List[SearchProviderResult]:
        if not self.available():
            raise SearchProviderError(
                self.name,
                "ANYSEARCH_API_KEY or ANYSEARCH_SEARCH_ENDPOINT is not configured",
                error_type="not_configured_optional",
            )

        payload: Dict[str, Any] = {
            "query": request.query,
            "max_results": _clean_limit(request.limit),
        }
        if request.language:
            payload["language"] = request.language
        if request.freshness:
            payload["constraint"] = {"freshness": request.freshness}
        for key in ("domains", "tags", "content_types", "zone", "providers"):
            value = request.options.get(key)
            if value:
                payload[key] = value

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            data = request_json("POST", self.endpoint, timeout=self.timeout, headers=headers, json=payload)
        except Exception as exc:
            raise SearchProviderError(self.name, str(exc), error_type="request_failed") from exc

        now = utc_now_iso()
        body = data.get("data") if isinstance(data.get("data"), dict) else data
        result_items = body.get("results") or body.get("items")

        results = []
        for item in result_items or []:
            url = str(item.get("url") or "")
            title = str(item.get("title") or "")
            if not url or not title:
                continue
            raw = dict(item)
            if not request.include_raw_content:
                raw.pop("raw_content", None)
                raw.pop("content", None)
            score = item.get("quality_score", item.get("score"))
            try:
                score_value = float(score) if score is not None else None
            except (TypeError, ValueError):
                score_value = None
            snippet = str(item.get("description") or item.get("content") or "")
            results.append(
                SearchProviderResult(
                    title=title,
                    url=url,
                    snippet=snippet[:800],
                    source_provider=self.name,
                    published_at=str(item.get("published_at") or ""),
                    retrieved_at=now,
                    score=score_value,
                    raw=raw,
                )
            )
        return results


class SearxngSearchProvider(BaseSearchProvider):
    name = "searxng"

    def __init__(self, base_url: str | None = None, timeout: float = 12.0) -> None:
        self.base_url = (base_url or os.environ.get("SEARXNG_BASE_URL", "")).rstrip("/")
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.base_url)

    def status(self) -> Dict[str, Any]:
        configured = bool(self.base_url)
        available = False
        detail = "SEARXNG_BASE_URL is not configured"
        if configured:
            try:
                with httpx.Client(timeout=3.0) as client:
                    response = client.get(f"{self.base_url}/search", params={"q": "health", "format": "json"})
                    response.raise_for_status()
                    data = response.json()
                available = isinstance(data.get("results"), list)
                detail = "search endpoint reachable" if available else "search endpoint returned no results field"
            except Exception as exc:
                detail = str(exc)
        return {
            "configured": configured,
            "available": available,
            "base_url": self.base_url,
            "detail": detail,
            "role": "optional_fallback",
            "degraded_ok": True,
            "status": "available" if available else "expected_degraded_optional",
            "notes": "SearXNG is a local/self-hosted optional fallback. A stopped daemon or refused localhost port is expected degraded when auto search has another working provider.",
        }

    def search(self, request: WebSearchRequest) -> List[SearchProviderResult]:
        if not self.available():
            raise SearchProviderError(self.name, "SEARXNG_BASE_URL is not configured", error_type="not_configured")

        params: Dict[str, Any] = {
            "q": request.query,
            "format": "json",
            "language": request.language or "auto",
        }
        if request.freshness:
            params["time_range"] = request.freshness

        try:
            data = request_json("GET", f"{self.base_url}/search", timeout=self.timeout, params=params)
        except Exception as exc:
            raise SearchProviderError(self.name, str(exc), error_type="request_failed") from exc

        now = utc_now_iso()
        results = []
        for item in (data.get("results") or [])[: _clean_limit(request.limit)]:
            url = str(item.get("url") or "")
            title = str(item.get("title") or "")
            if not url or not title:
                continue
            score = item.get("score")
            try:
                score_value = float(score) if score is not None else None
            except (TypeError, ValueError):
                score_value = None
            results.append(
                SearchProviderResult(
                    title=title,
                    url=url,
                    snippet=str(item.get("content") or ""),
                    source_provider=self.name,
                    published_at=str(item.get("publishedDate") or ""),
                    retrieved_at=now,
                    score=score_value,
                    raw=dict(item),
                )
            )
        return results
