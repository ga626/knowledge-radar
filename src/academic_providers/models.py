"""Shared models for academic metadata search."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Any, Dict, List, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_doi(value: str) -> str:
    doi = str(value or "").strip()
    if not doi:
        return ""
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.IGNORECASE)
    return doi.strip().lower()


def normalize_title(value: str) -> str:
    title = str(value or "").strip().lower()
    if not title:
        return ""
    title = re.sub(r"\s+", " ", title)
    title = re.sub(r"[\W_]+", "", title, flags=re.UNICODE)
    return title


@dataclass(frozen=True)
class AcademicSearchRequest:
    query: str
    limit: int = 5
    provider: str = "openalex"
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AcademicWork:
    title: str
    url: str
    authors: List[str] = field(default_factory=list)
    year: Optional[int] = None
    doi: str = ""
    abstract: str = ""
    source: str = ""
    oa_status: str = ""
    license: str = ""
    source_database: str = ""
    access_mode: str = "public_api"
    full_text_status: str = "metadata_only"
    provider_confidence: float = 0.0
    title_similarity: float = 0.0
    verification_status: str = "unverified"
    citation_export_formats: List[str] = field(default_factory=list)
    license_scope: str = "unknown"
    degraded_reason: str = ""
    retrieved_at: str = field(default_factory=utc_now_iso)
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        doi = normalize_doi(self.doi)
        return {
            "title": self.title,
            "url": self.url,
            "authors": list(self.authors),
            "year": self.year,
            "doi": doi,
            "abstract": self.abstract,
            "source": self.source,
            "oa_status": self.oa_status,
            "license": self.license,
            "source_database": self.source_database or self.source or "",
            "access_mode": self.access_mode,
            "full_text_status": self.full_text_status,
            "provider_confidence": self.provider_confidence,
            "title_similarity": self.title_similarity,
            "verification_status": self.verification_status,
            "citation_export_formats": list(self.citation_export_formats),
            "license_scope": self.license_scope,
            "degraded_reason": self.degraded_reason,
            "retrieved_at": self.retrieved_at,
            "raw": dict(self.raw),
        }


@dataclass(frozen=True)
class AcademicSearchResponse:
    query: str
    provider: str
    items: List[AcademicWork] = field(default_factory=list)
    error: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_mcp_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "query": self.query,
            "provider": self.provider,
            "items": [item.to_dict() for item in self.items],
            "total": len(self.items),
        }
        if self.error:
            data["error"] = dict(self.error)
        if self.metadata:
            data["metadata"] = dict(self.metadata)
        return data
