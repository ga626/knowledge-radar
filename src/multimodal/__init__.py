"""Compatibility re-exports for legacy imports."""

from routing import (
    ContentEnvelope,
    ContentKind,
    L1Signal,
    L1Snapshot,
    ModalitySignals,
    RouteDecision,
    SignalStatus,
    build_routing_metadata,
    classify_content,
    decide_route,
)

__all__ = [
    "ContentEnvelope",
    "ContentKind",
    "L1Signal",
    "L1Snapshot",
    "ModalitySignals",
    "RouteDecision",
    "SignalStatus",
    "build_routing_metadata",
    "classify_content",
    "decide_route",
]
