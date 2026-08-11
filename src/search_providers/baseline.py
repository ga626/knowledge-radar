"""Search baseline helpers used by optimization work."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List
from urllib.parse import urlparse


@dataclass(frozen=True)
class SearchQualitySnapshot:
    query: str
    total: int
    unique_domains: int
    source_ecologies: List[str] = field(default_factory=list)
    strong_or_contextual_evidence_count: int = 0
    attempted_providers: List[str] = field(default_factory=list)
    elapsed_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": "knowledgeradar-search-quality-snapshot/v1",
            "query": self.query,
            "total": self.total,
            "unique_domains": self.unique_domains,
            "source_ecologies": list(self.source_ecologies),
            "strong_or_contextual_evidence_count": self.strong_or_contextual_evidence_count,
            "attempted_providers": list(self.attempted_providers),
            "elapsed_ms": round(float(self.elapsed_ms), 3),
        }


def snapshot_from_response(query: str, response: Dict[str, Any], *, elapsed_ms: float = 0.0) -> Dict[str, Any]:
    items = response.get("items") if isinstance(response.get("items"), list) else []
    domains = set()
    ecologies = []
    evidence_count = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        host = urlparse(str(item.get("url") or "")).netloc.lower()
        if host:
            domains.add(host)
        ecology = str(item.get("source_ecology") or "")
        if ecology and ecology not in ecologies:
            ecologies.append(ecology)
        strength = str(item.get("evidence_strength") or "")
        if strength and not strength.startswith("weak"):
            evidence_count += 1
    return SearchQualitySnapshot(
        query=query,
        total=len(items),
        unique_domains=len(domains),
        source_ecologies=ecologies,
        strong_or_contextual_evidence_count=evidence_count,
        attempted_providers=[str(value) for value in response.get("attempted_providers") or []],
        elapsed_ms=elapsed_ms,
    ).to_dict()


def compare_snapshots(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "schema": "knowledgeradar-search-baseline-comparison/v1",
        "status": "ok",
        "query": after.get("query") or before.get("query") or "",
        "deltas": {
            "total": int(after.get("total") or 0) - int(before.get("total") or 0),
            "unique_domains": int(after.get("unique_domains") or 0) - int(before.get("unique_domains") or 0),
            "strong_or_contextual_evidence_count": int(after.get("strong_or_contextual_evidence_count") or 0)
            - int(before.get("strong_or_contextual_evidence_count") or 0),
            "elapsed_ms": round(float(after.get("elapsed_ms") or 0.0) - float(before.get("elapsed_ms") or 0.0), 3),
        },
        "before": before,
        "after": after,
    }
