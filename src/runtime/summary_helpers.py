"""Small summary builders shared by server-facing runtime checks."""

from __future__ import annotations

from typing import Any, Dict, List


def _latency_sla_from_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    latencies = summary.get("latency_by_platform") or {}
    max_latency = 0
    slow_platforms = []
    for platform, data in latencies.items():
        if not isinstance(data, dict):
            continue
        max_s = data.get("max_s") or data.get("max") or 0
        avg_s = data.get("avg_s") or data.get("avg") or 0
        try:
            max_latency = max(max_latency, float(max_s or 0))
            if float(max_s or 0) >= 60 or float(avg_s or 0) >= 30:
                slow_platforms.append({"platform": platform, "avg_s": avg_s, "max_s": max_s})
        except Exception:
            continue
    return {
        "max_latency_s": round(max_latency, 3),
        "slow_platforms": slow_platforms[:5],
    }


def decision_logs_compact_from_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "schema_version": "knowledgeradar-decision-log-compact/v1",
        "status": "ok",
        "limit": summary.get("limit"),
        "total_events": summary.get("total_events"),
        "success_count": summary.get("success_count"),
        "failure_count": summary.get("failure_count"),
        "success_rate": summary.get("success_rate"),
        "by_platform": summary.get("by_platform", {}),
        "by_strategy": summary.get("by_strategy", {}),
        "failure_tags": (summary.get("failure_tags") or {}).get("by_tag", {}),
        "top_errors": summary.get("top_errors", [])[:3],
        "latency_by_platform": summary.get("latency_by_platform", {}),
        "sla": _latency_sla_from_summary(summary),
    }


def task_status_compact_from_summary(summary: Dict[str, Any], stale: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "schema_version": "knowledgeradar-task-status-compact/v1",
        "status": "ok",
        "summary": {
            "total": summary.get("total"),
            "counts": summary.get("counts", {}),
            "active": summary.get("active"),
            "active_oldest_age_s": summary.get("active_oldest_age_s"),
            "stale_count": summary.get("stale_count"),
            "by_platform": summary.get("by_platform", [])[:5],
            "by_task_type": summary.get("by_task_type", [])[:5],
            "by_error_code": summary.get("by_error_code", [])[:5],
            "unknown_error_count": summary.get("unknown_error_count", 0),
        },
        "recent_failed": [
            {
                "task_id": task.get("task_id"),
                "platform": task.get("platform"),
                "task_type": task.get("task_type"),
                "error_code": task.get("error_code"),
                "error": str(task.get("error") or "")[:160],
            }
            for task in (summary.get("recent_failed") or [])[:3]
        ],
        "stale": [
            {
                "task_id": task.get("task_id"),
                "platform": task.get("platform"),
                "task_type": task.get("task_type"),
                "status": task.get("status"),
                "updated_at": task.get("updated_at"),
            }
            for task in stale[:3]
        ],
    }


def low_risk_execution_probe_declaration() -> Dict[str, Any]:
    return {
        "schema": "knowledgeradar-low-risk-execution-summary/v1",
        "status": "not_executed",
        "execution_mode": "explicit_probe_only",
        "probe_mode": "health_check(mode='low_risk_execution_probe')",
        "allowed_samples": ["search_academic", "extract_web_page"],
        "notes": [
            "summary mode stays lightweight and does not execute low-risk network samples",
            "run explicit probe mode to produce ExecutionEnvelope and trace/evidence ledger",
        ],
    }


def overall_status_from_summary(checks: Dict[str, Dict[str, Any]]) -> str:
    if _summary_has_no_current_blockers(checks):
        return "ok"
    statuses = [str(item.get("status") or "unknown") for item in checks.values()]
    if any(status == "down" for status in statuses):
        return "down"
    if any(status in {"degraded", "error"} for status in statuses):
        return "degraded"
    if any(status == "unknown" for status in statuses):
        return "degraded"
    return "ok"


_NON_BLOCKING_SUMMARY_STATUSES = {
    "",
    "not_executed",
    "recommendation_only",
    "awaiting_api_key",
    "ready_for_readonly_observation",
    "partial_pass",
    "ready_for_design",
    "ready",
    "design_ready",
    "not_applicable",
    "skipped",
    "blocked",
}


def _summary_has_no_current_blockers(checks: Dict[str, Dict[str, Any]]) -> bool:
    degradation = checks.get("degradation_summary") or {}
    task_queue = checks.get("task_queue") or {}
    tool_surface = checks.get("tool_surface") or {}
    if str(tool_surface.get("status") or "") != "ok":
        return False
    if int(degradation.get("recent_count") or 0) > 0:
        return False
    for name, item in checks.items():
        status = str((item or {}).get("status") or "unknown")
        if status in _NON_BLOCKING_SUMMARY_STATUSES:
            continue
        if status == "down":
            return False
        if status in {"degraded", "error"} and name not in {
            "xiaohongshu_detail_health",
            "xiaohongshu_chain_health",
            "xiaohongshu_budget",
        }:
            return False
    return True
