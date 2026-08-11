"""Lightweight strategy-tree descriptors for agent-readable routing."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional


ERROR_TAXONOMY: Dict[str, str] = {
    "CONFIG_MISSING": "Required configuration or credential is missing.",
    "PROVIDER_UNAVAILABLE": "Provider is not reachable or is disabled by health gate.",
    "TIMEOUT": "Provider or collector timed out.",
    "RATE_LIMITED": "Provider returned a rate-limit signal.",
    "AUTH_REQUIRED": "Authentication is required.",
    "LOGIN_REQUIRED": "Platform login state is missing or expired.",
    "COOKIE_EXPIRED": "Stored cookie/session is expired.",
    "CAPTCHA_REQUIRED": "Human verification is required.",
    "ANTI_BOT_BLOCKED": "Request appears blocked by anti-bot controls.",
    "SCHEMA_CHANGED": "Response/page schema no longer matches parser expectations.",
    "PARSE_FAILED": "Collector fetched content but could not parse enough useful data.",
    "DEPENDENCY_CONFLICT": "Runtime package, executable or sidecar dependency is conflicting.",
    "UNKNOWN": "Unclassified failure.",
}


@dataclass(frozen=True)
class StrategyNode:
    id: str
    kind: str
    cost_level: str = "low"
    risk_level: str = "low"
    timeout_s: float = 20.0
    retry_policy: str = "degradation_policy"
    health_gate: str = ""
    breaker_key: str = ""
    input_schema: str = ""
    output_schema: str = ""
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if not data["breaker_key"]:
            data["breaker_key"] = f"{self.kind}:{self.id}"
        return data


@dataclass(frozen=True)
class StrategyTree:
    id: str
    description: str
    max_depth: int
    nodes: List[StrategyNode]
    stop_conditions: List[str] = field(default_factory=list)
    evidence_fields: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": "knowledgeradar-strategy-tree/v1",
            "id": self.id,
            "description": self.description,
            "max_depth": self.max_depth,
            "nodes": [node.to_dict() for node in self.nodes],
            "stop_conditions": list(self.stop_conditions),
            "evidence_fields": list(self.evidence_fields),
        }


def _normalize_names(names: Iterable[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for name in names:
        clean = str(name or "").strip().lower()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        result.append(clean)
    return result


def search_strategy_tree(provider_order: Iterable[str], provider_status: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    status = provider_status or {}
    nodes: List[StrategyNode] = []
    for name in _normalize_names(provider_order):
        item = status.get(name, {})
        configured = bool(item.get("configured", True))
        available = bool(item.get("available", configured))
        cost = "medium" if name == "tavily" else "low"
        risk = "medium" if name in {"brave", "exa"} else "low"
        notes = []
        if not configured:
            notes.append("not_configured")
        if configured and not available:
            notes.append("configured_but_unavailable")
        if name == "anysearch":
            notes.append("runtime backup; public ecosystem evidence is limited")
        if name == "searxng":
            notes.append("local/self-hosted fallback and cross-check node")
        nodes.append(
            StrategyNode(
                id=name,
                kind="search_provider",
                cost_level=cost,
                risk_level=risk,
                timeout_s=15.0,
                health_gate=f"{name}.configured_and_available",
                breaker_key=f"provider:{name}",
                input_schema="WebSearchRequest",
                output_schema="WebSearchResponse",
                notes=notes,
            )
        )
    tree = StrategyTree(
        id="web_search",
        description="Open web search provider fallback tree.",
        max_depth=max(1, len(nodes)),
        nodes=nodes,
        stop_conditions=["first_non_empty_result", "explicit_provider_requested", "all_providers_failed"],
        evidence_fields=["provider", "attempted_providers", "fallback_used", "errors"],
    ).to_dict()
    tree["error_taxonomy"] = ERROR_TAXONOMY
    return tree


def generic_web_strategy_tree(*, use_jina: bool = True, include_dynamic_hint: bool = True) -> Dict[str, Any]:
    collectors = []
    if use_jina:
        collectors.append(
            StrategyNode(
                id="jina_reader",
                kind="web_collector",
                cost_level="low",
                risk_level="low",
                timeout_s=8.0,
                health_gate="network_to_r.jina.ai",
                input_schema="GenericWebRequest",
                output_schema="GenericWebResponse",
            )
        )
    collectors.extend(
        [
            StrategyNode(
                id="trafilatura",
                kind="web_collector",
                cost_level="low",
                risk_level="low",
                timeout_s=20.0,
                health_gate="python_dependency:trafilatura",
                input_schema="GenericWebRequest",
                output_schema="GenericWebResponse",
            ),
            StrategyNode(
                id="readability",
                kind="web_collector",
                cost_level="low",
                risk_level="low",
                timeout_s=20.0,
                health_gate="python_dependency:readability",
                input_schema="GenericWebRequest",
                output_schema="GenericWebResponse",
            ),
            StrategyNode(
                id="static_html",
                kind="web_collector",
                cost_level="low",
                risk_level="low",
                timeout_s=20.0,
                health_gate="python_dependency:bs4+lxml",
                input_schema="GenericWebRequest",
                output_schema="GenericWebResponse",
            ),
        ]
    )
    if include_dynamic_hint:
        collectors.append(
            StrategyNode(
                id="dynamic_playwright",
                kind="web_collector",
                cost_level="medium",
                risk_level="medium",
                timeout_s=25.0,
                health_gate="playwright_available",
                input_schema="GenericWebRequest",
                output_schema="GenericWebResponse",
                notes=["not invoked by extract_web_page; use extract_dynamic_page as explicit high-cost fallback"],
            )
        )
    tree = StrategyTree(
        id="generic_web_extraction",
        description="Open web extraction tree: light static collectors first, browser rendering by explicit escalation.",
        max_depth=len(collectors),
        nodes=collectors,
        stop_conditions=["first_valid_content_over_min_chars", "anti_bot_or_blocked_then_next", "all_collectors_failed"],
        evidence_fields=["collector", "fallback_errors", "elapsed_s", "content_chars"],
    ).to_dict()
    tree["error_taxonomy"] = ERROR_TAXONOMY
    return tree
