"""Academic provider orchestration."""

from __future__ import annotations

import time
import json
import hashlib
import threading
import copy
import math
from dataclasses import replace
import httpx
from typing import Dict, List
from runtime.paid_execution import PaidAdmissionError

from .ar5iv import Ar5ivError
from .arxiv import ArxivError, ArxivRateLimitError
from .citation_import import CitationImportError
from .chinese_open_access import ChineseOpenAccessError, _fetch_diagnostic
from .cnki_authorized_browser import cnki_browser_status
from .core import CoreAuthError, CoreError, CoreProvider, CoreRateLimitError
from .crossref import CrossrefError
from .models import AcademicSearchRequest, AcademicSearchResponse, AcademicWork, normalize_doi, normalize_title
from .openalex import OpenAlexError
from .planner import AcademicRoutePlan, analyze_academic_query, plan_academic_search
from .relevance import rank_by_metadata_relevance, score_metadata_relevance, select_fulltext_candidates
from .semanticscholar import SemanticScholarAuthError, SemanticScholarError, SemanticScholarRateLimitError, SemanticScholarProvider
from .wanfang import WanfangProvider
from .serpapi_scholar import SerpApiScholarAuthError, SerpApiScholarError, SerpApiScholarProvider, SerpApiScholarRateLimitError
from .unpaywall import UnpaywallAuthError, UnpaywallError
from .registry import academic_provider_profiles, instantiate_academic_providers
from runtime.status_schema import classify_provider_status


_CACHE: Dict[str, Dict[str, object]] = {}
_CACHE_LOCK = threading.RLock()
_LAST_CALL_AT: Dict[str, float] = {}
_CACHE_TTL_S = 300
_CACHE_MAX_ENTRIES = 128
_MIN_INTERVAL_S = 0.5
_DEFAULT_FULLTEXT_TOP_K = 3
_DEFAULT_FULLTEXT_MIN_SCORE = 0.15
PRIMARY_DISCOVERY_PROVIDERS = {"pubscholar", "openalex"}
DOMAIN_PROVIDERS = {"nssd", "chinaxiv", "sciengine", "napstic_oa", "arxiv", "europepmc", "nstrs", "calis_thesis", "toaj", "ntur", "hkjo"}


def academic_provider_status() -> Dict[str, Dict[str, object]]:
    profiles = academic_provider_profiles()
    providers = _providers()
    status = {}
    for name in providers:
        try:
            status[name] = _enrich_provider_status(name, providers[name].status())
        except (ImportError, OSError, ValueError) as exc:
            status[name] = {'available':False, 'auto_enabled':False,
                            'degraded_reason':type(exc).__name__, 'validation_status':'not_ready'}
    for name, item in status.items():
        profile = profiles.get(name)
        if profile:
            item["capability_profile"] = profile.to_status_dict()
    status["cnki_authorized_browser"] = {
        "configured": True,
        "available": False,
        "requires_api_key": False,
        "network": "managed_chrome_cdp",
        "auto_enabled": False,
        "access_mode": "authorized_browser",
        "status_values": ["OK", "NEEDS_QUERY", "LOGIN_REQUIRED", "CAPTCHA_REQUIRED", "AUTH_REQUIRED", "SCHEMA_CHANGED", "CHROME_UNAVAILABLE"],
        "degraded_reason": "explicit user-authorized browser workflow only; not part of provider=auto",
    }
    status["cnki_authorized_browser"] = _enrich_provider_status("cnki_authorized_browser", status["cnki_authorized_browser"])
    return status


def _enrich_provider_status(name: str, raw_status: Dict[str, object]) -> Dict[str, object]:
    status = dict(raw_status or {})
    status.setdefault("provider_tier", _academic_provider_tier(name, status))
    main_chain = name in PRIMARY_DISCOVERY_PROVIDERS
    classification = classify_provider_status(name, status, main_chain=main_chain)
    status["validation_status"] = classification["status_class"]
    status["status_class"] = classification["status_class"]
    status["validation_reason"] = classification["validation_reason"]
    status["blocks_overall_pass"] = classification["blocks_overall_pass"]
    status['live_verification'] = 'not_checked_by_status_query'
    status['validation_scope'] = 'configuration_and_declared_capability'
    status['article_entitlement'] = 'not_checked'
    return status


