"""ASR strategy planning contracts.

This module does not fetch subtitles, download audio, or load ASR models. It
produces a deterministic plan that platform collectors can execute and that
agents can inspect through capabilities/benchmark output.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from runtime.asr_policy import AsrPolicy
from runtime.resource_concurrency import (
    RESOURCE_ASR_CPU,
    RESOURCE_ASR_GPU,
    RESOURCE_MEDIA_DOWNLOAD,
    RESOURCE_SUBTITLE_PROBE,
    ResourceConcurrencyPolicy,
)


SUBTITLE_UNKNOWN = "unknown"
SUBTITLE_HIT = "hit"
SUBTITLE_MISS = "miss"
SUBTITLE_UNSUPPORTED = "unsupported"
SUBTITLE_ERROR = "error"


@dataclass(frozen=True)
class SubtitleProbeResult:
    platform: str
    status: str = SUBTITLE_UNKNOWN
    source: str = ""
    reason: str = ""
    text_chars: int = 0
    line_count: int = 0
    duration_s: float = 0.0

    @property
    def hit(self) -> bool:
        return self.status == SUBTITLE_HIT and self.text_chars > 0

    def compact(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AsrStrategyInput:
    platform: str
    content_id: str = ""
    source_url: str = ""
    media_duration_seconds: float | None = None
    has_transcript_cache: bool = False
    platform_subtitle: SubtitleProbeResult | None = None
    downloader_subtitle: SubtitleProbeResult | None = None
    allow_downloader_subtitle: bool = True
    allow_local_asr: bool = True
    prefer_gpu: bool = False
    gpu_available: bool | None = None
    cpu_load_percent: float | None = None
    audio_language: str = ""
    needs_final_transcript: bool = True


@dataclass(frozen=True)
class AsrStrategyStep:
    id: str
    kind: str
    status: str
    resource_kind: str = ""
    engine: str = ""
    reason: str = ""
    blocks_final_report: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def compact(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AsrStrategyPlan:
    schema: str
    selected_step_id: str
    final_transcript_expected: bool
    steps: tuple[AsrStrategyStep, ...]
    policy: dict[str, Any]
    concurrency: dict[str, Any]

    def compact(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "selected_step_id": self.selected_step_id,
            "final_transcript_expected": self.final_transcript_expected,
            "steps": [step.compact() for step in self.steps],
            "policy": self.policy,
            "concurrency": self.concurrency,
        }


ASR_ENGINE_BENCHMARK_PROFILE = {
    "schema": "knowledgeradar-asr-engine-benchmark-profile/v1",
    "status": "quality_requires_real_receipt",
    "validated_paths": {},
}


def _status(value: str | None) -> str:
    normalized = (value or SUBTITLE_UNKNOWN).strip().lower()
    return (
        normalized
        if normalized in {SUBTITLE_UNKNOWN, SUBTITLE_HIT, SUBTITLE_MISS, SUBTITLE_UNSUPPORTED, SUBTITLE_ERROR}
        else SUBTITLE_UNKNOWN
    )


def _default_platform_probe(platform: str) -> SubtitleProbeResult:
    normalized = (platform or "").strip().lower()
    if normalized in {"bilibili", "b站", "bili"}:
        return SubtitleProbeResult(platform=platform, status=SUBTITLE_UNKNOWN, source="bilibili_player_v2")
    if normalized in {"youtube", "yt"}:
        return SubtitleProbeResult(platform=platform, status=SUBTITLE_UNKNOWN, source="youtube_transcript_or_captions")
    return SubtitleProbeResult(
        platform=platform,
        status=SUBTITLE_UNSUPPORTED,
        source="platform_subtitle_probe",
        reason="no stable public probe implemented",
    )


def _default_downloader_probe(platform: str, allow: bool) -> SubtitleProbeResult:
    if not allow:
        return SubtitleProbeResult(platform=platform, status=SUBTITLE_UNSUPPORTED, source="yt-dlp", reason="disabled")
    normalized = (platform or "").strip().lower()
    if normalized in {"bilibili", "b站", "bili", "youtube", "yt"}:
        return SubtitleProbeResult(platform=platform, status=SUBTITLE_UNKNOWN, source="yt-dlp subtitles")
    return SubtitleProbeResult(
        platform=platform,
        status=SUBTITLE_UNSUPPORTED,
        source="yt-dlp subtitles",
        reason="not validated for this platform",
    )


def _select_local_asr_profile(request: AsrStrategyInput, policy: AsrPolicy) -> dict[str, Any]:
    from runtime.model_roles import ASR_MODEL_REF

    return {
        "model_ref": ASR_MODEL_REF,
        "device": policy.device,
        "compute_type": policy.compute_type,
        "status": "single_selected_engine",
        "selection_reason": "字幕缺失时按需启动隔离 0.6B worker；无备用引擎",
    }


def build_asr_strategy_plan(
    request: AsrStrategyInput,
    *,
    asr_policy: AsrPolicy | None = None,
    concurrency_policy: ResourceConcurrencyPolicy | None = None,
) -> AsrStrategyPlan:
    policy = asr_policy or AsrPolicy.from_env()
    concurrency = concurrency_policy or ResourceConcurrencyPolicy.from_env()
    platform_probe = request.platform_subtitle or _default_platform_probe(request.platform)
    downloader_probe = request.downloader_subtitle or _default_downloader_probe(
        request.platform, request.allow_downloader_subtitle
    )
    platform_status = _status(platform_probe.status)
    downloader_status = _status(downloader_probe.status)
    steps: list[AsrStrategyStep] = []

    if request.has_transcript_cache:
        steps.append(
            AsrStrategyStep(
                id="transcript_cache",
                kind="derived_text_cache",
                status="selected",
                reason="transcript cache is available; skip subtitle probe, download, and local ASR",
                blocks_final_report=request.needs_final_transcript,
            )
        )
        return _plan("transcript_cache", request, steps, policy, concurrency)

    platform_step_status = (
        "selected" if platform_probe.hit else ("candidate" if platform_status == SUBTITLE_UNKNOWN else "skipped")
    )
    steps.append(
        AsrStrategyStep(
            id="platform_subtitle",
            kind="subtitle_probe",
            status=platform_step_status,
            resource_kind=RESOURCE_SUBTITLE_PROBE,
            reason=platform_probe.reason
            or ("platform subtitle hit" if platform_probe.hit else f"platform subtitle status={platform_status}"),
            blocks_final_report=request.needs_final_transcript,
            metadata=platform_probe.compact(),
        )
    )
    if platform_probe.hit:
        return _plan("platform_subtitle", request, steps, policy, concurrency)

    downloader_step_status = (
        "selected" if downloader_probe.hit else ("candidate" if downloader_status == SUBTITLE_UNKNOWN else "skipped")
    )
    steps.append(
        AsrStrategyStep(
            id="downloader_subtitle",
            kind="downloader_subtitle_probe",
            status=downloader_step_status,
            resource_kind=RESOURCE_SUBTITLE_PROBE,
            engine="yt-dlp",
            reason=downloader_probe.reason
            or (
                "downloader subtitle hit" if downloader_probe.hit else f"downloader subtitle status={downloader_status}"
            ),
            blocks_final_report=request.needs_final_transcript,
            metadata=downloader_probe.compact(),
        )
    )
    if downloader_probe.hit:
        return _plan("downloader_subtitle", request, steps, policy, concurrency)

    local_profile = _select_local_asr_profile(request, policy)
    resource = RESOURCE_ASR_GPU if str(local_profile.get("device")) == "cuda" else RESOURCE_ASR_CPU
    if request.allow_local_asr:
        steps.append(
            AsrStrategyStep(
                id="local_asr",
                kind="local_asr",
                status="selected",
                resource_kind=resource,
                engine=str(local_profile.get("model_ref") or "local:qwen3-asr/0.6b-hf"),
                reason=f"subtitle/cache miss; {local_profile.get('selection_reason')}",
                blocks_final_report=request.needs_final_transcript,
                metadata={
                    "requires_media_download": True,
                    "download_resource_kind": RESOURCE_MEDIA_DOWNLOAD,
                    "media_duration_seconds": request.media_duration_seconds,
                    "audio_language": request.audio_language,
                    "benchmark_profile": local_profile,
                    "fallback_chain": [],
                },
            )
        )
        return _plan("local_asr", request, steps, policy, concurrency)

    steps.append(
        AsrStrategyStep(
            id="no_final_transcript",
            kind="degraded",
            status="selected",
            reason="local ASR disabled and no transcript/subtitle source is available",
            blocks_final_report=False,
        )
    )
    return _plan("no_final_transcript", request, steps, policy, concurrency, final_transcript_expected=False)


def _plan(
    selected: str,
    request: AsrStrategyInput,
    steps: list[AsrStrategyStep],
    policy: AsrPolicy,
    concurrency: ResourceConcurrencyPolicy,
    *,
    final_transcript_expected: bool | None = None,
) -> AsrStrategyPlan:
    return AsrStrategyPlan(
        schema="knowledgeradar-asr-strategy-plan/v1",
        selected_step_id=selected,
        final_transcript_expected=request.needs_final_transcript
        if final_transcript_expected is None
        else final_transcript_expected,
        steps=tuple(steps),
        policy=policy.compact(),
        concurrency=concurrency.compact(),
    )


def asr_strategy_manifest() -> dict[str, Any]:
    from runtime.model_roles import ASR_MODEL_REF

    sample = build_asr_strategy_plan(AsrStrategyInput(platform="bilibili", content_id="BV_SAMPLE"))
    return {
        "schema": "knowledgeradar-asr-strategy/v1",
        "status": "implemented",
        "strategy_order": ["transcript_cache", "platform_subtitle", "downloader_subtitle", "local_asr"],
        "default_engine": ASR_MODEL_REF,
        "engine_candidates": [ASR_MODEL_REF],
        "active_fallback_chain": [],
        "cache_policy": "固定输入哈希、权重/安装身份、语言和解析合同；不混用旧引擎结果",
        "sample_plan": sample.compact(),
    }
