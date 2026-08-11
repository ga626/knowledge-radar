"""L0 content classification and routing metadata."""

from __future__ import annotations

from typing import Any, Dict, List

from .models import (
    ContentEnvelope,
    ContentKind,
    L1Signal,
    L1Snapshot,
    ModalitySignals,
    RouteDecision,
    SignalStatus,
)
from .router import decide_route


def _text_len(value: Any) -> int:
    if not isinstance(value, str):
        return 0
    stripped = value.strip()
    if stripped.startswith("[transcribe]"):
        return 0
    return len(stripped)


def _count_list(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _status_for_text(value: Any, *, pending_prefix: str = "") -> SignalStatus:
    if not isinstance(value, str):
        return SignalStatus.MISSING
    stripped = value.strip()
    if not stripped:
        return SignalStatus.EMPTY
    if pending_prefix and stripped.startswith(pending_prefix):
        return SignalStatus.PENDING
    return SignalStatus.AVAILABLE


def _status_for_list(value: Any) -> SignalStatus:
    if not isinstance(value, list):
        return SignalStatus.MISSING
    return SignalStatus.AVAILABLE if value else SignalStatus.EMPTY


def _build_l1_snapshot(platform: str, data: Dict[str, Any]) -> L1Snapshot:
    content_len = _text_len(data.get("content"))
    desc_len = _text_len(data.get("desc"))
    transcript_status = _status_for_text(data.get("transcript"), pending_prefix="[transcribe]")
    comments_status = _status_for_list(data.get("comments"))
    filtered_comments_status = _status_for_list(data.get("filtered_comments"))
    images_status = _status_for_list(data.get("images"))

    signals = {
        "platform_text": L1Signal(
            name="platform_text",
            status=SignalStatus.AVAILABLE if content_len + desc_len > 0 else SignalStatus.EMPTY,
            source=platform,
            length=content_len + desc_len,
            notes="Merged detail content and description already returned by platform extractor.",
        ),
        "transcript": L1Signal(
            name="transcript",
            status=transcript_status,
            source="faster_whisper" if platform == "B站" else "",
            length=_text_len(data.get("transcript")),
            notes="Bilibili transcript may be pending because download/transcription runs in background.",
        ),
        "images": L1Signal(
            name="images",
            status=images_status,
            source=platform,
            count=_count_list(data.get("images")),
            notes="Image URLs only; OCR and image understanding are not triggered in phase 2.",
        ),
        "comments": L1Signal(
            name="comments",
            status=comments_status,
            source=platform,
            count=_count_list(data.get("comments")),
        ),
        "filtered_comments": L1Signal(
            name="filtered_comments",
            status=filtered_comments_status,
            source="comment_filter_model" if _count_list(data.get("filtered_comments")) else "",
            count=_count_list(data.get("filtered_comments")),
            notes="Only available when enable_comment_filtering was requested or cache was hit.",
        ),
    }
    return L1Snapshot(signals=signals)


def _infer_kind(platform: str, url: str, data: Dict[str, Any], signals: ModalitySignals) -> ContentKind:
    url_l = url.lower()
    if platform in {"B站", "YouTube"} or "bilibili.com/video/" in url_l or "youtube.com/watch" in url_l or "youtu.be/" in url_l:
        return ContentKind.VIDEO
    if platform == "知乎" and data.get("video_url"):
        return ContentKind.VIDEO
    if platform == "知乎":
        return ContentKind.TEXT
    if platform == "小红书":
        if signals.has_images and signals.has_text:
            return ContentKind.IMAGE_TEXT
        if signals.has_images:
            return ContentKind.IMAGE_TEXT
        if "video" in str(data.get("noteType") or data.get("type") or "").lower():
            return ContentKind.VIDEO
        return ContentKind.TEXT if signals.has_text else ContentKind.UNKNOWN
    if signals.has_video:
        return ContentKind.VIDEO
    if signals.has_images:
        return ContentKind.IMAGE_TEXT
    if signals.has_text:
        return ContentKind.TEXT
    return ContentKind.UNKNOWN


def classify_content(url: str, data: Dict[str, Any]) -> ContentEnvelope:
    platform = str(data.get("platform") or "unknown")
    content_len = _text_len(data.get("content"))
    desc_len = _text_len(data.get("desc"))
    transcript_len = _text_len(data.get("transcript"))
    image_count = _count_list(data.get("images"))
    comment_count = _count_list(data.get("comments"))
    has_video = (
        bool(data.get("video_url"))
        or platform in {"B站", "YouTube"}
        or "bilibili.com/video/" in url.lower()
        or "youtube.com/watch" in url.lower()
        or "youtu.be/" in url.lower()
    )

    signals = ModalitySignals(
        has_text=(content_len + desc_len) > 0,
        has_images=image_count > 0,
        has_video=has_video,
        has_audio=platform in {"B站", "YouTube"},
        has_transcript=transcript_len > 0,
        has_comments=comment_count > 0,
        text_length=content_len + desc_len,
        image_count=image_count,
        comment_count=comment_count,
        transcript_length=transcript_len,
        metadata={
            "has_deep_analysis": bool(data.get("deep_analysis")),
            "has_filtered_comments": _count_list(data.get("filtered_comments")) > 0,
        },
    )
    kind = _infer_kind(platform, url, data, signals)
    l1 = _build_l1_snapshot(platform, data)
    return ContentEnvelope(
        platform=platform,
        url=url,
        kind=kind,
        title=str(data.get("title") or ""),
        signals=signals,
        l1=l1,
    )


def _initial_reasons(envelope: ContentEnvelope) -> List[str]:
    s = envelope.signals
    if envelope.kind == ContentKind.VIDEO:
        reasons = ["video content detected"]
        if s.has_transcript:
            reasons.append("transcript is available for L1 understanding")
        else:
            reasons.append("transcript is missing or still processing")
        return reasons
    if envelope.kind == ContentKind.IMAGE_TEXT:
        reasons = ["image-text content detected"]
        if s.has_text:
            reasons.append("platform text is available")
        if s.has_images:
            reasons.append(f"{s.image_count} image(s) available for later OCR/probe")
        return reasons
    if envelope.kind == ContentKind.TEXT:
        return ["text content detected", "L1 text extraction is the default path"]
    return ["content type is unknown"]


def build_routing_metadata(url: str, data: Dict[str, Any]) -> Dict[str, Any]:
    envelope = classify_content(url, data)
    decision = decide_route(envelope, data)
    if decision.stage == "l1_signal_ready" and not decision.reasons:
        decision = RouteDecision(
            stage="l1_signal_ready",
            content_kind=envelope.kind,
            recommended_path="l1_only",
            should_run_l2=False,
            reasons=_initial_reasons(envelope),
            signals=envelope.signals,
            confidence=0.85,
            reason_codes=["CONTENT_L1_SIGNAL_READY", "PATH_L1_ONLY"],
        )
    return {
        "schema_version": "multimodal-routing/v1",
        "envelope": envelope.to_dict(),
        "decision": decision.to_dict(),
    }