def _academic_provider_tier(name: str, status: Dict[str, object]) -> str:
    if name in PRIMARY_DISCOVERY_PROVIDERS:
        return "primary_discovery"
    if name in DOMAIN_PROVIDERS:
        return "intent_domain_discovery"
    if name == "serpapi_scholar":
        return "optional_scholar_metadata_api"
    if name == "citation_import":
        return "user_supplied_citation_import"
    if name == "cnki_authorized_browser":
        return "expected_degraded_authorized_browser_only"
    if status.get("requires_login"):
        return "expected_degraded_login_required"
    if status.get("requires_api_key"):
        return "optional_api_provider"
    return "metadata_provider"


def _search_status(providers, names):
    """发现响应只带本次来源摘要；完整画像由能力目录按需提供。"""
    result = {}
    keys = {'configured','available','auto_enabled','requires_api_key','requires_login',
            'daily_exhausted','monthly_exhausted','degraded_reason','validation_status',
            'status_class','live_verification'}
    keys.update({'validation_scope', 'article_entitlement'})
    for name in dict.fromkeys(names):
        try:
            method = getattr(providers[name], 'status', None)
            status = _enrich_provider_status(name, method() if callable(method) else {})
            result[name] = {k:v for k,v in status.items() if k in keys}
        except (ImportError, OSError, ValueError) as exc:
            result[name] = {'available':False,'degraded_reason':type(exc).__name__}
    return result


def cnki_authorized_browser_probe(query: str = "", limit: int = 10, *, cleanup: bool = True) -> Dict[str, object]:
    return cnki_browser_status(query=query, limit=limit, cleanup=cleanup)


