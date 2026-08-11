"""Recruitment search result fusion helpers.

This module is intentionally side-effect free. Platform collectors can keep
their current behavior while probes or future parallel search code reuse this
scoring layer to merge already-returned candidates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlsplit

from kr_core.source_capability import VALID_NO_MATCH_FOR_SOURCE_TYPE
from runtime.recruitment_address import attach_recruitment_address_contract
from runtime.recruitment_sources import (
    is_structured_job_platform,
    normalize_recruitment_source,
    recruitment_empty_result_reason,
    recruitment_source_capability,
)


TRUST_BY_PLATFORM = {
    "liepin": 0.82,
    "猎聘": 0.82,
    "boss": 0.78,
    "BOSS直聘": 0.78,
    "zhilian": 0.72,
    "智联招聘": 0.72,
    "v2ex": 0.66,
    "V2EX": 0.66,
    "maimai": 0.54,
    "脉脉": 0.54,
}

STRATEGY_ADJUSTMENT = {
    "direct": 0.08,
    "chrome_cdp_page": 0.06,
    "stealth_cdp_page": 0.04,
    "persistent_profile": 0.04,
    "http_api": 0.03,
    "web_search_fallback": -0.12,
}


@dataclass(frozen=True)
class FusionWeights:
    trust: float = 0.35
    freshness: float = 0.2
    completeness: float = 0.25
    consistency: float = 0.2


def _text(value: Any) -> str:
    return str(value or "").strip()


def _norm_platform(platform: str) -> str:
    return normalize_recruitment_source(platform)


def _canonical_url(url: str) -> str:
    try:
        parsed = urlsplit(url.strip())
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"
    except Exception:
        return url.strip()


def _candidate_key(item: dict[str, Any]) -> str:
    url = _canonical_url(_text(item.get("url")))
    if url:
        return url
    title = _text(item.get("title") or item.get("position") or item.get("job_name")).lower()
    company = _text(item.get("company") or item.get("company_name") or item.get("author")).lower()
    city = _text(item.get("city") or item.get("location")).lower()
    return "::".join(part for part in (title, company, city) if part)


def _item_platform(item: dict[str, Any], fallback: str = "") -> str:
    return _text(item.get("platform") or item.get("source_platform") or fallback)


def _metadata_strategy(item: dict[str, Any]) -> str:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    return _text(item.get("strategy") or item.get("source") or metadata.get("strategy"))


def _completeness(item: dict[str, Any]) -> float:
    fields = (
        "title",
        "position",
        "job_name",
        "company",
        "company_name",
        "salary",
        "city",
        "location",
        "url",
        "desc",
        "snippet",
        "summary",
    )
    present = sum(1 for field in fields if _text(item.get(field)))
    return min(1.0, present / 7.0)


def _freshness(item: dict[str, Any]) -> float:
    text = " ".join(
        _text(item.get(field))
        for field in ("published_at", "updated_at", "date", "time", "retrieved_at", "snippet", "desc")
    )
    if any(marker in text for marker in ("今天", "刚刚", "分钟前", "小时前", "2026")):
        return 1.0
    if any(marker in text for marker in ("昨天", "近", "2025")):
        return 0.75
    return 0.45 if text else 0.35


def _trust(item: dict[str, Any], platform: str) -> float:
    base = TRUST_BY_PLATFORM.get(platform, TRUST_BY_PLATFORM.get(_item_platform(item), 0.5))
    strategy = _metadata_strategy(item)
    return max(0.0, min(1.0, base + STRATEGY_ADJUSTMENT.get(strategy, 0.0)))


def _consistency(provenance: list[str]) -> float:
    unique = {_norm_platform(item) for item in provenance if item}
    if len(unique) >= 3:
        return 1.0
    if len(unique) == 2:
        return 0.72
    return 0.42


def _score(item: dict[str, Any], *, platform: str, provenance: list[str], weights: FusionWeights) -> float:
    value = (
        weights.trust * _trust(item, platform)
        + weights.freshness * _freshness(item)
        + weights.completeness * _completeness(item)
        + weights.consistency * _consistency(provenance)
    )
    return round(max(0.0, min(1.0, value)), 4)


def _merge_item(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if key in {"fusion_score", "fusion"}:
            continue
        if not _text(merged.get(key)) and value not in (None, "", []):
            merged[key] = value
    return merged


def _degraded_source(provider: str, reason: str = "") -> dict[str, Any]:
    capability = recruitment_source_capability(provider)
    return {
        "platform": provider,
        "reason": reason or recruitment_empty_result_reason(provider),
        "source_type": capability.source_type,
        "expected_outputs": list(capability.native_outputs),
        "empty_semantics": capability.empty_semantics,
    }


def _apply_source_capability(item: dict[str, Any], platform: str) -> dict[str, Any]:
    capability = recruitment_source_capability(platform)
    item = attach_recruitment_address_contract(item, platform=platform)
    policy = capability.claim_policy.to_dict()
    item.setdefault("source_type", capability.source_type)
    item.setdefault("native_outputs", list(capability.native_outputs))
    item.setdefault("source_claim_policy", policy)
    item.setdefault("opportunity_signal_allowed", policy["opportunity_signal_allowed"])

    if is_structured_job_platform(platform):
        item.setdefault("market_claim_allowed", policy["market_claim_allowed"])
        item.setdefault("salary_claim_allowed", policy["salary_claim_allowed"])
        item.setdefault("representative_claim_allowed", policy["representative_claim_allowed"])
    else:
        item["market_claim_allowed"] = False
        item["salary_claim_allowed"] = False
        item["representative_claim_allowed"] = False
    return item


def fuse_recruitment_results(
    provider_items: dict[str, Iterable[dict[str, Any]]],
    *,
    limit: int = 10,
    weights: FusionWeights | None = None,
) -> dict[str, Any]:
    """Deduplicate and score recruitment candidates from multiple platforms."""
    weights = weights or FusionWeights()
    by_key: dict[str, dict[str, Any]] = {}
    degraded_sources: list[dict[str, Any]] = []
    source_boundaries: list[dict[str, Any]] = []

    for provider, raw_items in provider_items.items():
        items = list(raw_items or [])
        if not items:
            record = _degraded_source(provider)
            if record["empty_semantics"] == VALID_NO_MATCH_FOR_SOURCE_TYPE:
                source_boundaries.append(record)
            else:
                degraded_sources.append(record)
            continue
        for raw_item in items:
            if not isinstance(raw_item, dict):
                continue
            item = dict(raw_item)
            platform = _norm_platform(_item_platform(item, provider))
            item.setdefault("platform", provider)
            item = _apply_source_capability(item, platform)
            key = _candidate_key(item)
            if not key:
                degraded_sources.append(_degraded_source(provider, "candidate_missing_identity"))
                continue
            if key in by_key:
                current = by_key[key]
                provenance = list(current["fusion"]["platform_provenance"])
                if platform not in provenance:
                    provenance.append(platform)
                merged = _merge_item(current, item)
                merged["fusion"] = {**current["fusion"], "platform_provenance": provenance}
                by_key[key] = merged
            else:
                item["fusion"] = {
                    "dedupe_key": key,
                    "platform_provenance": [platform],
                    "strategy": _metadata_strategy(item),
                }
                by_key[key] = item

    fused: list[dict[str, Any]] = []
    for item in by_key.values():
        platform = _norm_platform(_item_platform(item))
        provenance = list(item["fusion"]["platform_provenance"])
        score = _score(item, platform=platform, provenance=provenance, weights=weights)
        item["fusion_score"] = score
        item["fusion"] = {
            **item["fusion"],
            "trust": _trust(item, platform),
            "freshness": _freshness(item),
            "completeness": _completeness(item),
            "consistency": _consistency(provenance),
        }
        fused.append(item)

    fused.sort(key=lambda item: item.get("fusion_score", 0), reverse=True)
    return {
        "schema": "knowledgeradar-recruitment-fusion/v1",
        "items": fused[: max(1, int(limit or 10))],
        "total": len(fused),
        "degraded_sources": degraded_sources,
        "source_boundaries": source_boundaries,
        "weights": weights.__dict__,
    }
