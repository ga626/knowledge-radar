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
        from generic_web.url_policy import canonical_url, public_evidence_payload
        url = canonical_url(self.url)
        key_source = f"{self.record_type}|{self.platform}|{url}|{self.query}"
        return {
            "schema": "knowledgeradar-evidence-store/v1",
            "id": hashlib.sha256(key_source.encode("utf-8", errors="ignore")).hexdigest()[:24],
            "record_type": self.record_type,
            "platform": self.platform,
            "url": url,
            "query": self.query,
            "timestamp": self.timestamp,
            "payload": public_evidence_payload(self.payload),
        }


class EvidenceStore:
    def __init__(self, path: str | None = None) -> None:
        from .evidence_assets import EvidenceAssets
        self.path = path or default_evidence_store_path()
        self.assets = EvidenceAssets(os.path.join(os.path.dirname(path), 'evidence_assets') if path else None)

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
        from .evidence_coverage import content_coverage
        from .evidence_locators import document_text, transcript_locators
        if isinstance(response.get('document_blocks'), list):
            body, locations = document_text(response['document_blocks'])
            if body:
                response['content'] = body
                response['content_locators'] = locations
        refs = []
        for kind in ("content", "transcript"):
            value = response.get(kind)
            if isinstance(value, str) and value.strip():
                if response.get("evidence_storage_allowed") is False:
                    response.setdefault("evidence_storage_gaps", []).append({"part": kind, "reason": "storage_not_permitted"})
                    continue
                try:
                    metadata = {"extractor": str((response.get("detail_metadata") or {}).get("strategy") or response.get("collector") or platform), "coverage": "unknown"}
                    locations = response.get(kind + '_locators')
                    if kind == 'transcript':
                        mapping = response.get('transcript_metadata') or {}
                        if isinstance(mapping.get('locators'), dict): mapping = mapping['locators']
                        locations = mapping.get('segments') if mapping.get('status') == 'aligned' else None
                        if locations is None:
                            mapped = transcript_locators(value, mapping.get('segments'))
                            if mapped['status'] == 'aligned': locations = mapped['segments']
                    if isinstance(locations, list): metadata['locators'] = locations
                    refs.append(self.assets.put(url, value, kind=kind, title=str(response.get("title") or ""), metadata=metadata))
                    if kind == "transcript" and isinstance(response.get("transcript_metadata"), dict):
                        refs.append(self.assets.put(url, json.dumps(response["transcript_metadata"], ensure_ascii=False), kind="transcript_map", metadata={"extractor": platform, "coverage": "segment_mapping_only"}))
                except (OSError, ValueError) as exc:
                    response.setdefault("evidence_storage_gaps", []).append({"part": kind, "reason": str(exc)})
        if response.get('evidence_storage_allowed') is not False:
            for part in ('comments', 'images', 'tables', 'media_manifest'):
                entries = response.get(part)
                if not isinstance(entries, list) or not entries:
                    continue
                try:
                    # 原始结构另存固定资产；URL 清单绝不冒充媒体二进制。
                    from generic_web.url_policy import public_evidence_payload
                    blocks = []
                    for index, entry in enumerate(entries):
                        value = entry if isinstance(entry, dict) else {'value': entry}
                        block = {'text': json.dumps(public_evidence_payload(value), ensure_ascii=False), 'kind': part}
                        if part == 'comments': block['comment_id'] = str(value.get('comment_id') or value.get('id') or index)
                        if part == 'images': block['image_index'] = index
                        blocks.append(block)
                    text, locations = document_text(blocks)
                    refs.append(self.assets.put(url, text, kind=part, metadata={'extractor': platform, 'coverage': 'manifest_only' if part in ('images','media_manifest') else 'partial', 'locators': locations}))
                except (OSError, ValueError) as exc:
                    response.setdefault('evidence_storage_gaps', []).append({'part': part, 'reason': str(exc)})
        response["evidence_assets"] = refs
        response.setdefault("content_coverage", content_coverage(response))
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
        "evidence_assets",
        "content_coverage",
        "evidence_storage_gaps",
    }
    data = {key: response.get(key) for key in keep if key in response}
    if isinstance(data.get("content"), str):
        data["content"] = data["content"][:4000]
    if isinstance(data.get("transcript"), str):
        data["transcript"] = data["transcript"][:4000]
    if response.get("evidence_storage_allowed") is False:
        for part in ("content", "transcript"):
            data.pop(part, None)
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