def search_academic_metadata(request: AcademicSearchRequest) -> AcademicSearchResponse:
    try:
        request = _validated_request(request)
    except (ValueError, TypeError, OverflowError) as exc:
        return AcademicSearchResponse(query=str(request.query or ''), provider=str(request.provider or ''),
            error={'type': 'invalid_input', 'message': str(exc), 'retryable': False, 'request_executed': False})
    provider_name = _normalize_provider_name(request.provider or "openalex")
    providers = _providers()
    if provider_name not in providers and provider_name != "auto":
        return AcademicSearchResponse(
            query=request.query,
            provider=provider_name,
            error={"type": "unknown_provider", "message": f"Academic provider not registered: {provider_name}"},
        )
    route_plan = _provider_plan(request, provider_name)
    order = route_plan.provider_order
    cache_key = _cache_key(request, order, providers)
    with _CACHE_LOCK:
        for key in list(_CACHE):
            if time.time() - float(_CACHE[key].get('created_at') or 0) > _CACHE_TTL_S:
                _CACHE.pop(key, None)
        cached = copy.deepcopy(_CACHE.get(cache_key))
    if cached and time.time() - float(cached.get("created_at") or 0) <= _CACHE_TTL_S:
        response = cached.get("response")
        if isinstance(response, AcademicSearchResponse):
            response = copy.deepcopy(response)
            response = replace(response, metadata={**response.metadata, "cache": {"hit": True, "ttl_s": _CACHE_TTL_S}})
            response.metadata['fulltext_resolution'] = _resolve_fulltext_candidates(request, response.items)
            return response

    attempted: List[str] = []
    completed: List[str] = []
    errors = []
    provider_outcomes = []
    all_items: List[AcademicWork] = []
    from .relevance import has_query_evidence

    relevant: List[AcademicWork] = []
    used_provider = ""
    started = time.monotonic()
    max_providers = max(1, min(int(request.options.get('max_provider_attempts', 8)), 22))
    deadline_s = max(1.0, min(float(request.options.get('deadline_s', 90)), 300.0))
    offset = max(0, min(int(request.options.get('provider_offset', 0)), len(order)))
    budget = {'max_provider_attempts': max_providers, 'deadline_s': deadline_s,
              'provider_offset': offset, 'skipped': [], 'boundary': 'provider_attempts_and_cooperative_deadline'}
    for index, name, resolve, batch_tail, admitted in _provider_attempts(
            request, order, providers, offset, max_providers, started, deadline_s):
        from runtime.execution_context import check_execution, ExecutionStopped
        check_execution()
        if not admitted and (len(attempted) >= max_providers or time.monotonic() - started >= deadline_s):
            reason = 'provider_budget_exhausted' if len(attempted) >= max_providers else 'deadline_exhausted'
            budget['skipped'] = [{'provider': pending, 'reason': reason} for pending in order[index:]]
            budget['continuation'] = {'provider_offset': index}
            break
        attempted.append(name)
        try:
            provider = providers[name]
            if name in {'pubscholar', 'nstrs'} and not request.options.get('_background_discovery'):
                from use_cases.academic_discovery import submit_discovery
                ref = submit_discovery(request, name)
                provider_outcomes.append({'provider':name, 'outcome':'pending', 'task':ref})
                continue
            items = resolve()
            diagnostics = list(getattr(items, 'diagnostics', []))
            errors.extend({'provider': name, **diagnostic} for diagnostic in diagnostics if diagnostic.get('type') != 'no_match')
        except ExecutionStopped:
            raise
        except ChineseOpenAccessError as exc:
            errors.extend({'provider': name, **diagnostic} for diagnostic in exc.diagnostics)
            continue
        except (SemanticScholarRateLimitError, ArxivRateLimitError, SerpApiScholarRateLimitError, CoreRateLimitError) as exc:
            errors.append({"provider": name, **_provider_error_diagnostic(exc, 'rate_limited')})
            continue
        except (SerpApiScholarAuthError, CoreAuthError, UnpaywallAuthError, SemanticScholarAuthError) as exc:
            errors.append({"provider": name, **_provider_error_diagnostic(exc, 'auth_required')})
            continue
        except (OpenAlexError, CrossrefError, SemanticScholarError, ArxivError, Ar5ivError, SerpApiScholarError, CoreError, UnpaywallError) as exc:
            errors.append({"provider": name, **_provider_error_diagnostic(exc)})
            continue
        except CitationImportError as exc:
            errors.append({"provider": name, "type": "invalid_user_import", "message": type(exc).__name__, "retryable": False})
            continue
        except PaidAdmissionError as exc:
            errors.append({"provider": name, "type": "budget_blocked", "message": exc.error_code, "retryable": False})
            continue
        except httpx.HTTPError as exc:
            errors.append({'provider': name, **_provider_error_diagnostic(exc)})
            continue
        except Exception as exc:
            errors.append({"provider": name, **_provider_error_diagnostic(exc)})
            continue
        completed.append(name)
        candidate_count = len(items)
        matching_count = sum(not item.raw.get('fallback_sample') and (name == 'citation_import' or has_query_evidence(request.query, item)) for item in items)
        if candidate_count:
            outcome = 'results' if matching_count else 'relevance_filtered'
        elif diagnostics:
            kinds = [row.get('type') for row in diagnostics]
            # 首页或其他备用页的空壳不能覆盖明确故障。
            outcome = next((kind for kind in kinds if kind not in {'no_match', 'search_not_implemented'}),
                           'no_match' if kinds and all(kind == 'no_match' for kind in kinds) else 'search_not_implemented')
        else:
            from .chinese_open_access import ChineseOpenAccessProvider
            outcome = 'search_response_unparsed' if isinstance(provider, ChineseOpenAccessProvider) else 'no_match'
            if outcome == 'search_response_unparsed':
                errors.append({'provider': name, 'type': outcome, 'retryable': False,
                               'message': '采集器未返回候选且没有无匹配证据；需修复或核验解析链路'})
        provider_outcomes.append({'provider': name, 'outcome': outcome, 'candidate_count': candidate_count,
                                  'matching_count': matching_count, 'diagnostics': diagnostics,
                                  'continuation':getattr(items, 'continuation', None),
                                  'pagination_support':'explicit_cursor' if hasattr(items, 'continuation') else 'not_implemented'})
        if items:
            all_items.extend(item for item in items if not item.raw.get("fallback_sample"))
            matching = [item for item in items if not item.raw.get("fallback_sample") and (name == 'citation_import' or has_query_evidence(request.query, item))]
            relevant.extend(matching)
            if matching and not used_provider:
                used_provider = name
        required = set(getattr(route_plan, "required_providers", []))
        if batch_tail and required.issubset(completed) and len(_dedupe(relevant)) >= max(1, int(request.limit or 5)):
            if index + 1 < len(order):
                budget['continuation'] = {'provider_offset':index + 1}
                budget['skipped'] = [{'provider':p, 'reason':'batch_limit_reached'} for p in order[index+1:]]
            break

    limit = max(1, min(int(request.limit or 5), 20))
    relevant = _dedupe(relevant)
    ranked = _rank_search_results(request.query, relevant)
    budget['elapsed_s'] = round(time.monotonic() - started, 3)
    budget['attempted_count'] = len(attempted)
    budget['missing_required_providers'] = [name for name in getattr(route_plan, 'required_providers', []) if name not in completed]
    deduped = ranked[:limit]
    from .identity import save_work, load_work
    filtered = [work for work in _dedupe(all_items) if not any(work.url == hit.url for hit in relevant)]
    filtered_refs = [{'title':work.title, 'reason':'lexical_filter', 'reference':save_work(work)} for work in filtered]
    overflow = [work for work in ranked[limit:]]
    overflow_refs = [{'title':work.title, 'reason':'batch_return_limit', 'reference':save_work(work)} for work in overflow]
    for work in deduped:
        work.raw['fulltext_ref'] = save_work(work)
        if work.raw.get('fulltext_text'):
            work.raw['fulltext_inline_ref'] = load_work(work.raw['fulltext_ref']).raw['fulltext_inline_ref']
            work.raw.pop('fulltext_text')
    fulltext_resolution = {'applied':False, 'reason':'disabled_by_request'}
    budget['elapsed_s'] = round(time.monotonic() - started, 3)
    known = {row['provider'] for row in provider_outcomes}
    provider_outcomes.extend({'provider': name, 'outcome': 'request_failed',
        'diagnostics': [error for error in errors if error.get('provider') == name]}
        for name in attempted if name not in known)
    if not deduped:
        return AcademicSearchResponse(
            query=request.query,
            provider=used_provider or "none",
            error=None if any(row.get('task') for row in provider_outcomes) and not errors else {
                "type": _aggregate_error_type(errors, completed=bool(completed)),
                "message": "No query-relevant academic results were returned",
                "details": errors,
                "retryable": any(error.get("retryable", False) for error in errors),
            },
            metadata={
                "attempted_providers": attempted,
                "completed_providers": completed,
                "provider_outcomes": provider_outcomes,
                "execution_budget": budget,
                "provider_status": _search_status(providers, attempted),
                "expected_degraded": True,
                "degraded_reason": provider_outcomes[0]['outcome'] if len(provider_outcomes) == 1 else "mixed_source_outcomes_no_results",
                "planner": _route_plan_metadata(route_plan),
                "filtered_candidates": filtered_refs,
                "additional_candidates":overflow_refs,
                "pending_discoveries": [row['task'] for row in provider_outcomes if row.get('task')],
                "fulltext_resolution":fulltext_resolution,
            },
        )
    response = AcademicSearchResponse(
        query=request.query,
        provider=used_provider,
        items=deduped,
        metadata={
            "attempted_providers": attempted,
            "completed_providers": completed,
            "provider_outcomes": provider_outcomes,
            "execution_budget": budget,
            "planner": _route_plan_metadata(route_plan),
            "fallback_used": bool(attempted and used_provider != attempted[0]),
            "errors": errors,
            "cache": {"hit": False, "ttl_s": _CACHE_TTL_S},
            "provider_status": _search_status(providers, attempted),
            "relevance_ranking": {
                "discarded_unrelated_count": max(0, len(_dedupe(all_items)) - len(relevant)),
                "identity_verification": "search_candidates_only; title match does not prove edition, year or DOI identity",
                "strategy": "metadata_title_abstract_source_confidence",
                "boundary": "基础词面过滤；语义相关性、材料类型适用性和证据充分性由 Codex 判断。",
                "applied": True,
                "top_score": score_metadata_relevance(request.query, deduped[0]) if deduped else 0.0,
            },
            "fulltext_resolution": fulltext_resolution,
            "filtered_candidates": filtered_refs,
            "additional_candidates":overflow_refs,
            "pending_discoveries": [row['task'] for row in provider_outcomes if row.get('task')],
        },
    )
    # 只缓存无研究作用域、无全文任务引用的书目。后台发现引用不跨研究缓存。
    if not response.metadata['pending_discoveries']:
        with _CACHE_LOCK:
            while len(_CACHE) >= _CACHE_MAX_ENTRIES:
                _CACHE.pop(next(iter(_CACHE)))
            _CACHE[cache_key] = {"created_at": time.time(), "response": copy.deepcopy(response)}
    response.metadata['fulltext_resolution'] = _resolve_fulltext_candidates(request, response.items)
    return response


