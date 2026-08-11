"""Routing helpers for KnowledgeRadar detail processing."""

from .classifier import build_routing_metadata, classify_content
from .detail_metadata import attach_routing_metadata, routing_recommends_l2, routing_snapshot
from .agent_native import build_agent_native_fields
from .models import (
    ContentEnvelope,
    ContentKind,
    L1Signal,
    L1Snapshot,
    ModalitySignals,
    RouteDecision,
    SignalStatus,
)
from .router import decide_route

__all__ = [
    "ContentEnvelope",
    "ContentKind",
    "L1Signal",
    "L1Snapshot",
    "ModalitySignals",
    "RouteDecision",
    "SignalStatus",
    "attach_routing_metadata",
    "build_routing_metadata",
    "classify_content",
    "decide_route",
    "routing_recommends_l2",
    "routing_snapshot",
    "build_agent_native_fields",
]
