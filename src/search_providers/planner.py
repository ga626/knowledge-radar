"""Planning for generic web search provider waves."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List

from .host import host_search_card_summary
from .profile import profile_for, provider_profiles
from .quota import SearchQuotaLedger


GENERAL_WEB_PROVIDERS = ["anysearch", "brave", "exa", "tavily"]


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
    from runtime.search_configuration import current_configuration
    config = current_configuration()
    host_names = list(host_provider_names or [])
    order = ['anysearch', 'ddgs', *host_names]
    eligible = [name for name in dict.fromkeys(order) if config.allowed(name) and _can_attempt(name, provider_status)]
    quota = SearchQuotaLedger().status('tavily')
    if not eligible and config.allowed('tavily') and _can_attempt('tavily', provider_status) and quota.status in {'available','record_only'}:
        eligible = ['tavily']
    return SearchPlan(waves=[[name] for name in eligible], profiles=provider_profiles(),
        quota={'tavily': quota.to_dict()}, host_cards=host_search_card_summary(),
        rationale=['preferred_then_bounded_fallback', 'stop_when_request_coverage_met', 'tavily_only_automatic_paid_fallback'])


def tavily_supplement_available(provider_status: Dict[str, dict]) -> bool:
    _, allowlist = _provider_allowlist()
    from runtime.search_configuration import current_configuration
    return current_configuration().allowed('tavily') and _can_attempt('tavily', provider_status) and SearchQuotaLedger().status('tavily').status in {'available','record_only'}


def _can_attempt(name: str, provider_status: Dict[str, dict]) -> bool:
    row = provider_status.get(name) if isinstance(provider_status.get(name), dict) else {}
    return bool(row.get("available"))


def _provider_allowlist(host_provider_names: List[str] | None = None) -> tuple[str, set[str]]:
    configured = os.environ.get("KR_WEB_SEARCH_PROVIDERS", "").strip()
    configured_names = [item.strip().lower() for item in configured.split(",") if item.strip()] if configured else []
    allowlist = set(configured_names) if configured_names else set(GENERAL_WEB_PROVIDERS)
    allowlist.update(host_provider_names or [])
    return configured, allowlist