def _search_provider(provider, name, request, started, deadline_s):
    from runtime.execution_context import check_execution
    check_execution()
    _rate_limit(name)
    remaining = deadline_s - (time.monotonic() - started)
    if remaining <= 0:
        raise TimeoutError('academic_discovery_deadline_exceeded')
    for attr in ('timeout', 'timeout_s'):
        current = getattr(provider, attr, None)
        if isinstance(current, (int, float)):
            setattr(provider, attr, min(current, remaining))
    result = provider.search(request)
    check_execution()
    return result


def _provider_attempts(request, order, providers, offset, max_providers, started, deadline_s):
    """相邻公开API小批并行；有费用/浏览器来源不预发，已发的整批都登记结果。"""
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context
    fast = {'openalex', 'crossref', 'arxiv', 'europepmc', 'semanticscholar', 'unpaywall'}
    width = request.options.get('api_parallelism', 3)
    index = offset
    with ThreadPoolExecutor(max_workers=width, thread_name_prefix='academic-api') as pool:
        while index < len(order):
            end = index + 1
            if time.monotonic() - started < deadline_s and index < offset + max_providers and order[index] in fast:
                while (end < min(len(order), index + width, offset + max_providers)
                       and order[end] in fast):
                    end += 1
            if end - index > 1:
                futures = []
                for position in range(index, end):
                    name = order[position]
                    context = copy_context()
                    futures.append(pool.submit(context.run, _search_provider, providers[name], name,
                                               request, started, deadline_s))
                for position, future in zip(range(index, end), futures):
                    yield position, order[position], future.result, position == end-1, True
            else:
                name = order[index]
                yield index, name, lambda: _search_provider(providers[name], name, request, started, deadline_s), True, False
            index = end


