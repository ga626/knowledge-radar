"""Media input models for unified multimodal routing.

These models are intentionally inert: constructing them must not read files,
fetch URLs, or download media. They only describe evidence that an upstream
detail strategy has already discovered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable


class MediaKind(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"


class MediaLocationType(str, Enum):
    URL = "url"
    LOCAL_PATH = "local_path"
    BASE64 = "base64"
    INLINE_TEXT = "inline_text"
    PLATFORM_ID = "platform_id"


class TextKind(str, Enum):
    TITLE = "title"
    DESCRIPTION = "description"
    COMMENT = "comment"
    SUBTITLE = "subtitle"
    TRANSCRIPT = "transcript"
    OCR = "ocr"
    SUMMARY = "summary"
    OTHER = "other"


@dataclass(frozen=True)
class TextItem:
    text: str
    kind: TextKind = TextKind.OTHER
    source: str = ""
    language: str = ""
    confidence: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "kind": self.kind.value,
            "source": self.source,
            "language": self.language,
            "confidence": self.confidence,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class MediaRef:
    kind: MediaKind
    uri: str
    location_type: MediaLocationType = MediaLocationType.URL
    role: str = ""
    mime_type: str = ""
    duration_seconds: float | None = None
    size_bytes: int | None = None
    width: int | None = None
    height: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_remote(self) -> bool:
        return self.location_type == MediaLocationType.URL

    @property
    def is_local(self) -> bool:
        return self.location_type == MediaLocationType.LOCAL_PATH

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "uri": self.uri,
            "location_type": self.location_type.value,
            "role": self.role,
            "mime_type": self.mime_type,
            "duration_seconds": self.duration_seconds,
            "size_bytes": self.size_bytes,
            "width": self.width,
            "height": self.height,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class MediaBundle:
    platform: str
    source_url: str
    content_id: str = ""
    title: str = ""
    texts: tuple[TextItem, ...] = ()
    image_refs: tuple[MediaRef, ...] = ()
    audio_refs: tuple[MediaRef, ...] = ()
    video_refs: tuple[MediaRef, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        *,
        platform: str,
        source_url: str,
        content_id: str = "",
        title: str = "",
        texts: Iterable[TextItem] = (),
        image_refs: Iterable[MediaRef] = (),
        audio_refs: Iterable[MediaRef] = (),
        video_refs: Iterable[MediaRef] = (),
        metadata: dict[str, Any] | None = None,
    ) -> "MediaBundle":
        return cls(
            platform=platform,
            source_url=source_url,
            content_id=content_id,
            title=title,
            texts=tuple(texts),
            image_refs=tuple(image_refs),
            audio_refs=tuple(audio_refs),
            video_refs=tuple(video_refs),
            metadata=dict(metadata or {}),
        )

    @property
    def has_text(self) -> bool:
        return bool(self.texts or self.title)

    @property
    def has_images(self) -> bool:
        return bool(self.image_refs)

    @property
    def has_audio(self) -> bool:
        return bool(self.audio_refs)

    @property
    def has_video(self) -> bool:
        return bool(self.video_refs)

    def refs_for(self, kind: MediaKind) -> tuple[MediaRef, ...]:
        if kind == MediaKind.IMAGE:
            return self.image_refs
        if kind == MediaKind.AUDIO:
            return self.audio_refs
        if kind == MediaKind.VIDEO:
            return self.video_refs
        return ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "source_url": self.source_url,
            "content_id": self.content_id,
            "title": self.title,
            "texts": [item.to_dict() for item in self.texts],
            "image_refs": [ref.to_dict() for ref in self.image_refs],
            "audio_refs": [ref.to_dict() for ref in self.audio_refs],
            "video_refs": [ref.to_dict() for ref in self.video_refs],
            "metadata": dict(self.metadata),
        }
