"""Capability profiles for generic web search providers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List


@dataclass(frozen=True)
class SearchProviderProfile:
    id: str
    kind: str = "general_web"
    cost_tier: str = "free_or_self_hosted"
    default_wave: str = "free_parallel"
    strengths: List[str] = field(default_factory=list)
    weaknesses: List[str] = field(default_factory=list)
    capabilities: Dict[str, Any] = field(default_factory=dict)
    runtime: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


DEFAULT_PROVIDER_PROFILES: Dict[str, SearchProviderProfile] = {
    "searxng": SearchProviderProfile(
        id="searxng",
        cost_tier="free_or_self_hosted",
        default_wave="free_parallel",
        strengths=["broad_metasearch", "low_cost", "self_hosted"],
        weaknesses=["backend_dependent", "can_be_noisy"],
        capabilities={"freshness": "weak", "domain_filter": False, "raw_content": False, "citations": False},
        runtime={"speed": "medium", "stability": "medium", "timeout_s": 12},
    ),
    "anysearch": SearchProviderProfile(
        id="anysearch",
        cost_tier="free_or_custom",
        default_wave="free_parallel",
        strengths=["custom_backend", "low_cost"],
        weaknesses=["deployment_specific", "configured_unverified"],
        capabilities={"freshness": "depends", "domain_filter": "depends", "raw_content": False, "citations": False},
        runtime={"speed": "medium", "stability": "unknown", "timeout_s": 15},
    ),
    "brave": SearchProviderProfile(
        id="brave",
        cost_tier="paid_or_free_quota",
        default_wave="quality_parallel",
        strengths=["independent_index", "general_web", "fresh_web"],
        weaknesses=["requires_api_key"],
        capabilities={"freshness": True, "domain_filter": False, "raw_content": False, "citations": False},
        runtime={"speed": "fast", "stability": "high", "timeout_s": 12},
    ),
    "exa": SearchProviderProfile(
        id="exa",
        cost_tier="paid_or_free_quota",
        default_wave="quality_parallel",
        strengths=["semantic_search", "research_discovery", "similar_content"],
        weaknesses=["requires_api_key", "semantic_results_need_dedup"],
        capabilities={"freshness": True, "domain_filter": True, "raw_content": True, "citations": False},
        runtime={"speed": "medium", "stability": "high", "timeout_s": 15},
    ),
    "tavily": SearchProviderProfile(
        id="tavily",
        cost_tier="paid_limited",
        default_wave="paid_supplement",
        strengths=["stable_summary", "broad_web", "raw_content_optional"],
        weaknesses=["monthly_quota_limited", "paid"],
        capabilities={"freshness": True, "domain_filter": True, "raw_content": True, "citations": False},
        runtime={"speed": "medium", "stability": "high", "timeout_s": 15},
    ),
}


def provider_profiles() -> Dict[str, Dict[str, Any]]:
    return {name: profile.to_dict() for name, profile in DEFAULT_PROVIDER_PROFILES.items()}


def profile_for(provider_id: str) -> Dict[str, Any]:
    profile = DEFAULT_PROVIDER_PROFILES.get(provider_id)
    return profile.to_dict() if profile else {}
