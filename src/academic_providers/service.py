"""Academic provider orchestration."""

from __future__ import annotations

import time
import json
from importlib import import_module
from typing import Dict, List

from .ar5iv import Ar5ivError
from .arxiv import ArxivError, ArxivRateLimitError
from .baidu_scholar import (
    BaiduScholarAuthError,
    BaiduScholarError,
    BaiduScholarProvider,
    BaiduScholarRateLimitError,
    BaiduScholarUnavailableError,
)
from .citation_import import CitationImportError
from .cnki_authorized_browser import cnki_browser_status
from .core import CoreAuthError, CoreError, CoreProvider, CoreRateLimitError
from .crossref import CrossrefError
from .fulltext import extract_academic_fulltext
from .models import AcademicSearchRequest, AcademicSearchResponse, AcademicWork, normalize_doi, normalize_title
from .openalex import OpenAlexError
from .planner import AcademicRoutePlan, analyze_academic_query, plan_academic_search
from .relevance import rank_by_metadata_relevance, score_metadata_relevance, select_fulltext_candidates
from .semanticscholar import SemanticScholarError, SemanticScholarRateLimitError
from .serpapi_scholar import SerpApiScholarAuthError, SerpApiScholarError, SerpApiScholarProvider, SerpApiScholarRateLimitError
from .unpaywall import UnpaywallAuthError, UnpaywallError
from .registry import academic_provider_profiles, instantiate_academic_providers
from runtime.status_schema import classify_provider_status


_CACHE: Dict[str, Dict[str, object]] = {}
_LAST_CALL_AT: Dict[str, float] = {}
_CACHE_TTL_S = 300
_MIN_INTERVAL_S = 0.5
_DEFAULT_FULLTEXT_TOP_K = 3
_DEFAULT_FULLTEXT_MIN_SCORE = 0.15
P2_4_DEFAULT_CHINESE_FULLTEXT_PROVIDERS = {"nssd", "chinaxiv", "hanspub", "oajrc", "sciopen", "pubscholar", "sciengine", "vip_oa"}
P2_4_CONNECTED_CHINESE_FULLTEXT_PROVIDERS = {
    "coaj",
    "nssd_cn",
    "ucdrs",
    "pubscholar",
    "ivy_publisher",
    "paper_edu",
    "calis_thesis",
    "nstrs",
    "paperscope",
    "toaj",
    "ntur",
    "hkjo",
    "gooa",
    "oalib",
    "sciengine",
}
P2_4_CHINESE_ABSTRACT_SUPPLEMENT_PROVIDERS = {"socolar"}
P2_4_EXPECTED_DEGRADED_EXTERNAL_PLATFORMS: Dict[str, Dict[str, object]] = {
    "wanfang": {
        "configured": False,
        "available": False,
        "requires_login": True,
        "auto_enabled": False,
        "access_mode": "user_export_or_authorized_browser",
        "status": "degraded",
        "provider_tier": "expected_degraded_external_login_or_subscription",
        "degraded_reason": "Wanfang full-text is not a registered automatic provider; use user citation export/import or an explicit authorized-browser workflow.",
    },
}
LEGACY_EXPLICIT_PROVIDER_CLASSES: Dict[str, str] = {
    # These providers remain available for explicit reprobe/status reporting,
    # but they are intentionally not part of the profile-driven auto planner.
    "coaj": "academic_providers.coaj:CoajProvider",
    "nssd_cn": "academic_providers.nssd_cn:NssdCnProvider",
    "ucdrs": "academic_providers.ucdrs:UcdrsProvider",
    "paper_edu": "academic_providers.paper_edu:PaperEduProvider",
    "calis_thesis": "academic_providers.calis_thesis:CalisThesisProvider",
    "nstrs": "academic_providers.nstrs:NstrsProvider",
    "paperscope": "academic_providers.paperscope:PaperScopeProvider",
    "toaj": "academic_providers.toaj:ToajProvider",
    "ntur": "academic_providers.ntur:NturProvider",
    "gooa": "academic_providers.gooa:GoOaProvider",
    "oalib": "academic_providers.oalib:OalibProvider",
    "baidu_scholar": "academic_providers.baidu_scholar:BaiduScholarProvider",
    "serpapi_scholar": "academic_providers.serpapi_scholar:SerpApiScholarProvider",
}


