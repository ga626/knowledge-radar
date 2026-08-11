"""Models for generic web search providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class WebSearchRequest:
    query: str
    limit: int = 5
    freshness: str = ""
    language: str = ""
    provider: str = "auto"
    include_raw_content: bool = False
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SearchProviderResult:
    title: str
    url: str
    snippet: str = ""
    source_provider: str = ""
    published_at: str = ""
    retrieved_at: str = ""
    score: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source_provider": self.source_provider,
            "published_at": self.published_at,
            "retrieved_at": self.retrieved_at,
            "raw": dict(self.raw),
        }
        if self.score is not None:
            data["score"] = self.score
        return data


@dataclass(frozen=True)
class WebSearchResponse:
    query: str
    provider: str
    items: List[SearchProviderResult] = field(default_factory=list)
    fallback_used: bool = False
    attempted_providers: List[str] = field(default_factory=list)
    error: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_mcp_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "query": self.query,
            "provider": self.provider,
            "items": [item.to_dict() for item in self.items],
            "total": len(self.items),
            "fallback_used": self.fallback_used,
            "attempted_providers": list(self.attempted_providers),
        }
        if self.error:
            data["error"] = dict(self.error)
        if self.metadata:
            data["metadata"] = dict(self.metadata)
        return data
