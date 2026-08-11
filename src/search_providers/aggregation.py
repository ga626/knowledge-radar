"""Result aggregation for generic web search."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import SearchProviderResult, WebSearchRequest


def canonical_url(url: str) -> str:
    try:
        parsed = urlsplit(url.strip())
        query = urlencode(
            [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if not key.lower().startswith("utm_")]
        )
        path = parsed.path.rstrip("/") or parsed.path
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, query, ""))
    except Exception:
        return url.strip()


def aggregate_results(provider_items: Dict[str, List[SearchProviderResult]], limit: int) -> List[SearchProviderResult]:
    seen: Dict[str, SearchProviderResult] = {}
    for provider, items in provider_items.items():
        for item in items:
            key = canonical_url(item.url) or f"{item.title.lower()}::{provider}"
            if key in seen:
                existing = seen[key]
                provenance = list((existing.raw or {}).get("provider_provenance") or [existing.source_provider])
                if provider not in provenance:
                    provenance.append(provider)
                raw = dict(existing.raw or {})
                raw["provider_provenance"] = provenance
                seen[key] = SearchProviderResult(
                    title=existing.title,
                    url=existing.url,
                    snippet=existing.snippet,
                    source_provider=existing.source_provider,
                    published_at=existing.published_at,
                    retrieved_at=existing.retrieved_at,
                    score=existing.score,
                    raw=raw,
                )
                continue
            raw = dict(item.raw or {})
            raw.setdefault("provider_provenance", [provider])
            seen[key] = SearchProviderResult(
                title=item.title,
                url=item.url,
                snippet=item.snippet,
                source_provider=item.source_provider or provider,
                published_at=item.published_at,
                retrieved_at=item.retrieved_at,
                score=item.score,
                raw=raw,
            )
    return list(seen.values())[: max(1, int(limit or 5))]


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
    if min_results > 1 and len(rows) >= min_results and len(domains) <= 1:
        triggers.append("domain_diversity_low")
    if request.freshness and not any(item.published_at for item in rows):
        triggers.append("freshness_required_missing")
    return CoverageDecision(sufficient=not triggers, triggers=triggers)
