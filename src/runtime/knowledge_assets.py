"""Knowledge asset interface summaries.

The asset layer is deliberately thin: KnowledgeRadar produces source, claim and
evidence-pack contracts that a future personal knowledge base can ingest.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Dict, Iterable, List


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_id(prefix: str, value: Any, *, length: int = 16) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8', errors='ignore')).hexdigest()[:length]}"


@dataclass(frozen=True)
class SourceRecord:
    schema: str = "knowledgeradar-source-record/v1"
    source_id: str = ""
    source_url: str = ""
    source_platform: str = ""
    retrieved_at: str = field(default_factory=utc_now_iso)
    published_at: str = ""
    access_type: str = "public_or_authorized_summary"
    collector: str = ""
    license_or_terms_note: str = ""
    strength: str = "medium"
    freshness: str = "unknown"

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if not data["source_id"]:
            data["source_id"] = stable_id("SRC", [data["source_url"], data["source_platform"]], length=12)
        return data


@dataclass(frozen=True)
class ClaimRecord:
    schema: str = "knowledgeradar-claim-record/v1"
    claim_id: str = ""
    claim: str = ""
    supporting_evidence_ids: List[str] = field(default_factory=list)
    counter_evidence_ids: List[str] = field(default_factory=list)
    confidence: str = "medium"
    freshness: str = "unknown"
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if not data["claim_id"]:
            data["claim_id"] = stable_id("CLM", [data["claim"], data["supporting_evidence_ids"]], length=12)
        return data


@dataclass(frozen=True)
class EvidencePack:
    schema: str = "knowledgeradar-evidence-pack/v1"
    pack_id: str = ""
    topic: str = ""
    scope: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    query_plan: Dict[str, Any] = field(default_factory=dict)
    source_records: List[Dict[str, Any]] = field(default_factory=list)
    claim_records: List[Dict[str, Any]] = field(default_factory=list)
    evidence_ids: List[str] = field(default_factory=list)
    refresh_policy: Dict[str, Any] = field(default_factory=dict)
    privacy_level: str = "redacted_summary"

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if not data["pack_id"]:
            data["pack_id"] = stable_id("EP", [data["topic"], data["scope"], data["evidence_ids"]], length=14)
        return data


def default_refresh_policy() -> Dict[str, Any]:
    return {
        "schema": "knowledgeradar-refresh-policy/v1",
        "default_ttl_days": 30,
        "refresh_when": ["source_unavailable", "claim_reused_in_new_report", "time_sensitive_topic"],
        "preserve_evidence_ids": True,
        "compact_output_only": True,
    }


def knowledge_asset_schema_summary() -> Dict[str, Any]:
    return {
        "schema": "knowledgeradar-knowledge-asset-interface/v1",
        "status": "ok",
        "execution_mode": "contract_only",
        "asset_schemas": {
            "source_record": "knowledgeradar-source-record/v1",
            "claim_record": "knowledgeradar-claim-record/v1",
            "evidence_pack": "knowledgeradar-evidence-pack/v1",
            "refresh_policy": "knowledgeradar-refresh-policy/v1",
        },
        "ownership_boundary": {
            "knowledgeradar_owns": ["source_records", "claim_records", "evidence_packs", "refresh_policy"],
            "future_personal_knowledge_base_owns": ["global_memory_graph", "file_index", "cross-session semantic memory"],
        },
        "privacy": {
            "default": "redacted_summary",
            "no_raw_log_dump": True,
            "no_cookie_or_token": True,
        },
    }


def build_evidence_pack_summary(
    *,
    topic: str,
    scope: str,
    evidence_rows: Iterable[Dict[str, Any]] = (),
    claim: str = "",
) -> Dict[str, Any]:
    rows = [row for row in evidence_rows if isinstance(row, dict)]
    sources = []
    evidence_ids = []
    for row in rows[:20]:
        evidence_id = str(row.get("id") or row.get("evidence_id") or "")
        if evidence_id:
            evidence_ids.append(evidence_id)
        sources.append(
            SourceRecord(
                source_url=str(row.get("url") or row.get("source_url") or ""),
                source_platform=str(row.get("platform") or row.get("source_platform") or ""),
                retrieved_at=str(row.get("timestamp") or row.get("retrieved_at") or utc_now_iso()),
                collector=str((row.get("payload") or {}).get("collector") or row.get("record_type") or ""),
                strength="medium" if evidence_id else "weak",
            ).to_dict()
        )
    claims = []
    if claim:
        claims.append(ClaimRecord(claim=claim, supporting_evidence_ids=evidence_ids[:12]).to_dict())
    return EvidencePack(
        topic=topic,
        scope=scope,
        query_plan={"source": "existing_evidence_rows", "row_count": len(rows)},
        source_records=sources,
        claim_records=claims,
        evidence_ids=evidence_ids,
        refresh_policy=default_refresh_policy(),
    ).to_dict()
