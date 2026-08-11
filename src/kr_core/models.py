"""Shared request and response models for platform adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .errors import KnowledgeRadarError
from .affordance import attach_result_affordance


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class SearchRequest:
    keyword: str
    limit: int = 10
    page: int = 1
    platform: str = ""
    search_type: str = ""
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SearchResultItem:
    title: str
    url: str
    platform: str
    author: str = ""
    summary: str = ""
    content_type: str = ""
    source: str = ""
    duration: Any = ""
    published_at: str = ""
    stats: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_legacy(cls, platform: str, item: Dict[str, Any]) -> "SearchResultItem":
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else dict(item)
        summary = str(
            item.get("summary")
            or metadata.get("desc")
            or metadata.get("description")
            or metadata.get("content")
            or ""
        )
        return cls(
            title=str(item.get("title") or ""),
            url=str(item.get("url") or ""),
            platform=platform,
            author=str(item.get("author") or ""),
            summary=summary[:160],
            content_type=str(item.get("content_type") or metadata.get("type") or ""),
            source=str(item.get("source") or metadata.get("source") or ""),
            duration=item.get("duration") or metadata.get("duration") or "",
            published_at=str(
                item.get("published_at")
                or item.get("pubdate")
                or metadata.get("published_at")
                or metadata.get("pubdate")
                or ""
            ),
            stats={
                key: value
                for key, value in {
                    "play": item.get("play") if "play" in item else metadata.get("play"),
                    "like": item.get("like") if "like" in item else metadata.get("like"),
                    "reply": item.get("reply") if "reply" in item else metadata.get("reply"),
                    "favorite": item.get("favorite") if "favorite" in item else metadata.get("favorite"),
                }.items()
                if value not in (None, "")
            },
            metadata=metadata,
        )

    def to_mcp_dict(self) -> Dict[str, Any]:
        data = {
            "title": self.title,
            "url": self.url,
            "author": self.author,
            "summary": self.summary,
            "metadata": dict(self.metadata),
        }
        if self.content_type:
            data["content_type"] = self.content_type
        if self.source:
            data["source"] = self.source
        if self.duration not in (None, ""):
            data["duration"] = self.duration
        if self.published_at:
            data["published_at"] = self.published_at
        if self.stats:
            data["stats"] = dict(self.stats)
        return attach_result_affordance(self.platform, data)


@dataclass(frozen=True)
class SearchResponse:
    platform: str
    items: List[SearchResultItem] = field(default_factory=list)
    error: Optional[KnowledgeRadarError] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_legacy_items(cls, platform: str, items: List[Dict[str, Any]]) -> "SearchResponse":
        return cls(
            platform=platform,
            items=[SearchResultItem.from_legacy(platform, item) for item in items],
        )

    @classmethod
    def from_error(cls, platform: str, error: KnowledgeRadarError) -> "SearchResponse":
        return cls(platform=platform, items=[], error=error)

    def to_mcp_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "items": [item.to_mcp_dict() for item in self.items],
            "total": len(self.items),
            "platform": self.platform,
        }
        if self.error:
            data["error"] = self.error.to_mcp_dict()
        if self.metadata:
            data["metadata"] = dict(self.metadata)
        return data


@dataclass(frozen=True)
class EvidenceItem:
    source_url: str
    source_platform: str
    retrieved_at: str = field(default_factory=utc_now_iso)
    published_at: str = ""
    summary: str = ""
    credibility: str = "medium"
    freshness: str = "unknown"
    verification_status: str = "已验证"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_mcp_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "source_url": self.source_url,
            "source_platform": self.source_platform,
            "retrieved_at": self.retrieved_at,
            "published_at": self.published_at or "unknown",
            "summary": self.summary,
            "credibility": self.credibility,
            "freshness": self.freshness,
            "verification_status": self.verification_status,
        }
        if self.metadata:
            data["metadata"] = dict(self.metadata)
        return data


@dataclass(frozen=True)
class DetailRequest:
    url: str
    enable_deep_analysis: bool = False
    enable_comment_filtering: bool = False
    auto_multimodal: bool = False
    platform: str = ""
    research_session_id: str = ""
    work_scope_id: str = ""
    task_scope_id: str = ""
    scope_kind: str = "detail_request"
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DetailResponse:
    platform: str
    url: str
    data: Dict[str, Any] = field(default_factory=dict)
    evidence: Optional[EvidenceItem] = None
    error: Optional[KnowledgeRadarError] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_legacy(
        cls,
        platform: str,
        url: str,
        data: Dict[str, Any],
        *,
        evidence: Optional[EvidenceItem] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "DetailResponse":
        return cls(
            platform=platform,
            url=url,
            data=dict(data),
            evidence=evidence,
            metadata=metadata or {},
        )

    def to_legacy_dict(self) -> Dict[str, Any]:
        data = dict(self.data)
        if self.error:
            data["error"] = self.error.message
            data["error_detail"] = self.error.to_mcp_dict()
            data.setdefault("platform", self.platform)
            data.setdefault("url", self.url)
        if self.evidence:
            data["evidence"] = self.evidence.to_mcp_dict()
        if self.metadata:
            data.setdefault("detail_metadata", {}).update(dict(self.metadata))
        return data


@dataclass(frozen=True)
class PlatformCapability:
    platform: str
    search: bool = True
    detail: bool = False
    comments: bool = False
    media_extract: bool = False
    login_required: bool = False
    strategies: List[str] = field(default_factory=list)
    notes: str = ""