def _providers() -> Dict[str, object]:
    return instantiate_academic_providers()


def _provider_plan(request: AcademicSearchRequest, provider_name: str) -> AcademicRoutePlan:
    profiles = academic_provider_profiles()
    core = CoreProvider()
    serpapi = SerpApiScholarProvider()
    wanfang = WanfangProvider()
    return plan_academic_search(
        request,
        provider_name,
        profiles,
        semantic_available=SemanticScholarProvider().auto_available,
        wanfang_available=bool(wanfang.enabled and wanfang.api_key),
        core_available=bool(core.api_key and not core.daily_quota.exhausted),
        serpapi_available=bool(serpapi.enabled_for_auto and serpapi.api_key and not serpapi.daily_quota.exhausted),
    )


def _route_plan_metadata(plan: AcademicRoutePlan) -> Dict[str, object]:
    intent = plan.intent
    reason = "explicit_provider"
    if plan.waves and "explicit" not in plan.waves:
        if intent.chinese_like:
            reason = "chinese_primary_then_intent_sources"
        elif intent.doi_like:
            reason = "doi_crossref_then_oa_lookup"
        elif intent.citation_import:
            reason = "citation_import_then_metadata_chain"
        else:
            reason = "global_metadata_chain"
    return {
        "schema": "knowledgeradar-academic-route-plan/v2",
        "required_providers": list(plan.required_providers),
        "coverage_rule": "complete_primary_and_intent_sources_before_count_stop",
        "reason": reason,
        "provider_order": list(plan.provider_order),
        "waves": {name: list(values) for name, values in plan.waves.items()},
        "intent": {
            "language": intent.language,
            "citation_import": intent.citation_import,
            "arxiv_like": intent.arxiv_like,
            "doi_like": intent.doi_like,
            "chinese_like": intent.chinese_like,
            "disciplines": list(intent.disciplines),
        },
        "stages": ["metadata", "fulltext_resolution"],
    }


