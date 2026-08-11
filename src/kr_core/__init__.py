"""KnowledgeRadar platform adapter standard layer."""

from .adapter import DetailStrategy, PlatformAdapter
from .decision_log import DecisionLogEvent, DecisionLogger
from .errors import ErrorCode, KnowledgeRadarError
from .collection import CollectionTrace, StrategyAttempt
from .models import (
    DetailRequest,
    DetailResponse,
    EvidenceItem,
    PlatformCapability,
    SearchRequest,
    SearchResponse,
    SearchResultItem,
)
from .registry import PlatformRegistry, registry
from .strategy import ERROR_TAXONOMY, StrategyNode, StrategyTree, generic_web_strategy_tree, search_strategy_tree

__all__ = [
    "ErrorCode",
    "KnowledgeRadarError",
    "CollectionTrace",
    "StrategyAttempt",
    "DetailRequest",
    "DetailResponse",
    "EvidenceItem",
    "DetailStrategy",
    "DecisionLogEvent",
    "DecisionLogger",
    "PlatformAdapter",
    "PlatformCapability",
    "PlatformRegistry",
    "ERROR_TAXONOMY",
    "SearchRequest",
    "SearchResponse",
    "SearchResultItem",
    "StrategyNode",
    "StrategyTree",
    "generic_web_strategy_tree",
    "registry",
    "search_strategy_tree",
]
