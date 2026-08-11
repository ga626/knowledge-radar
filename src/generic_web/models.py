"""Models for generic web collection."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class GenericWebRequest:
    url: str
    preferred_format: str = "markdown"
    timeout: float = 20.0
    use_jina: bool = True


@dataclass(frozen=True)
class GenericWebResponse:
    url: str
    final_url: str = ""
    title: str = ""
    content: str = ""
    content_format: str = "markdown"
    collector: str = ""
    fetched_at: str = ""
    elapsed_s: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    error: Optional[Dict[str, Any]] = None

    def to_mcp_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "url": self.url,
            "final_url": self.final_url or self.url,
            "title": self.title,
            "content": self.content,
            "content_format": self.content_format,
            "collector": self.collector,
            "fetched_at": self.fetched_at,
            "elapsed_s": self.elapsed_s,
            "metadata": dict(self.metadata),
        }
        if self.error:
            data["error"] = dict(self.error)
        return data
