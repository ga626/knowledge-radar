"""Shared models for routing decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List


class ContentKind(str, Enum):
    TEXT = "text"
    IMAGE_TEXT = "image_text"
    VIDEO = "video"
    AUDIO = "audio"
    UNKNOWN = "unknown"


class SignalStatus(str, Enum):
    AVAILABLE = "available"
    PENDING = "pending"
    EMPTY = "empty"
    MISSING = "missing"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class L1Signal:
    name: str
    status: SignalStatus
    source: str = ""
    count: int = 0
    length: int = 0
    notes: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "source": self.source,
            "count": self.count,
            "length": self.length,
            "notes": self.notes,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class L1Snapshot:
    signals: Dict[str, L1Signal] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {name: signal.to_dict() for name, signal in self.signals.items()}


@dataclass(frozen=True)
class ModalitySignals:
    has_text: bool = False
    has_images: bool = False
    has_video: bool = False
    has_audio: bool = False
    has_transcript: bool = False
    has_comments: bool = False
    text_length: int = 0
    image_count: int = 0
    comment_count: int = 0
    transcript_length: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "has_text": self.has_text,
            "has_images": self.has_images,
            "has_video": self.has_video,
            "has_audio": self.has_audio,
            "has_transcript": self.has_transcript,
            "has_comments": self.has_comments,
            "text_length": self.text_length,
            "image_count": self.image_count,
            "comment_count": self.comment_count,
            "transcript_length": self.transcript_length,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ContentEnvelope:
    platform: str
    url: str
    kind: ContentKind
    title: str = ""
    signals: ModalitySignals = field(default_factory=ModalitySignals)
    l1: L1Snapshot = field(default_factory=L1Snapshot)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform,
            "url": self.url,
            "kind": self.kind.value,
            "title": self.title,
            "signals": self.signals.to_dict(),
            "l1": self.l1.to_dict(),
        }


@dataclass(frozen=True)
class RouteDecision:
    stage: str
    content_kind: ContentKind
    recommended_path: str
    should_run_l2: bool
    reasons: List[str] = field(default_factory=list)
    signals: ModalitySignals = field(default_factory=ModalitySignals)
    scores: Dict[str, Any] = field(default_factory=dict)
    probes: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    reason_codes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "content_kind": self.content_kind.value,
            "recommended_path": self.recommended_path,
            "should_run_l2": self.should_run_l2,
            "reasons": list(self.reasons),
            "signals": self.signals.to_dict(),
            "scores": dict(self.scores),
            "probes": dict(self.probes),
            "confidence": self.confidence,
            "reason_codes": list(self.reason_codes),
        }
