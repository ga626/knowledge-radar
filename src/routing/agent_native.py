"""Agent-native detail response annotations."""

from __future__ import annotations

from typing import Any, Dict, List


def build_agent_native_fields(data: Dict[str, Any], *, strategy: str = "") -> Dict[str, Any]:
    routing = data.get("routing") if isinstance(data.get("routing"), dict) else {}
    decision = routing.get("decision") if isinstance(routing.get("decision"), dict) else {}
    pipeline = data.get("multimodal_pipeline") if isinstance(data.get("multimodal_pipeline"), dict) else {}
    events = pipeline.get("events") if isinstance(pipeline.get("events"), list) else []
    retry_history = _retry_history(data, events)
    return {
        "routing_reason": {
            "recommended_path": decision.get("recommended_path") or "",
            "confidence": decision.get("confidence"),
            "reason_codes": decision.get("reason_codes") if isinstance(decision.get("reason_codes"), list) else [],
            "reasons": decision.get("reasons") if isinstance(decision.get("reasons"), list) else [],
        },
        "strategy_chain": _strategy_chain(data, strategy, events),
        "retry_history": retry_history,
        "recommended_next_action": _recommended_next_action(data, decision, retry_history),
    }


def _strategy_chain(data: Dict[str, Any], strategy: str, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    chain = []
    if strategy:
        chain.append({"name": strategy, "status": "ok" if not data.get("error") else "failed"})
    for event in events:
        chain.append({
            "name": str(event.get("name") or ""),
            "status": str(event.get("status") or "unknown"),
            "trigger": str(event.get("trigger") or ""),
        })
    return chain


def _retry_history(data: Dict[str, Any], events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    history = []
    failure_type = data.get("failure_type") or ""
    if failure_type:
        history.append({"stage": "detail", "status": "failed", "failure_type": failure_type})
    for event in events:
        if event.get("status") == "failed":
            history.append({"stage": event.get("name"), "status": "failed", "error": event.get("error")})
    return history


def _recommended_next_action(data: Dict[str, Any], decision: Dict[str, Any], retry_history: List[Dict[str, Any]]) -> str:
    if data.get("manual_action_required"):
        return "manual_platform_verification"
    if data.get("error"):
        return "retry_or_use_alternate_source" if retry_history else "inspect_error"
    if decision.get("recommended_path") == "recommend_l2_video" and not data.get("deep_analysis"):
        return "run_detail_with_auto_multimodal"
    if decision.get("recommended_path") == "need_more_probe":
        return "run_keyframe_or_detail_probe"
    return "use_as_evidence"
