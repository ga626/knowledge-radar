"""Concrete generic web search providers."""

from __future__ import annotations

import os
from runtime.search_configuration import config_value
from typing import Any, Dict, List

import httpx

from .models import SearchProviderResult, WebSearchRequest, utc_now_iso
from .http_client import request_json


class SearchProviderError(Exception):
    def __init__(self, provider: str, message: str, *, error_type: str = "provider_error", status_code: int | None = None, retry_after: str = "") -> None:
        super().__init__(message)
        self.provider = provider
        self.message = message
        self.error_type = error_type
        self.status_code = status_code
        self.retry_after = retry_after

    def to_dict(self) -> Dict[str, str]:
        return {'provider': self.provider, 'type': self.error_type, 'message': self.message,
                'status_code': self.status_code, 'retry_after': self.retry_after,
                'retryable': self.error_type in {'rate_limited','upstream_error'},
                'next_action': 'Correct configuration/constraints or choose an authorized alternative; unknown sent requests are not automatically replayed.'}


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
        self.api_key = api_key or config_value("TAVILY_API_KEY", "")
        self.timeout = timeout
        self.endpoint = config_value("TAVILY_SEARCH_ENDPOINT", "https://api.tavily.com/search")

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
            from runtime.paid_execution import paid_send, PaidAdmissionError
            import json
            with paid_send("tavily", {k: v for k, v in payload.items() if k != "api_key"},
                           bound=len(json.dumps(payload).encode("utf-8"))):
                data = request_json("POST", self.endpoint, timeout=self.timeout, json=payload)
        except PaidAdmissionError as exc:
            raise SearchProviderError(self.name, exc.error_code, error_type="budget_blocked") from exc
        except Exception as exc:
            raise _request_error(self.name, exc) from exc

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
        self.api_key = api_key or config_value("BRAVE_SEARCH_API_KEY", "")
        self.timeout = timeout
        self.endpoint = config_value("BRAVE_SEARCH_ENDPOINT", "https://api.search.brave.com/res/v1/web/search")

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
            from runtime.paid_execution import paid_send, PaidAdmissionError
            import json
            with paid_send("brave", params, bound=len(json.dumps(params).encode("utf-8"))):
                data = request_json("GET", self.endpoint, timeout=self.timeout, headers=headers, params=params)
        except PaidAdmissionError as exc:
            raise SearchProviderError(self.name, exc.error_code, error_type="budget_blocked") from exc
        except Exception as exc:
            raise _request_error(self.name, exc) from exc

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
        self.api_key = api_key or config_value("EXA_API_KEY", "")
        self.timeout = timeout
        self.endpoint = config_value("EXA_SEARCH_ENDPOINT", "https://api.exa.ai/search")

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

        if request.freshness:
            from datetime import datetime, timedelta, timezone
            days = {'day': 1, 'week': 7, 'month': 30, 'year': 365}[request.freshness]
            payload['startPublishedDate'] = (datetime.now(timezone.utc)-timedelta(days=days)).isoformat()
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
        }
        try:
            from runtime.paid_execution import paid_send, PaidAdmissionError
            import json
            with paid_send("exa", payload, bound=len(json.dumps(payload).encode("utf-8"))):
                data = request_json("POST", self.endpoint, timeout=self.timeout, headers=headers, json=payload)
        except PaidAdmissionError as exc:
            raise SearchProviderError(self.name, exc.error_code, error_type="budget_blocked") from exc
        except Exception as exc:
            raise _request_error(self.name, exc) from exc

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
        self.api_key = api_key or config_value("ANYSEARCH_API_KEY", "")
        self.timeout = timeout
        self.endpoint = config_value("ANYSEARCH_SEARCH_ENDPOINT", "https://api.anysearch.com/v1/search")
        self.endpoint_explicit = bool(config_value("ANYSEARCH_SEARCH_ENDPOINT"))

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
            "role": "preferred",
            "degraded_ok": True,
            "notes": "AnySearch is the preferred configurable discovery route; explicit provider smoke may be EXPECTED_DEGRADED when the endpoint is absent, refused, or circuit-open.",
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
            from runtime.paid_execution import paid_send, PaidAdmissionError
            import json
            admission = paid_send("anysearch", payload, bound=len(json.dumps(payload).encode("utf-8")), endpoint=self.endpoint)
            with admission:
                data = request_json("POST", self.endpoint, timeout=self.timeout, headers=headers, json=payload)
        except PaidAdmissionError as exc:
            raise SearchProviderError(self.name, exc.error_code, error_type="budget_blocked") from exc
        except Exception as exc:
            raise _request_error(self.name, exc) from exc

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
            snippet = str(item.get("snippet") or item.get("description") or item.get("content") or "")
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


