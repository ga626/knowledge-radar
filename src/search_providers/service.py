"""Provider orchestration for generic web search."""

from __future__ import annotations
import sqlite3

import os
import time
import threading
from contextvars import copy_context
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
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
    DdgsSearchProvider,
    BaseSearchProvider,
    BraveSearchProvider,
    ExaSearchProvider,
    SearchProviderError,
    TavilySearchProvider,
)
from .quota import SearchQuotaLedger, quota_summary
from .status_cache import provider_status_cache
_STATUS_LOCK = threading.RLock()


def _default_provider_order() -> List[str]:
    configured = os.environ.get("KR_WEB_SEARCH_PROVIDERS", "").strip()
    if configured:
        return [item.strip().lower() for item in configured.split(",")
                if item.strip() and not item.strip().lower().startswith('searxng')]
    return ["tavily", "brave", "exa", "anysearch"]


def _providers() -> Dict[str, BaseSearchProvider]:
    providers: Dict[str, BaseSearchProvider] = {
        "tavily": TavilySearchProvider(),
        "anysearch": AnySearchProvider(),
        "brave": BraveSearchProvider(),
        "exa": ExaSearchProvider(),
        "ddgs": DdgsSearchProvider(),
    }
    providers.update(host_search_providers())
    return providers


def _load_provider_status() -> Dict[str, Dict[str, object]]:
    status = {}
    profiles = provider_profiles()
    for name, provider in _providers().items():
        from runtime.search_configuration import current_configuration
        config = current_configuration()
        row = provider.status() if config.allowed(name) else {'available':False, 'status':'disabled', 'configured':provider.available()}
        row.update(enabled=config.allowed(name), configuration_generation=config.generation,
                   health='not_probed', usable_for_routing=bool(row.get('available')))
        row["capability_profile"] = profiles.get(name) or profile_for(name)
        status[name] = row
    status["_quota"] = quota_summary()
    status["_host_search_cards"] = host_search_card_summary()
    return status