def _provider_order(request: AcademicSearchRequest, provider_name: str) -> List[str]:
    return _provider_plan(request, provider_name).provider_order


def _looks_like_citation_import(request: AcademicSearchRequest) -> bool:
    return analyze_academic_query(request).citation_import


def _normalize_provider_name(provider: str) -> str:
    name = str(provider or "").strip().lower().replace("-", "_")
    aliases = {
        "semantic_scholar": "semanticscholar",
        "semantic_scholar_api": "semanticscholar",
        "s2": "semanticscholar",
        "google_scholar": "serpapi_scholar",
        "serpapi": "serpapi_scholar",
        "nssd_cn": "nssd",
        "china_xiv": "chinaxiv",
    }
    return aliases.get(name, name)


def _looks_like_chinese_query(query: str) -> bool:
    return analyze_academic_query(AcademicSearchRequest(query=str(query or ""))).chinese_like


def _looks_like_arxiv_query(query: str) -> bool:
    return analyze_academic_query(AcademicSearchRequest(query=str(query or ""))).arxiv_like


def _validated_request(request: AcademicSearchRequest) -> AcademicSearchRequest:
    if not isinstance(request.query, str) or not request.query.strip():
        raise ValueError('query must be a nonempty string')
    if not isinstance(request.options, dict):
        raise ValueError('options must be an object')
    options = dict(request.options)
    if 'source_cursor' in options and (not isinstance(options['source_cursor'], str) or len(options['source_cursor']) > 4000):
        raise ValueError('source_cursor must be a string of at most 4000 characters')
    limits = {'max_provider_attempts': (1, 22, True), 'provider_offset': (0, 22, True), 'api_parallelism': (1, 4, True),
              'deadline_s': (1, 300, False), 'fulltext_top_k': (0, 5, True),
              'fulltext_min_score': (0, 1, False), 'fulltext_deadline_s': (1, 3600, False), 'source_offset': (0, 100000, True)}
    for key, (low, high, integer) in limits.items():
        if key not in options:
            continue
        value = options[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f'{key} must be a finite number')
        if integer and int(value) != value:
            raise ValueError(f'{key} must be an integer')
        options[key] = max(low, min(int(value) if integer else float(value), high))
    for key in ('resolve_fulltext', 'need_fulltext', 'has_readable_fulltext', 'include_ocr'):
        if key in options and not isinstance(options[key], bool):
            raise ValueError(f'{key} must be a boolean')
    if isinstance(request.limit, bool) or not isinstance(request.limit, int):
        raise ValueError('limit must be an integer')
    return replace(request, query=request.query.strip(), limit=max(1, min(request.limit, 20)), options=options)


def _cache_key(request: AcademicSearchRequest, order: List[str], providers=None) -> str:
    # 配置改变后不复用旧授权结果；只将摘要用于内存键，不写出凭据。
    identity = {name: {attr: getattr((providers or {}).get(name), attr, None)
                      for attr in ('api_key', 'email', 'endpoint', 'base_url', 'enabled')}
                for name in order}
    return "|".join(
        [
            request.query.strip().lower(),
            str(int(request.limit or 5)),
            ",".join(order),
            _options_cache_key(request.options),
            _options_cache_key(identity),
        ]
    )


def _options_cache_key(options: Dict[str, object]) -> str:
    if not options:
        return "{}"
    try:
        value = json.dumps(options, sort_keys=True, ensure_ascii=True, default=str)
    except TypeError:
        value = str(sorted((str(key), str(value)) for key, value in options.items()))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _rate_limit(provider_name: str) -> None:
    from .rate_limit import wait_interval
    wait_interval(provider_name, _MIN_INTERVAL_S)


