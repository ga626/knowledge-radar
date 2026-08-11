"""Data models for media action planning.

The four source modes are media-agnostic:

- basic_text: use already available title, description, comments, metadata.
- derived_text: obtain text from media, such as transcript, ASR, OCR, captions.
- sampled_media_with_text: inspect sampled frames/images plus derived/basic text.
- native_media: pass original media references to a model, such as video_url.

This file only defines the contracts used by later routing and async work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from media_bundle import MediaBundle, MediaKind, MediaLocationType, TextKind
from media_policy import MediaModelPolicy
from routing.models import RouteDecision


class InformationSourceMode(str, Enum):
    BASIC_TEXT = "basic_text"
    DERIVED_TEXT = "derived_text"
    SAMPLED_MEDIA_WITH_TEXT = "sampled_media_with_text"
    NATIVE_MEDIA = "native_media"


class MediaOperation(str, Enum):
    READ_BASIC_TEXT = "read_basic_text"
    READ_TRANSCRIPT = "read_transcript"
    RUN_ASR = "run_asr"
    RUN_OCR = "run_ocr"
    SAMPLE_FRAMES = "sample_frames"
    PASS_IMAGE_URL = "pass_image_url"
    PASS_AUDIO_URL = "pass_audio_url"
    PASS_VIDEO_URL = "pass_video_url"


class ExecutionMode(str, Enum):
    SYNC = "sync"
    ASYNC = "async"
    DEFERRED = "deferred"


class StoragePolicy(str, Enum):
    NO_DOWNLOAD = "no_download"
    TEMPORARY_ONLY = "temporary_only"
    MEDIA_CACHE_TTL = "media_cache_ttl"


class WaitMode(str, Enum):
    NONE = "none"
    SHORT = "short"
    CLIENT_BUDGET = "client_budget"


@dataclass(frozen=True)
class MediaBudget:
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_cost_cny: float | None = None
    max_duration_seconds: float | None = None
    max_frames: int | None = None
    fps: float | None = None
    max_pixels: int | None = None
    timeout_seconds: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_cost_cny": self.max_cost_cny,
            "max_duration_seconds": self.max_duration_seconds,
            "max_frames": self.max_frames,
            "fps": self.fps,
            "max_pixels": self.max_pixels,
            "timeout_seconds": self.timeout_seconds,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class MediaActionPlan:
    source_mode: InformationSourceMode
    target_kinds: tuple[MediaKind, ...] = ()
    operations: tuple[MediaOperation, ...] = ()
    provider: str = ""
    model: str = ""
    execution_mode: ExecutionMode = ExecutionMode.SYNC
    wait_mode: WaitMode = WaitMode.NONE
    storage_policy: StoragePolicy = StoragePolicy.NO_DOWNLOAD
    budget: MediaBudget = field(default_factory=MediaBudget)
    reasons: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_bundle(
        cls,
        bundle: MediaBundle,
        *,
        source_mode: InformationSourceMode,
        target_kinds: tuple[MediaKind, ...],
        operations: tuple[MediaOperation, ...],
        provider: str = "",
        model: str = "",
        execution_mode: ExecutionMode = ExecutionMode.SYNC,
        wait_mode: WaitMode = WaitMode.NONE,
        storage_policy: StoragePolicy = StoragePolicy.NO_DOWNLOAD,
        budget: MediaBudget | None = None,
        reasons: tuple[str, ...] = (),
        metadata: dict[str, Any] | None = None,
    ) -> "MediaActionPlan":
        plan_metadata = {"platform": bundle.platform, "content_id": bundle.content_id}
        plan_metadata.update(metadata or {})
        return cls(
            source_mode=source_mode,
            target_kinds=target_kinds,
            operations=operations,
            provider=provider,
            model=model,
            execution_mode=execution_mode,
            wait_mode=wait_mode,
            storage_policy=storage_policy,
            budget=budget or MediaBudget(),
            reasons=reasons,
            metadata=plan_metadata,
        )

    @property
    def requires_network(self) -> bool:
        local_providers = {"", "local", "local-whisper", "whisper"}
        processing_requires_network = self.provider.lower() not in local_providers
        return any(
            operation
            in {
                MediaOperation.PASS_IMAGE_URL,
                MediaOperation.PASS_AUDIO_URL,
                MediaOperation.PASS_VIDEO_URL,
            }
            for operation in self.operations
        ) or (
            processing_requires_network
            and any(operation in {MediaOperation.RUN_ASR, MediaOperation.RUN_OCR} for operation in self.operations)
        )

    @property
    def allows_download(self) -> bool:
        return self.storage_policy != StoragePolicy.NO_DOWNLOAD

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_mode": self.source_mode.value,
            "target_kinds": [kind.value for kind in self.target_kinds],
            "operations": [operation.value for operation in self.operations],
            "provider": self.provider,
            "model": self.model,
            "execution_mode": self.execution_mode.value,
            "wait_mode": self.wait_mode.value,
            "storage_policy": self.storage_policy.value,
            "budget": self.budget.to_dict(),
            "reasons": list(self.reasons),
            "metadata": dict(self.metadata),
            "requires_network": self.requires_network,
            "allows_download": self.allows_download,
        }


def plan_media_action(
    bundle: MediaBundle,
    route_decision: RouteDecision | None = None,
    *,
    prefer_native_media: bool = False,
    allow_download: bool = False,
    default_provider: str = "bailian",
    policy: MediaModelPolicy | None = None,
    direct_media_probe: dict[str, Any] | None = None,
) -> MediaActionPlan:
    """Map content routing output to a media execution plan.

    This is the P0.2 bridge between the existing content-value router and the
    new source-mode model. It makes no network calls and does not touch local
    media files.
    """
    if bundle.has_video:
        return _plan_video(
            bundle,
            route_decision,
            prefer_native_media=prefer_native_media,
            allow_download=allow_download,
            default_provider=default_provider,
            policy=policy or MediaModelPolicy.from_env(),
            direct_media_probe=direct_media_probe,
        )
    active_policy = policy or MediaModelPolicy.from_env()
    if bundle.has_images:
        return _plan_image(bundle, route_decision, default_provider=default_provider, policy=active_policy)
    if bundle.has_audio:
        return _plan_audio(bundle, route_decision, allow_download=allow_download, policy=active_policy)
    return _basic_text_plan(bundle, route_decision, reason="no media references are present")


def _route_path(route_decision: RouteDecision | None) -> str:
    return route_decision.recommended_path if route_decision else ""


def _route_metadata(route_decision: RouteDecision | None) -> dict[str, Any]:
    if not route_decision:
        return {}
    return {
        "route_stage": route_decision.stage,
        "recommended_path": route_decision.recommended_path,
        "route_confidence": route_decision.confidence,
        "reason_codes": list(route_decision.reason_codes),
        "scores": dict(route_decision.scores),
        "probes": dict(route_decision.probes),
    }


def _text_kinds(bundle: MediaBundle) -> set[TextKind]:
    return {item.kind for item in bundle.texts}


def _has_derived_text(bundle: MediaBundle) -> bool:
    kinds = _text_kinds(bundle)
    return bool(kinds & {TextKind.SUBTITLE, TextKind.TRANSCRIPT, TextKind.OCR, TextKind.SUMMARY})


def _has_remote_ref(bundle: MediaBundle, kind: MediaKind) -> bool:
    return any(ref.location_type == MediaLocationType.URL for ref in bundle.refs_for(kind))


def _basic_text_plan(bundle: MediaBundle, route_decision: RouteDecision | None, *, reason: str) -> MediaActionPlan:
    return MediaActionPlan.from_bundle(
        bundle,
        source_mode=InformationSourceMode.BASIC_TEXT,
        target_kinds=(MediaKind.TEXT,),
        operations=(MediaOperation.READ_BASIC_TEXT,),
        provider="",
        execution_mode=ExecutionMode.SYNC,
        wait_mode=WaitMode.NONE,
        storage_policy=StoragePolicy.NO_DOWNLOAD,
        budget=MediaBudget(max_input_tokens=4000, timeout_seconds=5),
        reasons=(reason,),
        metadata=_route_metadata(route_decision),
    )


def _plan_video(
    bundle: MediaBundle,
    route_decision: RouteDecision | None,
    *,
    prefer_native_media: bool,
    allow_download: bool,
    default_provider: str,
    policy: MediaModelPolicy,
    direct_media_probe: dict[str, Any] | None,
) -> MediaActionPlan:
    path = _route_path(route_decision)
    metadata = _route_metadata(route_decision)
    has_transcript = TextKind.TRANSCRIPT in _text_kinds(bundle) or TextKind.SUBTITLE in _text_kinds(bundle)
    has_remote_video = _has_remote_ref(bundle, MediaKind.VIDEO)

    video_duration = _max_duration(bundle.video_refs)
    native_within_default_budget = (
        video_duration is None or video_duration <= policy.native_auto_max_duration_seconds
    )
    provider_downloadable = _provider_downloadable(direct_media_probe)
    if prefer_native_media and has_remote_video and native_within_default_budget and provider_downloadable:
        return MediaActionPlan.from_bundle(
            bundle,
            source_mode=InformationSourceMode.NATIVE_MEDIA,
            target_kinds=(MediaKind.VIDEO,),
            operations=(MediaOperation.PASS_VIDEO_URL,),
            provider=default_provider,
            model=policy.default_model_for("native_video"),
            execution_mode=ExecutionMode.ASYNC,
            wait_mode=WaitMode.CLIENT_BUDGET,
            storage_policy=StoragePolicy.NO_DOWNLOAD,
            budget=MediaBudget(
                max_duration_seconds=policy.native_auto_max_duration_seconds,
                max_frames=policy.native_auto_max_frames,
                fps=policy.native_auto_fps,
                max_input_tokens=policy.native_auto_max_input_tokens,
                timeout_seconds=policy.native_auto_timeout_seconds,
                metadata={"policy": "native_auto_default"},
            ),
            reasons=("native video_url is preferred and a remote video reference is present",),
            metadata={**metadata, "direct_media_probe": _direct_media_plan_metadata(direct_media_probe)},
        )

    if path in {"recommend_l2_video", "need_more_probe"}:
        operations: tuple[MediaOperation, ...]
        if has_transcript:
            operations = (MediaOperation.READ_TRANSCRIPT, MediaOperation.SAMPLE_FRAMES)
        else:
            operations = (MediaOperation.RUN_ASR, MediaOperation.SAMPLE_FRAMES)
        storage_policy = StoragePolicy.MEDIA_CACHE_TTL if allow_download else StoragePolicy.NO_DOWNLOAD
        return MediaActionPlan.from_bundle(
            bundle,
            source_mode=InformationSourceMode.SAMPLED_MEDIA_WITH_TEXT,
            target_kinds=(MediaKind.VIDEO, MediaKind.AUDIO),
            operations=operations,
            provider=default_provider,
            model=policy.default_model_for("frame_vision"),
            execution_mode=ExecutionMode.ASYNC,
            wait_mode=WaitMode.CLIENT_BUDGET,
            storage_policy=storage_policy,
            budget=MediaBudget(max_frames=8, fps=1, max_input_tokens=12000, timeout_seconds=90),
            reasons=(f"content route requested visual probe path={path}",),
            metadata={
                **metadata,
                "direct_media_probe": _direct_media_plan_metadata(direct_media_probe),
                "native_media_fallback_reason": _native_fallback_reason(
                    prefer_native_media=prefer_native_media,
                    has_remote_video=has_remote_video,
                    native_within_default_budget=native_within_default_budget,
                    direct_media_probe=direct_media_probe,
                ),
            },
        )

    if path == "l1_transcript_enough" or has_transcript:
        operation = MediaOperation.READ_TRANSCRIPT if has_transcript else MediaOperation.RUN_ASR
        storage_policy = StoragePolicy.NO_DOWNLOAD if has_transcript else StoragePolicy.TEMPORARY_ONLY
        return MediaActionPlan.from_bundle(
            bundle,
            source_mode=InformationSourceMode.DERIVED_TEXT,
            target_kinds=(MediaKind.AUDIO,),
            operations=(operation,),
            provider="" if has_transcript else "whisper",
            model="" if has_transcript else policy.default_model_for("asr"),
            execution_mode=ExecutionMode.SYNC if has_transcript else ExecutionMode.ASYNC,
            wait_mode=WaitMode.SHORT if not has_transcript else WaitMode.NONE,
            storage_policy=storage_policy,
            budget=MediaBudget(max_duration_seconds=3600, timeout_seconds=30 if has_transcript else 90),
            reasons=("speech-derived text is enough for the content route",),
            metadata=metadata,
        )

    return _basic_text_plan(bundle, route_decision, reason="content route does not justify media processing")


def _plan_image(
    bundle: MediaBundle,
    route_decision: RouteDecision | None,
    *,
    default_provider: str,
    policy: MediaModelPolicy,
) -> MediaActionPlan:
    path = _route_path(route_decision)
    if _has_derived_text(bundle) and path == "l1_only":
        return _basic_text_plan(bundle, route_decision, reason="existing OCR or summary text is enough")
    return MediaActionPlan.from_bundle(
        bundle,
        source_mode=InformationSourceMode.SAMPLED_MEDIA_WITH_TEXT,
        target_kinds=(MediaKind.IMAGE,),
        operations=(MediaOperation.PASS_IMAGE_URL, MediaOperation.RUN_OCR),
        provider=default_provider,
        model=policy.default_model_for("ocr"),
        execution_mode=ExecutionMode.SYNC,
        wait_mode=WaitMode.SHORT,
        storage_policy=StoragePolicy.NO_DOWNLOAD,
        budget=MediaBudget(max_pixels=1280 * 1280, max_input_tokens=5000, timeout_seconds=30),
        reasons=("image references are present and may contain evidence text",),
        metadata=_route_metadata(route_decision),
    )


def _plan_audio(
    bundle: MediaBundle,
    route_decision: RouteDecision | None,
    *,
    allow_download: bool,
    policy: MediaModelPolicy,
) -> MediaActionPlan:
    if _has_derived_text(bundle):
        return MediaActionPlan.from_bundle(
            bundle,
            source_mode=InformationSourceMode.DERIVED_TEXT,
            target_kinds=(MediaKind.AUDIO,),
            operations=(MediaOperation.READ_TRANSCRIPT,),
            execution_mode=ExecutionMode.SYNC,
            wait_mode=WaitMode.NONE,
            storage_policy=StoragePolicy.NO_DOWNLOAD,
            budget=MediaBudget(max_input_tokens=8000, timeout_seconds=10),
            reasons=("audio-derived transcript is already available",),
            metadata=_route_metadata(route_decision),
        )
    return MediaActionPlan.from_bundle(
        bundle,
        source_mode=InformationSourceMode.DERIVED_TEXT,
        target_kinds=(MediaKind.AUDIO,),
        operations=(MediaOperation.RUN_ASR,),
        provider="whisper",
        model=policy.default_model_for("asr"),
        execution_mode=ExecutionMode.ASYNC,
        wait_mode=WaitMode.CLIENT_BUDGET,
        storage_policy=StoragePolicy.TEMPORARY_ONLY if allow_download else StoragePolicy.NO_DOWNLOAD,
        budget=MediaBudget(max_duration_seconds=3600, timeout_seconds=90),
        reasons=("audio is present but transcript is missing",),
        metadata=_route_metadata(route_decision),
    )


def _max_duration(refs: tuple[Any, ...]) -> float | None:
    durations = [float(ref.duration_seconds) for ref in refs if ref.duration_seconds is not None]
    return max(durations) if durations else None


def _provider_downloadable(direct_media_probe: dict[str, Any] | None) -> bool:
    if direct_media_probe is None:
        return True
    provider = direct_media_probe.get("provider_downloadability") or {}
    return bool(provider.get("allow_native_media"))


def _direct_media_plan_metadata(direct_media_probe: dict[str, Any] | None) -> dict[str, Any]:
    if not direct_media_probe:
        return {}
    provider = direct_media_probe.get("provider_downloadability") or {}
    reachability = direct_media_probe.get("reachability") or {}
    return {
        "status": direct_media_probe.get("status", ""),
        "reachability_status": reachability.get("status", ""),
        "provider_downloadability_status": provider.get("status", ""),
        "allow_native_media": bool(provider.get("allow_native_media")),
    }


def _native_fallback_reason(
    *,
    prefer_native_media: bool,
    has_remote_video: bool,
    native_within_default_budget: bool,
    direct_media_probe: dict[str, Any] | None,
) -> str:
    if not prefer_native_media:
        return ""
    if not has_remote_video:
        return "no remote video reference"
    if not native_within_default_budget:
        return "video exceeds native auto duration budget"
    if direct_media_probe is not None and not _provider_downloadable(direct_media_probe):
        provider = direct_media_probe.get("provider_downloadability") or {}
        return provider.get("reason") or "direct URL is not provider-downloadable"
    return ""