def academic_provider_status() -> Dict[str, Dict[str, object]]:
    profiles = academic_provider_profiles()
    status = {name: _enrich_provider_status(name, provider.status()) for name, provider in _providers().items()}
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
    for name, item in P2_4_EXPECTED_DEGRADED_EXTERNAL_PLATFORMS.items():
        status[name] = _enrich_provider_status(name, item)
    return status


def _enrich_provider_status(name: str, raw_status: Dict[str, object]) -> Dict[str, object]:
    status = dict(raw_status or {})
    status.setdefault("provider_tier", _academic_provider_tier(name, status))
    if name == "baidu_scholar":
        if not status.get("degraded_reason"):
            status["degraded_reason"] = "Baidu Qianfan is an external API entitlement/quota boundary and is not part of the always-on Chinese full-text acceptance gate."
        status["validation_status"] = "EXPECTED_DEGRADED"
    main_chain = name in P2_4_DEFAULT_CHINESE_FULLTEXT_PROVIDERS and bool(status.get("auto_enabled", True))
    classification = classify_provider_status(name, status, main_chain=main_chain)
    status["validation_status"] = classification["status_class"]
    status["status_class"] = classification["status_class"]
    status["validation_reason"] = classification["validation_reason"]
    status["blocks_overall_pass"] = classification["blocks_overall_pass"]
    return status


def _academic_provider_tier(name: str, status: Dict[str, object]) -> str:
    if name in P2_4_DEFAULT_CHINESE_FULLTEXT_PROVIDERS:
        return "p2_4_default_chinese_fulltext"
    if name in P2_4_CHINESE_ABSTRACT_SUPPLEMENT_PROVIDERS:
        return "p2_4_chinese_abstract_discovery_supplement"
    if name in P2_4_CONNECTED_CHINESE_FULLTEXT_PROVIDERS:
        return "p2_4_connected_chinese_fulltext"
    if name == "baidu_scholar":
        return "expected_degraded_official_api_when_unprovisioned"
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


def cnki_authorized_browser_probe(query: str = "", limit: int = 10, *, cleanup: bool = True) -> Dict[str, object]:
    return cnki_browser_status(query=query, limit=limit, cleanup=cleanup)


