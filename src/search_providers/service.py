"""Provider orchestration for generic web search."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List

from runtime.degradation import get_degradation_policy
from runtime.tool_trace import record_trace_child

from .aggregation import aggregate_results, coverage_decision
from .concurrency import compute_wave_concurrency
from .host import host_search_card_summary, host_search_providers
from .models import SearchProviderResult, WebSearchRequest, WebSearchResponse
from .planner import auto_search_plan, explicit_provider_plan, tavily_supplement_available
from .profile import profile_for, provider_profiles
from .providers import (
    AnySearchProvider,
    BaseSearchProvider,
    BraveSearchProvider,
    ExaSearchProvider,
    SearchProviderError,
    SearxngSearchProvider,
    TavilySearchProvider,
)
from .quota import SearchQuotaLedger, quota_summary
from .status_cache import provider_status_cache


def _default_provider_order() -> List[str]:
    configured = os.environ.get("KR_WEB_SEARCH_PROVIDERS", "").strip()
    if configured:
        return [item.strip().lower() for item in configured.split(",") if item.strip()]
    return ["tavily", "brave", "exa", "anysearch", "searxng"]


def _providers() -> Dict[str, BaseSearchProvider]:
    providers: Dict[str, BaseSearchProvider] = {
        "tavily": TavilySearchProvider(),
        "anysearch": AnySearchProvider(),
        "brave": BraveSearchProvider(),
        "exa": ExaSearchProvider(),
        "searxng": SearxngSearchProvider(),
    }
    providers.update(host_search_providers())
    return providers


def _load_provider_status() -> Dict[str, Dict[str, object]]:
    status = {}
    profiles = provider_profiles()
    for name, provider in _providers().items():
        row = provider.status()
        row["capability_profile"] = profiles.get(name) or profile_for(name)
        status[name] = row
    status["_quota"] = quota_summary()
    status["_host_search_cards"] = host_search_card_summary()
    return status


def provider_status(*, force_refresh: bool = False) -> Dict[str, Dict[str, object]]:
    status = provider_status_cache.get(_load_provider_status, force_refresh=force_refresh)
    status["_status_cache"] = provider_status_cache.summary()
    return status


def _strategy_metadata(strategy: Dict[str, object], *, selected: str, attempted: List[str], errors: List[dict]) -> Dict[str, object]:
    failed_attempts = [
        {
            "name": str(error.get("provider") or ""),
            "status": "failed",
            "error_type": str(error.get("type") or "unknown"),
            "detail": str(error.get("message") or ""),
        }
        for error in errors
    ]
    ok_attempts = [{"name": name, "status": "ok"} for name in attempted if name == selected]
    return {
        "strategy": strategy,
        "errors": errors,
        "selected_node": selected,
        "selected_strategy": selected,
        "fallback_count": max(0, len(attempted) - 1 if selected else len(attempted)),
        "attempts": [*failed_attempts, *ok_attempts],
        "failure_taxonomy": strategy.get("error_taxonomy", {}),
    }


def _stale_config_breaker_can_probe(provider: BaseSearchProvider, breaker: Dict[str, object]) -> bool:
    if not breaker.get("open"):
        return True
    reason = str(breaker.get("last_reason") or "").lower()
    if "not configured" not in reason and "api_key" not in reason:
        return False
    try:
        status = provider.status()
    except Exception:
        return False
    return bool(status.get("available"))


def _request_payload(request: WebSearchRequest) -> Dict[str, object]:
    return {
        "query": request.query,
        "limit": request.limit,
        "freshness": request.freshness,
        "language": request.language,
        "provider": request.provider,
        "include_raw_content": request.include_raw_content,
        "options": dict(request.options or {}),
    }


def _search_provider(request: WebSearchRequest, provider: BaseSearchProvider) -> tuple[str, List[SearchProviderResult], dict | None]:
    started = time.perf_counter()
    policy = get_degradation_policy()
    provider_name = provider.name
    try:
        provider_state = provider.status()
    except Exception:
        provider_state = {}
    if not provider.available():
        return provider_name, [], {
            "provider": provider_name,
            "type": str(provider_state.get("status") or "not_configured"),
            "message": str(provider_state.get("notes") or "provider is not configured or unavailable"),
            "retryable": False,
            "expected_degraded": bool(provider_state.get("degraded_ok")),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        }
    if provider_name == "tavily" and not SearchQuotaLedger().allow("tavily"):
        return provider_name, [], {
            "provider": "tavily",
            "type": "quota_exhausted",
            "message": "Tavily daily quota is exhausted; provider skipped until the next local day.",
            "retryable": False,
            "expected_degraded": True,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        }
    breaker_key = f"provider:{provider_name}"
    breaker = policy.is_open(breaker_key)
    stale_config_probe = _stale_config_breaker_can_probe(provider, breaker)
    if breaker.get("open") and not stale_config_probe:
        policy.record_degradation(
            "search_provider",
            breaker_key,
            f"provider circuit breaker open: {breaker.get('last_reason') or 'recent failures'}",
            {"request": _request_payload(request), "breaker": breaker},
        )
        return provider_name, [], {
            "provider": provider_name,
            "type": "circuit_open",
            "message": breaker.get("last_reason") or "circuit breaker open",
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        }
    if breaker.get("open") and stale_config_probe:
        policy.mark_success(
            breaker_key,
            "search_provider",
            {
                "provider": provider_name,
                "reason": "stale configuration breaker cleared because provider is currently available",
            },
        )
    try:
        items = policy.retry_with_jitter(
            breaker_key,
            "search_provider",
            lambda: provider.search(request),
            retryable_exceptions=(SearchProviderError,),
            metadata={
                "provider": provider_name,
                "query": request.query,
                "limit": request.limit,
                "options": dict(request.options or {}),
            },
        )
    except SearchProviderError as exc:
        policy.record_degradation(
            "search_provider",
            breaker_key,
            exc.message if hasattr(exc, "message") else str(exc),
            {"provider": provider_name, "query": request.query, "limit": request.limit},
        )
        error = exc.to_dict()
        error["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return provider_name, [], error
    except Exception as exc:
        policy.record_degradation(
            "search_provider",
            breaker_key,
            str(exc),
            {"provider": provider_name, "query": request.query, "limit": request.limit},
        )
        return provider_name, [], {"provider": provider_name, "type": "unknown", "message": str(exc), "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)}
    if items:
        if provider_name == "tavily":
            SearchQuotaLedger().record_success("tavily")
        return provider_name, items, None
    expected_empty = bool(provider_state.get("degraded_ok"))
    if not expected_empty:
        policy.record_degradation(
            "search_provider",
            breaker_key,
            "provider returned no results",
            {"provider": provider_name, "query": request.query, "limit": request.limit},
        )
    return provider_name, [], {
        "provider": provider_name,
        "type": "empty_results",
        "message": "provider returned no results for this query",
        "retryable": False,
        "expected_degraded": expected_empty,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def _run_wave(
    request: WebSearchRequest,
    providers: Dict[str, BaseSearchProvider],
    wave: List[str],
    *,
    provider_status_rows: Dict[str, Dict[str, object]] | None = None,
    profiles: Dict[str, object] | None = None,
) -> tuple[Dict[str, List[SearchProviderResult]], List[dict], List[str], dict]:
    results: Dict[str, List[SearchProviderResult]] = {}
    errors: List[dict] = []
    attempted: List[str] = []
    concurrency = compute_wave_concurrency(
        wave,
        provider_status=provider_status_rows,
        profiles=profiles,
        include_raw_content=bool(request.include_raw_content),
    )
    with ThreadPoolExecutor(max_workers=int(concurrency.get("selected_workers") or 1)) as executor:
        futures = {}
        for name in wave:
            provider = providers.get(name)
            attempted.append(name)
            if provider is None:
                errors.append({"provider": name, "type": "unknown_provider", "message": "provider not registered"})
                continue
            futures[executor.submit(_search_provider, request, provider)] = name
        for future in as_completed(futures):
            name, items, error = future.result()
            if items:
                results[name] = items
            if error:
                errors.append(error)
    return results, errors, attempted, concurrency


def _empty_response(request: WebSearchRequest, attempted: List[str], errors: List[dict], metadata: Dict[str, object]) -> WebSearchResponse:
    expected_degraded = bool(errors) and all(bool(error.get("expected_degraded")) for error in errors)
    get_degradation_policy().record_dead_letter(
        "search_provider",
        request.provider or "auto",
        "all providers failed",
        payload=_request_payload(request),
        metadata={"attempted": attempted, "errors": errors},
    )
    return WebSearchResponse(
        query=request.query,
        provider="none",
        items=[],
        fallback_used=len(attempted) > 1,
        attempted_providers=attempted,
        error={
            "type": "all_providers_failed",
            "message": "No configured search provider returned results",
            "details": errors,
            "expected_degraded": expected_degraded,
        },
        metadata={**metadata, "expected_degraded": expected_degraded},
    )


def _record_provider_attempt_receipts(
    *,
    wave_index: int,
    attempted: List[str],
    results: Dict[str, List[SearchProviderResult]],
    errors: List[dict],
) -> None:
    """Record safe per-provider receipts under the active tool trace."""
    errors_by_provider = {
        str(item.get("provider") or ""): item for item in errors if isinstance(item, dict)
    }
    for provider_id in attempted:
        rows = results.get(provider_id) or []
        error = errors_by_provider.get(provider_id) or {}
        status = "ok" if rows else ("degraded" if error.get("expected_degraded") else "failed")
        record_trace_child(
            "provider_attempt",
            tool_name="kr_web_search",
            metadata={
                "status": status,
                "wave_index": int(wave_index),
                "provider_id": provider_id,
                "result_count": len(rows),
                "outcome": "usable_results" if rows else str(error.get("type") or "empty_results"),
                "expected_degraded": bool(error.get("expected_degraded", False)),
                "elapsed_ms": error.get("elapsed_ms"),
            },
        )
def _search_explicit(request: WebSearchRequest, providers: Dict[str, BaseSearchProvider], provider_name: str) -> WebSearchResponse:
    attempted: List[str] = []
    errors: List[dict] = []
    plan = explicit_provider_plan(provider_name)
    if provider_name in {"github", "gh"}:
        attempted.append(provider_name)
        errors.append({
            "provider": provider_name,
            "type": "deprecated_provider_alias",
            "message": "GitHub repository search is now an independent MCP tool; call search_github_repositories instead.",
            "expected_degraded": True,
        })
        return _empty_response(request, attempted, errors, {"plan": plan.to_dict(), "errors": errors, "preferred_tool": "search_github_repositories"})
    provider = providers.get(provider_name)
    if provider is None:
        attempted.append(provider_name)
        errors.append({"provider": provider_name, "type": "unknown_provider", "message": "provider not registered"})
        return _empty_response(request, attempted, errors, {"plan": plan.to_dict(), "errors": errors})
    name, items, error = _search_provider(request, provider)
    attempted.append(name)
    if error:
        errors.append(error)
    _record_provider_attempt_receipts(
        wave_index=0,
        attempted=attempted,
        results={name: items} if items else {},
        errors=errors,
    )
    if items:
        return WebSearchResponse(
            query=request.query,
            provider=name,
            items=items,
            fallback_used=False,
            attempted_providers=attempted,
            metadata={"plan": plan.to_dict(), "errors": errors, "selected_node": name, "selected_strategy": name},
        )
    return _empty_response(request, attempted, errors, {"plan": plan.to_dict(), "errors": errors})


def search_web(request: WebSearchRequest) -> WebSearchResponse:
    started = time.perf_counter()
    providers = _providers()
    if request.provider and request.provider != "auto":
        return _search_explicit(request, providers, request.provider.lower())

    statuses = provider_status()
    host_names = [name for name in providers if name not in {"tavily", "anysearch", "brave", "exa", "searxng"}]
    plan = auto_search_plan(statuses, host_provider_names=host_names)
    attempted: List[str] = []
    errors: List[dict] = []
    provider_items: Dict[str, List[SearchProviderResult]] = {}
    wave_concurrency: List[dict] = []

    for wave_index, wave in enumerate(plan.waves):
        wave_items, wave_errors, wave_attempted, concurrency = _run_wave(
            request,
            providers,
            wave,
            provider_status_rows=statuses,
            profiles=plan.profiles,
        )
        wave_concurrency.append(concurrency)
        attempted.extend(wave_attempted)
        errors.extend(wave_errors)
        provider_items.update(wave_items)
        _record_provider_attempt_receipts(
            wave_index=wave_index,
            attempted=wave_attempted,
            results=wave_items,
            errors=wave_errors,
        )

    items = aggregate_results(provider_items, request.limit) if provider_items else []
    successful = [name for name, rows in provider_items.items() if rows]
    coverage = coverage_decision(request, items, successful)
    supplement_used = False
    if not coverage.sufficient and tavily_supplement_available(statuses) and "tavily" not in attempted:
        wave_items, wave_errors, wave_attempted, concurrency = _run_wave(
            request,
            providers,
            ["tavily"],
            provider_status_rows=statuses,
            profiles=plan.profiles,
        )
        wave_concurrency.append(concurrency)
        attempted.extend(wave_attempted)
        errors.extend(wave_errors)
        provider_items.update(wave_items)
        _record_provider_attempt_receipts(
            wave_index=len(plan.waves),
            attempted=wave_attempted,
            results=wave_items,
            errors=wave_errors,
        )
        items = aggregate_results(provider_items, request.limit) if provider_items else []
        successful = [name for name, rows in provider_items.items() if rows]
        coverage = coverage_decision(request, items, successful)
        supplement_used = bool(wave_items)

    metadata = {
        "plan": plan.to_dict(),
        "coverage": coverage.to_dict(),
        "provider_result_counts": {name: len(rows) for name, rows in provider_items.items()},
        "successful_providers": successful,
        "errors": errors,
        "selected_node": "multi" if successful else "",
        "selected_strategy": "parallel_wave_pool",
        "tavily_supplement_used": supplement_used,
        "wave_concurrency": wave_concurrency,
        "provider_status_cache": statuses.get("_status_cache", {}),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
    }
    if items:
        return WebSearchResponse(
            query=request.query,
            provider="multi",
            items=items,
            fallback_used=supplement_used or len(set(successful)) > 1,
            attempted_providers=attempted,
            metadata=metadata,
        )

    return _empty_response(request, attempted, errors, metadata)