def _request_error(provider, exc):
    from runtime.execution_context import ExecutionStopped
    if isinstance(exc, ExecutionStopped):
        raise exc
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        kind = {401:'authentication_required',403:'access_forbidden',404:'not_found',429:'rate_limited',451:'legal_restriction'}.get(status, 'upstream_error' if status >= 500 else 'http_error')
        return SearchProviderError(provider, f'HTTP {status}', error_type=kind,
            status_code=status, retry_after=exc.response.headers.get('retry-after', ''))
    if isinstance(exc, PermissionError):
        return SearchProviderError(provider, str(exc), error_type='permission_revoked')
    if isinstance(exc, httpx.TimeoutException):
        return SearchProviderError(provider, 'Request timed out; sent outcome may be unknown', error_type='timeout_unknown_outcome')
    return SearchProviderError(provider, type(exc).__name__, error_type='transport_error')


class DdgsSearchProvider(BaseSearchProvider):
    name = 'ddgs'

    def __init__(self, timeout=12.0):
        self.timeout = timeout

    def available(self):
        from importlib.util import find_spec
        return find_spec('ddgs') is not None

    def status(self):
        return {'configured': True, 'available': self.available(), 'degraded_ok': True,
                'status': 'library_ready' if self.available() else 'component_missing',
                'notes': 'Search-only library; no daemon, no content extraction, no auto backend fallback.'}

    def search(self, request):
        from runtime.search_configuration import check_search_send
        check_search_send()
        if not self.available():
            raise SearchProviderError(self.name, 'DDGS component is missing', error_type='component_missing')
        from ddgs.ddgs import DDGS
        from ddgs.engines import ENGINES
        backend = str(request.options.get('ddgs_backend') or config_value('DDGS_BACKENDS', 'duckduckgo'))
        allowed = set(ENGINES.get('text', {}))
        if backend not in allowed or backend in {'auto','all'}:
            raise SearchProviderError(self.name, 'Choose one supported backend: '+', '.join(sorted(allowed)), error_type='invalid_backend')
        region = str(request.options.get('region') or config_value('DDGS_REGION') or
                     {'zh':'cn-zh', 'zh-CN':'cn-zh', 'en':'us-en'}.get(request.language, 'wt-wt'))
        from runtime.proxy_config import get_httpx_proxy
        # One fixed engine means one worker already. Do not mutate the library's
        # process-global DDGS.threads while other tasks might be searching.
        engine_request_status = []
        send_failures = []
        from contextvars import copy_context
        send_context = copy_context()
        try:
            client = DDGS(proxy=get_httpx_proxy(), timeout=self.timeout)
            # Per-instance observation: the pinned DDGS library discards every
            # non-200 response in BaseSearchEngine.request. Preserve its status
            # without a global monkeypatch or another request.
            engine = client._get_engines('text', backend)[0]
            request_http = engine.http_client.request
            def observed_request(*args, **kwargs):
                try:
                    send_context.run(check_search_send)
                except Exception as exc:
                    send_failures.append(exc)
                    raise
                response = request_http(*args, **kwargs)
                engine_request_status.append(response.status_code)
                return response
            engine.http_client.request = observed_request
            rows = client.text(request.query,
                backend=backend, region=region, max_results=_clean_limit(request.limit),
                timelimit={'day':'d','week':'w','month':'m','year':'y'}.get(request.freshness))
        except Exception as exc:
            from runtime.execution_context import ExecutionStopped
            if send_failures:
                raise _request_error(self.name, send_failures[0]) from send_failures[0]
            if isinstance(exc, ExecutionStopped):
                raise
            from ddgs.exceptions import TimeoutException, RatelimitException
            status = next((s for s in engine_request_status if s != 200), None)
            if status is not None:
                kind = {202:'upstream_challenge', 403:'access_forbidden',429:'rate_limited',
                        451:'legal_restriction'}.get(status,'upstream_error' if status >= 500 else 'upstream_http_error')
                raise SearchProviderError(self.name,
                    f'{backend} returned HTTP {status}; no automatic retry or backend rotation.',
                    error_type=kind, status_code=status) from exc
            kind = ('timeout_unknown_outcome' if isinstance(exc, TimeoutException) else
                    'rate_limited' if isinstance(exc, RatelimitException) else 'upstream_no_usable_results')
            raise SearchProviderError(self.name, type(exc).__name__, error_type=kind) from exc
        check_search_send()
        return [SearchProviderResult(title=str(x.get('title','')),url=str(x.get('href') or x.get('url') or ''),
            snippet=str(x.get('body') or '')[:800], source_provider=self.name, retrieved_at=utc_now_iso(),
            raw={'upstream_engine':getattr(ENGINES['text'][backend], 'provider', 'unknown'),
                 'discovery_backend':backend,'region':region,'discovery_only':True}) for x in rows if x.get('title') and (x.get('href') or x.get('url'))]
