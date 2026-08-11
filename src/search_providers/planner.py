"""Planning for generic web search provider waves."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List

from .host import host_search_card_summary
from .profile import profile_for, provider_profiles
from .quota import SearchQuotaLedger


GENERAL_WEB_PROVIDERS = ["searxng", "anysearch", "brave", "exa", "tavily"]


@dataclass(frozen=True)
class SearchPlan:
    waves: List[List[str]] = field(default_factory=list)
    profiles: Dict[str, dict] = field(default_factory=dict)
    quota: Dict[str, dict] = field(default_factory=dict)
    host_cards: Dict[str, object] = field(default_factory=dict)
    rationale: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "schema": "knowledgeradar-web-search-plan/v1",
            "waves": [list(wave) for wave in self.waves],
            "profiles": dict(self.profiles),
            "quota": dict(self.quota),
            "host_cards": dict(self.host_cards),
            "rationale": list(self.rationale),
        }


def explicit_provider_plan(provider: str) -> SearchPlan:
    normalized = provider.lower().strip()
    profile = profile_for(normalized)
    return SearchPlan(waves=[[normalized]], profiles={normalized: profile}, rationale=["explicit_provider"])


def auto_search_plan(provider_status: Dict[str, dict], host_provider_names: List[str] | None = None) -> SearchPlan:
    configured, allowlist = _provider_allowlist(host_provider_names)
    if configured:
        allowlist.update(host_provider_names or [])

    host_names = list(host_provider_names or [])
    wave_a = [name for name in ["searxng", "anysearch", *host_names] if name in allowlist and _can_attempt(name, provider_status)]
    wave_b = [name for name in ["brave", "exa"] if name in allowlist and _can_attempt(name, provider_status)]
    tavily_status = SearchQuotaLedger().status("tavily")
    wave_c = ["tavily"] if "tavily" in allowlist and _can_attempt("tavily", provider_status) and tavily_status.status == "available" else []
    waves = [wave for wave in [wave_a, wave_b] if wave]
    if not waves and wave_c:
        waves.append(wave_c)
        wave_c = []
    return SearchPlan(
        waves=waves,
        profiles=provider_profiles(),
        quota={"tavily": tavily_status.to_dict()},
        host_cards=host_search_card_summary(),
        rationale=[
            "env_provider_list_is_allowlist" if configured else "default_provider_profile_pool",
            "free_and_quality_parallel_first",
            "tavily_paid_supplement",
        ],
    )


def tavily_supplement_available(provider_status: Dict[str, dict]) -> bool:
    _, allowlist = _provider_allowlist()
    return "tavily" in allowlist and _can_attempt("tavily", provider_status) and SearchQuotaLedger().status("tavily").status == "available"


def _can_attempt(name: str, provider_status: Dict[str, dict]) -> bool:
    row = provider_status.get(name) if isinstance(provider_status.get(name), dict) else {}
    return bool(row.get("available"))


def _provider_allowlist(host_provider_names: List[str] | None = None) -> tuple[str, set[str]]:
    configured = os.environ.get("KR_WEB_SEARCH_PROVIDERS", "").strip()
    configured_names = [item.strip().lower() for item in configured.split(",") if item.strip()] if configured else []
    allowlist = set(configured_names) if configured_names else set(GENERAL_WEB_PROVIDERS)
    if configured:
        allowlist.update(host_provider_names or [])
    return configured, allowlist
