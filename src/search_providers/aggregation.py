"""Result aggregation for generic web search."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List
from urllib.parse import urlsplit

from .models import SearchProviderResult, WebSearchRequest


def canonical_url(url: str) -> str:
    from generic_web.url_policy import discovery_url as canonical
    return canonical(url)


def aggregate_results(provider_items: Dict[str, List[SearchProviderResult]], limit: int | None) -> List[SearchProviderResult]:
    """RRF仅融合排名；稳定tie-break不依赖网络完成顺序，保留全部来源。"""
    from dataclasses import replace
    seen, votes, provenance = {}, {}, {}
    for provider in sorted(provider_items):
        provider_seen = set()
        for rank, item in enumerate(provider_items[provider], 1):
            key = canonical_url(item.url) or f"{item.title.casefold()}::{provider}"
            if key in provider_seen:
                continue
            provider_seen.add(key)
            seen.setdefault(key, item)
            raw = item.raw or {}
            upstream = raw.get('upstream_engine')
            engines = raw.get('engines') or ([raw['engine']] if raw.get('engine') else [])
            family = provider.split(':')[0]
            # Multiple instances do not create independent ranking votes. Known
            # Repeated upstream engines collapse; provenance is retained.
            groups = ['engine:'+str(upstream).lower()] if upstream else (
                ['engine:'+str(e).lower() for e in engines] if engines else [family])
            bucket = votes.setdefault(key, {})
            for group in groups:
                bucket[group] = max(bucket.get(group, 0), 1.0 / (60 + rank))
            provenance.setdefault(key, []).append(provider)
    ranks = {key: sum(value.values()) for key,value in votes.items()}
    ordered = sorted(seen, key=lambda key: (-ranks[key], key))
    if limit is not None:
        ordered = ordered[:max(1, int(limit))]
    return [replace(seen[key], raw={**(seen[key].raw or {}),
                    "provider_provenance": provenance[key], "ranking": "rrf_v1",
                    "rank_score": ranks[key], "ranking_support": sorted(votes[key]),
                    "source_independence": "not_verified", "score_is_truth_confidence": False}) for key in ordered]


@dataclass(frozen=True)
class CoverageDecision:
    sufficient: bool
    triggers: List[str]

    def to_dict(self) -> Dict[str, object]:
        return {"sufficient": self.sufficient, "triggers": list(self.triggers)}


def coverage_decision(request: WebSearchRequest, items: Iterable[SearchProviderResult], successful_providers: List[str]) -> CoverageDecision:
    rows = list(items)
    triggers: List[str] = []
    min_results = int((request.options or {}).get("min_results") or min(max(1, request.limit), 5))
    if len(rows) < min_results:
        triggers.append("unique_results_below_threshold")
    if not successful_providers:
        triggers.append("no_successful_free_or_quality_provider")
    domains = {urlsplit(item.url).netloc.lower() for item in rows if item.url}
    if request.options.get('require_diverse_sources') and min_results > 1 and len(rows) >= min_results and len(domains) <= 1:
        triggers.append("domain_diversity_low")
    if request.freshness and not any(item.published_at for item in rows):
        triggers.append("freshness_required_missing")
    return CoverageDecision(sufficient=not triggers, triggers=triggers)