def provider_status(*, force_refresh: bool = False) -> Dict[str, Dict[str, object]]:
    from runtime.search_configuration import current_configuration
    generation = current_configuration().generation
    with _STATUS_LOCK:
        if getattr(provider_status_cache, '_generation', None) != generation:
            force_refresh = True
        status = provider_status_cache.get(_load_provider_status, force_refresh=force_refresh)
        provider_status_cache._generation = generation
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
    from runtime.search_configuration import current_configuration
    from runtime.execution_context import check_execution
    check_execution()
    started = time.perf_counter()
    policy = get_degradation_policy()
    provider_name = provider.name
    if not current_configuration(fresh=True).allowed(provider_name):
        return provider_name, [], {'provider':provider_name,'type':'disabled_or_unauthorized',
            'message':'Enable this connection and authorize its data/cost boundary first.',
            'retryable':False,'expected_degraded':True}
    from .constraints import constraint_report
    constraints = constraint_report(request, provider_name)
    if request.options.get('require_constraints') and any(not x['hard_constraint_satisfied'] and not x['locally_enforceable'] for x in constraints.values()):
        return provider_name, [], {'provider':provider_name,'type':'unsupported_constraint',
            'constraints':constraints,'message':'Choose another authorized provider or explicitly relax the constraint.',
            'retryable':False,'expected_degraded':True}
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
            "message": "Tavily free monthly credits are exhausted or quota state is unavailable.",
            "retryable": False,
            "expected_degraded": True,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        }
    breaker_key = f"provider:{provider_name}:{current_configuration().generation}"
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
        from runtime.search_configuration import sending_provider
        deadline = float(request.options.get('_deadline') or time.monotonic()+30)
        provider.timeout = min(float(getattr(provider, 'timeout', 15)), max(.01,deadline-time.monotonic()))
        if time.monotonic() >= deadline:
            return provider_name, [], {'provider':provider_name, 'type':'deadline_exceeded', 'retryable':False}
        with sending_provider(provider_name, deadline):
            request.options.setdefault('_sent_providers', []).append(provider_name)
            items = provider.search(request)
        check_execution()
        from .constraints import filter_results
        items = filter_results(request, items)
        request.options.setdefault('_filtered_providers', []).append(provider_name)
    except SearchProviderError as exc:
        policy.record_degradation(
            "search_provider",
            breaker_key,
            exc.message if hasattr(exc, "message") else str(exc),
            {"provider": provider_name, "query": request.query, "limit": request.limit},
        )
        error = exc.to_dict()
        error['expected_degraded'] = bool(provider_state.get('degraded_ok'))
        error["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return provider_name, [], error
    except Exception as exc:
        from runtime.execution_context import ExecutionStopped
        if isinstance(exc, ExecutionStopped):
            raise
        policy.record_degradation(
            "search_provider",
            breaker_key,
            str(exc),
            {"provider": provider_name, "query": request.query, "limit": request.limit},
        )
        return provider_name, [], {"provider": provider_name, "type": "unknown", "message": str(exc), "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)}
    if items:
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
    executor = ThreadPoolExecutor(max_workers=int(concurrency.get("selected_workers") or 1))
    futures = {}
    try:
        futures = {}
        for name in wave:
            from runtime.execution_context import check_execution
            check_execution()
            if time.monotonic() >= float(request.options.get('_deadline') or float('inf')):
                errors.append({'provider':name,'type':'deadline_exceeded','retryable':False})
                break
            provider = providers.get(name)
            attempted.append(name)
            if provider is None:
                errors.append({"provider": name, "type": "unknown_provider", "message": "Unknown provider. Read the available capability catalog and select a registered connection."})
                continue
            futures[executor.submit(copy_context().run, _observed_search_provider, request, provider)] = name
        pending = set(futures)
        while pending:
            check_execution()
            if time.monotonic() >= float(request.options.get('_deadline') or float('inf')):
                for future in pending:
                    cancelled = future.cancel()
                    if not cancelled:
                        _retain_late_future(future, request)
                    errors.append({'provider':futures[future], 'type':'deadline_exceeded',
                                   'execution_state':'not_sent' if cancelled else 'draining',
                                   'retryable':False, 'late_results':'retained_in_research_candidates_if_task_bound_and_not_cancelled',
                                   'message':'No new send; running request may still finish within its network timeout.'})
                break
            done, pending = wait(pending, timeout=.1, return_when=FIRST_COMPLETED)
            if not done:
                continue
            for future in done:
                _consume_future(future, request, results, errors)
    finally:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
    return results, errors, attempted, concurrency


def _observed_search_provider(request, provider):
    from .concurrency import record_provider_outcome
    started = time.monotonic()
    failed = True
    try:
        result = _search_provider(request, provider)
        error = result[2] or {}
        failed = bool(error) and error.get('type') not in {'empty_results','not_configured','disabled_or_unauthorized','unsupported_constraint'}
        return result
    finally:
        record_provider_outcome(provider.name, elapsed_s=time.monotonic()-started, failed=failed)


def _retain_late_future(future, request):
    """收尾只收已有请求的候选；不延长网络请求、不重发、不改变已返回结果。"""
    context = copy_context()
    def retain(done):
        if done.cancelled():
            return
        try:
            _consume_future(done, request, {}, [])
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning('late_search_candidate_not_recorded:%s', type(exc).__name__)
    future.add_done_callback(lambda done: context.run(retain, done))


def _consume_future(future, request, results, errors):
    from runtime.execution_context import check_execution
    name, items, error = future.result()
    check_execution()
    if items:
        results[name] = items
        # provider完成立即进入发现目录；最终回执仍由调用收口关联。
        from runtime.tool_trace import current_tool_trace
        trace = current_tool_trace()
        if trace.get("research_task_id"):
            try:
                from runtime.research_index import ResearchIndex
                ResearchIndex().candidates(trace["research_task_id"], [x.to_dict() for x in items],
                    "generic_web_ecology", str(trace.get("trace_id", ""))+":"+name, query=request.query)
            except (OSError, ValueError, sqlite3.Error):
                # 整波响应保留acquired_items，最终账本路径仍会补交；不丢已取回候选。
                pass
    if error:
        errors.append(error)

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
        errors.append({"provider": provider_name, "type": "unknown_provider", "message": "Unknown provider. Read the available capability catalog and select a registered connection."})
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
    from runtime.search_configuration import configuration_scope
    from .constraints import prepare_request
    with configuration_scope() as config:
        request = prepare_request(request)
        request = replace(request, options={**request.options,
            '_deadline': time.monotonic()+min(120,max(1,float(request.options.get('timeout_s') or 30)))})
        response = _search_web(request)
        response.metadata['configuration_generation'] = config.generation
        response.metadata['constraint_application'] = {name: __import__('search_providers.constraints', fromlist=['constraint_report']).constraint_report(request,name,sent=name in request.options.get('_sent_providers', []),filtered=name in request.options.get('_filtered_providers', [])) for name in response.attempted_providers}
        return response


def _search_web(request: WebSearchRequest) -> WebSearchResponse:
    started = time.perf_counter()
    providers = _providers()
    if request.provider and request.provider != 'auto':
        name = request.provider.lower()
        return _search_explicit(request, providers, name)

    statuses = provider_status()
    from .catalog import SEARCH_CATALOG
    host_names = [name for name in providers if name.split(':')[0] not in SEARCH_CATALOG]
    plan = auto_search_plan(statuses, host_provider_names=host_names)
    complementary = request.options.get('complementary_providers') or []
    if complementary:
        if not isinstance(complementary, list) or len(complementary) > 6 or any(name not in providers for name in complementary):
            raise ValueError('complementary_providers must name up to six registered connections')
        plan = replace(plan, waves=[list(dict.fromkeys(complementary))],
                       rationale=['executor_selected_complementary_sources', 'bounded_parallel_execution'])
    attempted: List[str] = []
    errors: List[dict] = []
    provider_items: Dict[str, List[SearchProviderResult]] = {}
    wave_concurrency: List[dict] = []

    for wave_index, wave in enumerate(plan.waves):
        from runtime.execution_context import check_execution
        check_execution()
        if time.monotonic() >= request.options['_deadline']:
            errors.append({'type':'deadline_exceeded','message':'No new provider started after deadline'})
            break
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
        if coverage_decision(request, aggregate_results(provider_items, request.limit), list(provider_items)).sufficient:
            break

    items = aggregate_results(provider_items, request.limit) if provider_items else []
    successful = [name for name, rows in provider_items.items() if rows]
    coverage = coverage_decision(request, items, successful)
    supplement_used = False
    if (not coverage.sufficient and tavily_supplement_available(statuses) and "tavily" not in attempted
            and time.monotonic() < request.options['_deadline']):
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
        "coverage": {**coverage.to_dict(), "scope": "provider_wave_not_research_saturation"},
        "acquired_unique_count": len(aggregate_results(provider_items, None)),
        "display_limit": request.limit,
        "ranking": "rrf_v1_constant_60_not_truth_score",
        "provider_result_counts": {name: len(rows) for name, rows in provider_items.items()},
        "successful_providers": successful,
        "errors": errors,
        "selected_node": "multi" if successful else "",
        "selected_strategy": "bounded_incremental_sources",
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
            acquired_items=aggregate_results(provider_items, None),
            fallback_used=supplement_used or len(set(successful)) > 1,
            attempted_providers=attempted,
            metadata=metadata,
        )

    return _empty_response(request, attempted, errors, metadata)
