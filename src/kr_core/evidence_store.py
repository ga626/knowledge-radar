"""Append-only evidence repository for search and detail reuse."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List

from .models import utc_now_iso
from runtime.paths import runtime_log_dir


def default_evidence_store_path() -> str:
    return os.path.join(str(runtime_log_dir()), "knowledgeradar-evidence-store.jsonl")


@dataclass(frozen=True)
class EvidenceRecord:
    record_type: str
    platform: str
    url: str
    payload: Dict[str, Any]
    query: str = ""
    timestamp: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        key_source = f"{self.record_type}|{self.platform}|{self.url}|{self.query}"
        return {
            "schema": "knowledgeradar-evidence-store/v1",
            "id": hashlib.sha256(key_source.encode("utf-8", errors="ignore")).hexdigest()[:24],
            "record_type": self.record_type,
            "platform": self.platform,
            "url": self.url,
            "query": self.query,
            "timestamp": self.timestamp,
            "payload": self.payload,
        }


class EvidenceStore:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or default_evidence_store_path()

    def append(self, record: EvidenceRecord) -> bool:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n")
        return True

    def append_search(self, *, platform: str, query: str, response: Dict[str, Any]) -> None:
        trace = _current_trace()
        for item in response.get("items") or []:
            if not isinstance(item, dict):
                continue
            self.append(EvidenceRecord("search_result", platform, str(item.get("url") or ""), _with_trace(item, trace), query=query))

    def append_detail(self, *, platform: str, url: str, response: Dict[str, Any]) -> None:
        self.append(EvidenceRecord("detail", platform, url, _with_trace(_compact_detail(response), _current_trace())))

    def append_academic_search(self, *, query: str, response: Dict[str, Any]) -> None:
        trace = _current_trace()
        for item in response.get("items") or []:
            if not isinstance(item, dict):
                continue
            self.append(EvidenceRecord("academic_search_result", "academic", str(item.get("url") or item.get("doi") or ""), _with_trace(item, trace), query=query))

    def academic_recent_summary(self, limit: int = 10) -> Dict[str, Any]:
        rows = [row for row in self.recent(max(limit * 4, 20)) if row.get("record_type") == "academic_search_result"]
        rows = rows[-max(1, min(limit, 50)):]
        return {
            "status": "ok",
            "count": len(rows),
            "recent": [
                {
                    "query": row.get("query", ""),
                    "url": row.get("url", ""),
                    "title": ((row.get("payload") or {}).get("title") or ""),
                    "doi": ((row.get("payload") or {}).get("doi") or ""),
                    "source": ((row.get("payload") or {}).get("source") or ""),
                    "retrieved_at": ((row.get("payload") or {}).get("retrieved_at") or row.get("timestamp", "")),
                }
                for row in rows
            ],
        }

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        if not os.path.isfile(self.path):
            return []
        with open(self.path, "r", encoding="utf-8") as f:
            lines = f.readlines()[-max(1, min(limit, 500)):]
        rows = []
        for line in lines:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def health(self) -> Dict[str, Any]:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write("")
            return {"status": "ok", "path": self.path, "schema": "knowledgeradar-evidence-store/v1"}
        except Exception as exc:
            return {"status": "degraded", "path": self.path, "detail": str(exc)}


def _compact_detail(response: Dict[str, Any]) -> Dict[str, Any]:
    keep = {
        "platform",
        "url",
        "title",
        "desc",
        "content",
        "transcript",
        "author",
        "routing_reason",
        "recommended_next_action",
        "evidence",
        "detail_metadata",
    }
    data = {key: response.get(key) for key in keep if key in response}
    if isinstance(data.get("content"), str):
        data["content"] = data["content"][:4000]
    if isinstance(data.get("transcript"), str):
        data["transcript"] = data["transcript"][:4000]
    return data


def _current_trace() -> Dict[str, Any]:
    try:
        from runtime.tool_trace import current_tool_trace

        trace = current_tool_trace()
        return trace if isinstance(trace, dict) else {}
    except Exception:
        return {}


def _with_trace(payload: Dict[str, Any], trace: Dict[str, Any]) -> Dict[str, Any]:
    if not trace:
        return payload
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    return {
        **payload,
        "metadata": {
            **metadata,
            "trace_id": trace.get("trace_id", ""),
            "trace_tool": trace.get("tool_name", ""),
            "trace_strategy": trace.get("strategy", ""),
        },
    }
