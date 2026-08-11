"""Append-only decision logging for routing and evidence calibration."""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List

from runtime.failure_tags import detect_failure_tags, describe_failure_tags

from .models import utc_now_iso


@dataclass(frozen=True)
class DecisionLogEvent:
    event_type: str
    platform: str
    url: str
    timestamp: str = field(default_factory=utc_now_iso)
    success: bool = True
    strategy: str = ""
    routing: Dict[str, Any] = field(default_factory=dict)
    evidence: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    elapsed_s: float = 0.0
    trace: Dict[str, Any] = field(default_factory=dict)
    failure_tags: List[str] = field(default_factory=list)
    health_layers: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    code_generation: str = ""
    runtime_generation: str = ""
    reproducible: bool | None = None
    first_seen: str = ""
    last_seen: str = ""
    resolved_at: str = ""
    reopened_at: str = ""
    failure_class: str = ""
    evidence_quality: str = ""

    def to_json_line(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":")) + "\n"


class DecisionLogger:
    """Best-effort JSONL logger used by routing and report-quality feedback loops."""

    def __init__(self, path: str):
        self.path = path

    def record(self, event: DecisionLogEvent) -> bool:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(event.to_json_line())
        return True

    def health(self) -> Dict[str, Any]:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write("")
            return {
                "status": "ok",
                "detail": "决策日志可写",
                "path": self.path,
                "schema": "knowledgeradar-decision-log/v1",
            }
        except Exception as exc:
            return {
                "status": "degraded",
                "detail": f"决策日志不可写: {exc}",
                "path": self.path,
                "schema": "knowledgeradar-decision-log/v1",
            }

    def read_recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        if not os.path.isfile(self.path):
            return []
        limit = max(1, min(int(limit or 50), 500))
        with open(self.path, "r", encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
        events: List[Dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    def summarize(self, limit: int = 50) -> Dict[str, Any]:
        events = self.read_recent(limit)
        by_platform = Counter(str(event.get("platform") or "unknown") for event in events)
        by_strategy = Counter(str(event.get("strategy") or "unknown") for event in events)
        by_path = Counter()
        by_kind = Counter()
        by_failure_tag = Counter()
        by_trace_tool = Counter()
        errors = Counter()
        latency_by_platform: Dict[str, List[float]] = defaultdict(list)

        success_count = 0
        by_generation = Counter()
        reproducible_failures = 0
        resolved_failures = 0
        for event in events:
            if event.get("success"):
                success_count += 1
            platform = str(event.get("platform") or "unknown")
            try:
                latency_by_platform[platform].append(float(event.get("elapsed_s") or 0.0))
            except (TypeError, ValueError):
                pass
            routing = event.get("routing") if isinstance(event.get("routing"), dict) else {}
            by_path[str(routing.get("recommended_path") or "unknown")] += 1
            by_kind[str(routing.get("content_kind") or "unknown")] += 1
            error = str(event.get("error") or "")
            if error:
                errors[error[:120]] += 1
            generation = str(event.get("code_generation") or event.get("runtime_generation") or "unknown")
            by_generation[generation] += 1
            if not event.get("success") and event.get("reproducible") is True:
                reproducible_failures += 1
            if event.get("resolved_at"):
                resolved_failures += 1
            for tag in event.get("failure_tags") or detect_failure_tags(error, event.get("metadata", {})):
                by_failure_tag[str(tag)] += 1
            trace = event.get("trace") if isinstance(event.get("trace"), dict) else {}
            if trace.get("tool_name"):
                by_trace_tool[str(trace.get("tool_name"))] += 1

        latency_summary = {}
        for platform, values in latency_by_platform.items():
            clean_values = [value for value in values if value >= 0]
            if not clean_values:
                continue
            latency_summary[platform] = {
                "count": len(clean_values),
                "avg_s": round(sum(clean_values) / len(clean_values), 3),
                "max_s": round(max(clean_values), 3),
            }

        recent_events = [
            {
                "timestamp": event.get("timestamp", ""),
                "platform": event.get("platform", ""),
                "strategy": event.get("strategy", ""),
                "success": bool(event.get("success")),
                "recommended_path": (
                    event.get("routing", {}).get("recommended_path")
                    if isinstance(event.get("routing"), dict)
                    else ""
                ),
                "title": (
                    event.get("evidence", {}).get("metadata", {}).get("title", "")
                    if isinstance(event.get("evidence"), dict)
                    and isinstance(event.get("evidence", {}).get("metadata"), dict)
                    else ""
                ),
                "url": event.get("url", ""),
                "elapsed_s": event.get("elapsed_s", 0),
                "error": event.get("error", ""),
                "failure_tags": event.get("failure_tags", []),
                "trace": event.get("trace", {}),
                "health_layers": event.get("health_layers", {}),
            }
            for event in events[-10:]
        ]

        total = len(events)
        return {
            "schema_version": "knowledgeradar-decision-log-summary/v1",
            "path": self.path,
            "limit": max(1, min(int(limit or 50), 500)),
            "total_events": total,
            "success_count": success_count,
            "failure_count": total - success_count,
            "success_rate": round(success_count / total, 4) if total else None,
            "by_generation": dict(by_generation),
            "reproducible_failure_count": reproducible_failures,
            "resolved_event_count": resolved_failures,
            "by_platform": dict(by_platform),
            "by_strategy": dict(by_strategy),
            "routing": {
                "by_recommended_path": dict(by_path),
                "by_content_kind": dict(by_kind),
            },
            "trace": {
                "by_tool": dict(by_trace_tool),
            },
            "failure_tags": {
                "by_tag": dict(by_failure_tag),
                "details": describe_failure_tags(by_failure_tag.keys()),
            },
            "latency_by_platform": latency_summary,
            "top_errors": [{"error": key, "count": value} for key, value in errors.most_common(5)],
            "recent_events": recent_events,
        }
