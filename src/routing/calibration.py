"""Self-evolution calibration suggestions from evidence and decision logs."""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List


def build_calibration_report(*, evidence_rows: List[Dict[str, Any]], decision_summary: Dict[str, Any]) -> Dict[str, Any]:
    record_types = Counter(str(row.get("record_type") or "") for row in evidence_rows)
    failure_tags = ((decision_summary or {}).get("failure_tags") or {}).get("by_tag") or {}
    trace_tools = ((decision_summary or {}).get("trace") or {}).get("by_tool") or {}
    suggestions = []

    if failure_tags.get("empty_detail", 0) or failure_tags.get("dead_link", 0):
        suggestions.append({
            "target": "detail_prefilter",
            "action": "tighten_url_preflight",
            "reason": "近期详情失败中出现 dead_link/empty_detail",
            "confidence": 0.7,
        })
    if failure_tags.get("anti_bot_verification", 0):
        suggestions.append({
            "target": "xiaohongshu_fallback",
            "action": "prefer_external_search_then_detail_temporarily",
            "reason": "近期出现平台验证标签",
            "confidence": 0.75,
        })
    if record_types.get("search_result", 0) >= 3 and not failure_tags:
        suggestions.append({
            "target": "routing_threshold",
            "action": "keep_current_thresholds",
            "reason": "证据仓库有稳定搜索样本且近期无失败标签",
            "confidence": 0.8,
        })

    return {
        "schema_version": "knowledgeradar-calibration/v1",
        "mode": "advisory",
        "evidence_records": dict(record_types),
        "failure_tags": failure_tags,
        "trace_tools": trace_tools,
        "suggestions": suggestions,
    }
