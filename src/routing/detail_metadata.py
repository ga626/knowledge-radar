"""Detail routing metadata helpers.

These functions keep the multimodal routing annotations outside the MCP entry
module while preserving the current legacy detail response shape.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from .classifier import build_routing_metadata

log = logging.getLogger("mcp-server")


def attach_routing_metadata(url: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Attach read-only L0 multimodal routing metadata to detail results."""
    if not isinstance(result, dict) or result.get("error"):
        return result
    try:
        result["routing"] = build_routing_metadata(url, result)
    except Exception as exc:
        log.warning(f"多模态路由元信息生成失败: {exc}")
    return result


def routing_recommends_l2(result: Dict[str, Any]) -> bool:
    decision = (result.get("routing") or {}).get("decision") or {}
    return decision.get("recommended_path") == "recommend_l2_video"


def routing_snapshot(result: Dict[str, Any]) -> Dict[str, Any]:
    routing = result.get("routing") if isinstance(result, dict) else None
    if not isinstance(routing, dict):
        return {}
    decision = routing.get("decision") if isinstance(routing.get("decision"), dict) else {}
    return {
        "content_kind": routing.get("content_kind"),
        "recommended_path": decision.get("recommended_path"),
        "should_run_l2": decision.get("should_run_l2"),
        "stage": decision.get("stage"),
        "scores": decision.get("scores") if isinstance(decision.get("scores"), dict) else {},
        "reasons": decision.get("reasons") if isinstance(decision.get("reasons"), list) else [],
        "confidence": decision.get("confidence"),
        "reason_codes": decision.get("reason_codes") if isinstance(decision.get("reason_codes"), list) else [],
    }