def search_academic_metadata(request: AcademicSearchRequest) -> AcademicSearchResponse:
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
    cache_key = _cache_key(request, order)
    cached = _CACHE.get(cache_key)
    if cached and time.time() - float(cached.get("created_at") or 0) <= _CACHE_TTL_S:
        response = cached.get("response")
        if isinstance(response, AcademicSearchResponse):
            return AcademicSearchResponse(
                query=response.query,
                provider=response.provider,
                items=response.items,
                error=response.error,
                metadata={**response.metadata, "cache": {"hit": True, "ttl_s": _CACHE_TTL_S}},
            )

    attempted: List[str] = []
    errors = []
    all_items: List[AcademicWork] = []
    used_provider = ""
    for name in order:
        provider = providers[name]
        attempted.append(name)
        try:
            _rate_limit(name)
            items = provider.search(request)
        except (SemanticScholarRateLimitError, ArxivRateLimitError, BaiduScholarRateLimitError, SerpApiScholarRateLimitError, CoreRateLimitError) as exc:
            errors.append({"provider": name, "type": "rate_limited", "message": str(exc), "retryable": True, "backoff_suggested": True})
            continue
        except (BaiduScholarAuthError, SerpApiScholarAuthError, CoreAuthError, UnpaywallAuthError) as exc:
            errors.append({"provider": name, "type": "auth_required", "message": str(exc), "retryable": False})
            continue
        except BaiduScholarUnavailableError as exc:
            errors.append({"provider": name, "type": "provider_unavailable", "message": str(exc), "retryable": False})
            continue
        except (OpenAlexError, CrossrefError, SemanticScholarError, ArxivError, Ar5ivError, BaiduScholarError, SerpApiScholarError, CoreError, UnpaywallError) as exc:
            errors.append({"provider": name, "type": _classify_provider_error(exc), "message": str(exc), "retryable": True})
            continue
        except CitationImportError as exc:
            errors.append({"provider": name, "type": "invalid_user_import", "message": str(exc), "retryable": False})
            continue
        except Exception as exc:
            errors.append({"provider": name, "type": "unknown", "message": str(exc), "retryable": True})
            continue
        if items:
            all_items.extend(items)
            used_provider = name if not used_provider else used_provider
        if len(_dedupe(all_items)) >= max(1, int(request.limit or 5)):
            break

    limit = max(1, min(int(request.limit or 5), 20))
    ranked = _rank_search_results(request.query, _dedupe(all_items))
    fulltext_resolution = _resolve_fulltext_candidates(request, ranked)
    deduped = ranked[:limit]
    if not deduped:
        return AcademicSearchResponse(
            query=request.query,
            provider=used_provider or "none",
            error={
                "type": _aggregate_error_type(errors),
                "message": "No academic provider returned results",
                "details": errors,
                "retryable": True,
            },
            metadata={
                "attempted_providers": attempted,
                "provider_status": academic_provider_status(),
                "expected_degraded": True,
                "degraded_reason": "academic_provider_unavailable",
                "planner": _route_plan_metadata(route_plan),
            },
        )
    response = AcademicSearchResponse(
        query=request.query,
        provider=used_provider,
        items=deduped,
        metadata={
            "attempted_providers": attempted,
            "planner": _route_plan_metadata(route_plan),
            "fallback_used": bool(attempted and used_provider != attempted[0]),
            "errors": errors,
            "cache": {"hit": False, "ttl_s": _CACHE_TTL_S},
            "provider_status": academic_provider_status(),
            "relevance_ranking": {
                "strategy": "metadata_title_abstract_source_confidence",
                "applied": True,
                "top_score": score_metadata_relevance(request.query, deduped[0]) if deduped else 0.0,
            },
            "fulltext_resolution": fulltext_resolution,
        },
    )
    _CACHE[cache_key] = {"created_at": time.time(), "response": response}
    return response


def _providers() -> Dict[str, object]:
    providers = instantiate_academic_providers()
    for provider_id, provider_class in LEGACY_EXPLICIT_PROVIDER_CLASSES.items():
        providers.setdefault(provider_id, _instantiate_provider(provider_class))
    return providers


def _instantiate_provider(provider_class: str) -> object:
    module_name, sep, class_name = provider_class.partition(":")
    if not sep:
        raise ValueError(f"Invalid provider class spec: {provider_class}")
    module = import_module(module_name)
    return getattr(module, class_name)()


def _provider_plan(request: AcademicSearchRequest, provider_name: str) -> AcademicRoutePlan:
    profiles = academic_provider_profiles()
    baidu = BaiduScholarProvider()
    core = CoreProvider()
    serpapi = SerpApiScholarProvider()
    return plan_academic_search(
        request,
        provider_name,
        profiles,
        baidu_available=bool(baidu.bearer_token),
        core_available=bool(core.api_key and not core.daily_quota.exhausted),
        serpapi_available=bool(serpapi.enabled_for_auto and serpapi.api_key and not serpapi.daily_quota.exhausted),
    )


def _route_plan_metadata(plan: AcademicRoutePlan) -> Dict[str, object]:
    intent = plan.intent
    reason = "explicit_provider"
    if plan.waves and "explicit" not in plan.waves:
        if intent.chinese_like:
            reason = "chinese_query_metadata_then_open_fulltext_chain"
        elif intent.doi_like:
            reason = "doi_query_prefers_unpaywall_then_global_metadata"
        elif intent.citation_import:
            reason = "citation_import_then_metadata_chain"
        else:
            reason = "global_metadata_chain"
    return {
        "schema": "knowledgeradar-academic-route-plan/v1",
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
        "baidu": "baidu_scholar",
        "baidu_qianfan": "baidu_scholar",
        "china_xiv": "chinaxiv",
    }
    return aliases.get(name, name)


def _looks_like_chinese_query(query: str) -> bool:
    return analyze_academic_query(AcademicSearchRequest(query=str(query or ""))).chinese_like


