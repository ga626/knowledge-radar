"""Unified multimodal trigger/result helpers."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List


@dataclass
class MultimodalPipeline:
    platform: str
    events: List[Dict[str, Any]] = field(default_factory=list)

    def run(self, name: str, *, trigger: str, enabled: bool, fn: Callable[[], Any]) -> Any:
        started = time.time()
        if not enabled:
            self.events.append({
                "name": name,
                "trigger": trigger,
                "status": "skipped",
                "elapsed_s": 0.0,
            })
            return None
        try:
            value = fn()
            self.events.append({
                "name": name,
                "trigger": trigger,
                "status": "ok",
                "elapsed_s": round(time.time() - started, 3),
                "summary": summarize_value(value),
            })
            return value
        except Exception as exc:
            self.events.append({
                "name": name,
                "trigger": trigger,
                "status": "failed",
                "elapsed_s": round(time.time() - started, 3),
                "error": str(exc),
            })
            raise

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": "knowledgeradar-multimodal-pipeline/v1",
            "platform": self.platform,
            "events": self.events,
        }


def summarize_value(value: Any) -> Dict[str, Any]:
    if value is None:
        return {"type": "none"}
    if isinstance(value, str):
        return {"type": "text", "chars": len(value)}
    if isinstance(value, list):
        return {"type": "list", "count": len(value)}
    if isinstance(value, dict):
        text = value.get("text") or value.get("summary") or value.get("content") or ""
        return {"type": "dict", "keys": sorted(value.keys())[:12], "text_chars": len(str(text))}
    return {"type": type(value).__name__}