def _provider_error_diagnostic(exc: Exception, fallback: str = '') -> Dict[str, Any]:
    cause, seen = exc, set()
    for _ in range(5):
        if id(cause) in seen:
            break
        seen.add(id(cause))
        if isinstance(cause, httpx.HTTPError):
            return {**_fetch_diagnostic(cause), 'request_stage': 'search', 'wrapper': type(exc).__name__}
        cause = cause.__cause__ or cause.__context__
        if cause is None:
            break
    kind = fallback or _classify_provider_error(exc)
    return {'type': kind, 'message': type(exc).__name__, 'request_stage': 'search',
            'retryable': kind not in {'auth_required', 'budget_blocked'}, 'backoff_suggested': kind == 'rate_limited'}


def _classify_provider_error(exc: Exception) -> str:
    if isinstance(exc, PaidAdmissionError):
        return "budget_blocked"
    cause = exc
    for _ in range(5):
        if isinstance(cause, httpx.HTTPError):
            return _fetch_diagnostic(cause).get('type', 'request_failed')
        if not cause.__cause__:
            break
        cause = cause.__cause__
    text = str(exc).lower()
    if "429" in text or "rate limit" in text or "too many requests" in text or "限流" in text:
        return "rate_limited"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    return "request_failed"


def _aggregate_error_type(errors: List[Dict[str, object]], *, completed: bool = False) -> str:
    if not errors:
        return "empty_results"
    if completed:
        return "partial_provider_errors_no_results"
    if errors and all(error.get("type") == "rate_limited" for error in errors):
        return "all_providers_rate_limited"
    if any(error.get("type") == "rate_limited" for error in errors):
        return "provider_rate_limited_or_empty"
    return "all_providers_failed"


def _dedupe(items: List[AcademicWork]) -> List[AcademicWork]:
    seen = {}
    deduped = []
    for item in items:
        from .identity import normalized_permissions
        item = replace(item, raw=normalized_permissions(item.raw))
        doi = normalize_doi(item.doi)
        key = (doi or _metadata_key(item)) + '|edition:' + str(item.version or 'unknown') + '|type:' + (item.document_type if item.document_type not in {'unknown', 'journal-article', 'article'} else 'article')
        if not key:
            continue
        if key in seen:
            index = seen[key]
            previous = deduped[index]
            # 同一 DOI 才合并；标题去重还要求年份与首位作者一致。
            records = list(previous.raw.get('source_records') or [_source_record(previous)])
            if _source_record(item) not in records:
                records.append(_source_record(item))
            links = list(dict.fromkeys(list(previous.raw.get('fulltext_candidate_urls') or []) +
                _work_links(previous) + _work_links(item)))
            raw = {**item.raw, **previous.raw, 'source_records': records, 'fulltext_candidate_urls': links}
            # 书目聚合不是版本核实或权限升级，任一明确禁止都不能被另一条记录覆盖。
            raw['edition_merge_verification'] = 'unverified' if item.version == 'unknown' else 'declared_version_match'
            for permission in ('read_allowed', 'download_allowed', 'cloud_processing_allowed', 'evidence_storage_allowed'):
                if item.raw.get(permission) is False or previous.raw.get(permission) is False:
                    raw[permission] = False
            if item.raw.get('fulltext_text') and not raw.get('fulltext_text'):
                raw['fulltext_text'] = item.raw['fulltext_text']
            deduped[index] = replace(previous, abstract=previous.abstract or item.abstract,
                authors=previous.authors or item.authors, year=previous.year or item.year,
                document_type=item.document_type if previous.document_type == 'unknown' else previous.document_type,
                raw=raw)
            continue
        seen[key] = len(deduped)
        deduped.append(copy.deepcopy(item))
    return deduped


def _source_record(work):
    return {'provider_id': _work_provider_id(work), 'url': work.url, 'doi': work.doi,
            'version': work.version, 'license': work.license, 'full_text_status': work.full_text_status}


def _work_links(work):
    raw = work.raw
    links = [raw.get(key) for key in ('pdf_url', 'download_url', 'url_for_pdf', 'ar5iv_html_url', 'reader_url', 'open_access_pdf_url', 'fulltext_link', 'url')]
    primary = raw.get('primary_location') or {}
    links += [primary.get('pdf_url'), primary.get('landing_page_url')]
    links += [entry.get('url') for entry in raw.get('full_text_urls', [])
              if isinstance(entry, dict) and entry.get('availabilityCode') == 'OA']
    links += raw.get('source_fulltext_urls') or []
    return [value for value in links if isinstance(value, str) and value.startswith(('http://', 'https://'))]