def _looks_like_arxiv_query(query: str) -> bool:
    return analyze_academic_query(AcademicSearchRequest(query=str(query or ""))).arxiv_like


def _cache_key(request: AcademicSearchRequest, order: List[str]) -> str:
    return "|".join(
        [
            request.query.strip().lower(),
            str(int(request.limit or 5)),
            ",".join(order),
            _options_cache_key(request.options),
        ]
    )


def _options_cache_key(options: Dict[str, object]) -> str:
    if not options:
        return "{}"
    try:
        return json.dumps(options, sort_keys=True, ensure_ascii=True, default=str)[:1000]
    except TypeError:
        return str(sorted((str(key), str(value)) for key, value in options.items()))[:1000]


def _rate_limit(provider_name: str) -> None:
    now = time.time()
    last = _LAST_CALL_AT.get(provider_name, 0.0)
    wait = _MIN_INTERVAL_S - (now - last)
    if wait > 0:
        time.sleep(wait)
    _LAST_CALL_AT[provider_name] = time.time()


def _classify_provider_error(exc: Exception) -> str:
    text = str(exc).lower()
    if "429" in text or "rate limit" in text or "too many requests" in text or "限流" in text:
        return "rate_limited"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    return "request_failed"


def _aggregate_error_type(errors: List[Dict[str, object]]) -> str:
    if errors and all(error.get("type") == "rate_limited" for error in errors):
        return "all_providers_rate_limited"
    if any(error.get("type") == "rate_limited" for error in errors):
        return "provider_rate_limited_or_empty"
    return "all_providers_failed"


def _dedupe(items: List[AcademicWork]) -> List[AcademicWork]:
    seen = set()
    deduped = []
    for item in items:
        doi = normalize_doi(item.doi)
        key = doi or _metadata_key(item)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _rank_search_results(query: str, items: List[AcademicWork]) -> List[AcademicWork]:
    if len(items) <= 1:
        return items
    return rank_by_metadata_relevance(query, items, academic_provider_profiles())


def _resolve_fulltext_candidates(request: AcademicSearchRequest, ranked: List[AcademicWork]) -> Dict[str, object]:
    options = dict(request.options or {})
    if options.get("resolve_fulltext") is False:
        return {"strategy": "top_k_direct_read", "applied": False, "reason": "disabled_by_request"}
    if not ranked:
        return {"strategy": "top_k_direct_read", "applied": False, "reason": "no_ranked_items"}

    profiles = academic_provider_profiles()
    top_k = max(0, min(int(options.get("fulltext_top_k", _DEFAULT_FULLTEXT_TOP_K) or 0), 5))
    min_score = float(options.get("fulltext_min_score", _DEFAULT_FULLTEXT_MIN_SCORE) or 0.0)
    if top_k <= 0:
        return {"strategy": "top_k_direct_read", "applied": False, "reason": "top_k_zero"}

    candidates = select_fulltext_candidates(request.query, ranked, top_k=top_k, min_score=min_score)
    attempted: List[Dict[str, object]] = []
    resolved_count = 0
    for candidate in candidates:
        provider_id = _work_provider_id(candidate)
        if not provider_id:
            continue
        profile = profiles.get(provider_id)
        if not profile:
            continue
        if not (profile.content.direct_read_preferred or profile.content.html_fulltext or profile.content.pdf_fulltext):
            continue
        result = extract_academic_fulltext(provider_id, candidate.url, profiles=profiles)
        attempted.append(
            {
                "provider_id": provider_id,
                "url": candidate.url,
                "status": result.status,
                "text_extractable": result.text_extractable,
                "text_length": result.text_length,
                "page_count": result.page_count,
                "degraded_reason": result.degraded_reason,
            }
        )
        if result.status == "PASS" and result.text_extractable:
            resolved_count += 1
            candidate.raw["fulltext_resolution"] = result.to_dict()
            candidate.raw["fulltext_sample"] = result.sample
            _mark_fulltext_resolved(candidate, result.text_length)

    return {
        "strategy": "top_k_direct_read",
        "applied": bool(attempted),
        "candidate_count": len(candidates),
        "attempted_count": len(attempted),
        "resolved_count": resolved_count,
        "attempted": attempted,
    }


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