def _rank_search_results(query: str, items: List[AcademicWork]) -> List[AcademicWork]:
    if len(items) <= 1:
        return items
    return rank_by_metadata_relevance(query, items, academic_provider_profiles())


def _resolve_fulltext_candidates(request: AcademicSearchRequest, ranked: List[AcademicWork], *, timeout_s=60) -> Dict[str, object]:
    options = dict(request.options or {})
    if not options.get('resolve_fulltext', options.get('need_fulltext', False)):
        return {"strategy": "top_k_direct_read", "applied": False, "reason": "disabled_by_request"}
    if options.get('has_readable_fulltext'):
        return {'strategy': 'top_k_direct_read', 'applied': False, 'reason': 'already_readable'}
    if not ranked:
        return {"strategy": "top_k_direct_read", "applied": False, "reason": "no_ranked_items"}

    profiles = academic_provider_profiles()
    top_k = max(0, min(int(options.get("fulltext_top_k", _DEFAULT_FULLTEXT_TOP_K) or 0), 5))
    min_score = float(options.get("fulltext_min_score", _DEFAULT_FULLTEXT_MIN_SCORE) or 0.0)
    if top_k <= 0:
        return {"strategy": "top_k_direct_read", "applied": False, "reason": "top_k_zero"}

    from .fulltext import provider_supports_direct_read
    eligible = [work for work in ranked if provider_supports_direct_read(_work_provider_id(work), profiles, work=work)]
    candidates = select_fulltext_candidates(request.query, eligible, top_k=top_k, min_score=min_score)
    from use_cases.academic_fulltext import request_academic_fulltext
    from .identity import save_work
    from runtime.tool_trace import current_tool_trace
    trace = current_tool_trace()
    tasks, errors = [], []
    task_timeout = max(1, min(3600, float(options.get('fulltext_deadline_s', 600))))
    for candidate in candidates:
        provider_id = _work_provider_id(candidate)
        try:
            reference = candidate.raw.get('fulltext_ref') or save_work(candidate)
            task = request_academic_fulltext(reference, timeout_s=task_timeout,
                research_task_id=trace.get('research_task_id',''), include_ocr=options.get('include_ocr'))
            ref = {'task_id':task['task_id'],'status':task['status'],'provider_id':provider_id,'url':candidate.url}
            tasks.append(ref)
            candidate.raw['fulltext_task'] = ref
        except (ValueError, RuntimeError, OSError) as exc:
            from runtime.execution_context import ExecutionStopped
            if isinstance(exc, ExecutionStopped):
                raise
            errors.append({'provider_id':provider_id,'reason':type(exc).__name__})
    return {'strategy':'independent_fulltext_tasks', 'applied':bool(tasks),
            'candidate_count':len(candidates),'attempted_count':len(tasks),'resolved_count':0,
            'tasks':tasks,'submission_errors':errors,'requested_scope_complete':not tasks and not candidates,
            'next_action':'Read get_task_status for each fulltext task, then read fixed evidence via read_knowledge.'}



def _work_provider_id(work: AcademicWork) -> str:
    return str(work.source_database or work.source or "").strip().lower()


def _mark_fulltext_resolved(work: AcademicWork, text_length: int) -> None:
    object.__setattr__(work, "full_text_status", "direct_read_text_extractable")
    object.__setattr__(work, "verification_status", "fulltext_verified")
    raw = dict(work.raw)
    raw.setdefault("fulltext_text_length", text_length)
    object.__setattr__(work, "raw", raw)


def _metadata_key(item: AcademicWork) -> str:
    first_author = ""
    if item.authors:
        first_author = str(item.authors[0] or "").strip().lower()
        first_author = "".join(ch for ch in first_author if ch.isalnum())
    title = normalize_title(item.title)
    if not title:
        return ""
    return f"title:{title}|year:{item.year or ''}|author:{first_author}"
