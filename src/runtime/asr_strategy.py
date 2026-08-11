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


ASR_ENGINE_BENCHMARK_PROFILE: dict[str, Any] = {
    "schema": "knowledgeradar-asr-engine-benchmark-profile/v1",
    "created_at": "2026-06-05",
    "host": "Windows RTX 2070 SUPER 8GB local fixtures",
    "evidence_files": [
        "runtime/verification/p2_2_faster_whisper_engine_matrix.json",
        "runtime/verification/p2_2_funasr_smoke_58s_5m.json",
        "runtime/verification/p2_2_sherpa_smoke_58s_5m.json",
        "runtime/verification/p2_2_asr_concurrency_gpu_serial.json",
        "runtime/verification/p2_2_asr_concurrency_gpu_cpu_parallel.json",
    ],
    "validated_paths": {
        "faster_whisper_cpu_base_int8": {
            "model_ref": "local:faster-whisper/base",
            "device": "cpu",
            "compute_type": "int8",
            "status": "default_main_path",
            "reason": "best quality/speed balance without GPU lifecycle risk",
            "local_58s": {"model_load_s": 21.529, "transcribe_s": 12.733, "total_s": 34.413},
            "local_5m": {"model_load_s": 0.0, "transcribe_s": 51.08, "total_s": 51.247},
        },
        "faster_whisper_cpu_tiny_int8": {
            "model_ref": "local:faster-whisper/tiny",
            "device": "cpu",
            "compute_type": "int8",
            "status": "speed_fallback",
            "reason": "fastest CPU fallback when load is high or quality bar is lower",
            "local_58s": {"model_load_s": 21.769, "transcribe_s": 7.659, "total_s": 34.716},
            "local_5m": {"model_load_s": 0.0, "transcribe_s": 30.037, "total_s": 30.221},
        },
        "faster_whisper_gpu_base_float16": {
            "model_ref": "local:faster-whisper/base",
            "device": "cuda",
            "compute_type": "float16",
            "status": "opt_in_hot_path",
            "reason": "fast after first load, but native cleanup returned non-zero in validation and consumes VRAM",
            "local_58s": {"model_load_s": 21.378, "transcribe_s": 4.806, "total_s": 26.326},
            "local_5m": {"model_load_s": 0.0, "transcribe_s": 15.2, "total_s": 15.359},
            "lifecycle_risk": "completed rows but GPU benchmark process returned exit code 1 during native cleanup",
        },
        "funasr_sensevoice_cpu": {
            "model_ref": "local:funasr/sensevoice-small",
            "device": "cpu",
            "compute_type": "torch_cpu",
            "status": "optional_uninstalled_after_benchmark",
            "reason": "benchmark evidence retained, but package is currently uninstalled from main .python312; cold load was 184.5s on 58s fixture and 89.286s on 5m fixture",
            "local_58s": {"model_load_s": 184.5, "transcribe_s": 8.361, "total_s": 193.024},
            "local_5m": {"model_load_s": 89.286, "transcribe_s": 37.659, "total_s": 127.096},
        },
        "sherpa_onnx_sensevoice_cpu": {
            "model_ref": "local:sherpa-onnx/sensevoice-int8",
            "device": "cpu",
            "compute_type": "onnxruntime_cpu_int8",
            "status": "optional_uninstalled_after_benchmark",
            "reason": "benchmark evidence retained, but package is currently uninstalled from main .python312; 5m fixture took 213.67s and quality was poor",
            "local_58s": {"model_load_s": 1.443, "transcribe_s": 26.764, "total_s": 28.36},
            "local_5m": {"model_load_s": 1.323, "transcribe_s": 212.199, "total_s": 213.67},
        },
    },
    "concurrency_conclusion": {
        "cpu_asr_default": 1,
        "gpu_asr_default": 1,
        "reason": "CPU parallel and GPU+CPU parallel were dominated by CPU long-audio latency; do not raise concurrency without new benchmark evidence",
    },
}


def _status(value: str | None) -> str:
    normalized = (value or SUBTITLE_UNKNOWN).strip().lower()
    return normalized if normalized in {SUBTITLE_UNKNOWN, SUBTITLE_HIT, SUBTITLE_MISS, SUBTITLE_UNSUPPORTED, SUBTITLE_ERROR} else SUBTITLE_UNKNOWN


def _default_platform_probe(platform: str) -> SubtitleProbeResult:
    normalized = (platform or "").strip().lower()
    if normalized in {"bilibili", "b站", "bili"}:
        return SubtitleProbeResult(platform=platform, status=SUBTITLE_UNKNOWN, source="bilibili_player_v2")
    if normalized in {"youtube", "yt"}:
        return SubtitleProbeResult(platform=platform, status=SUBTITLE_UNKNOWN, source="youtube_transcript_or_captions")
    return SubtitleProbeResult(platform=platform, status=SUBTITLE_UNSUPPORTED, source="platform_subtitle_probe", reason="no stable public probe implemented")


def _default_downloader_probe(platform: str, allow: bool) -> SubtitleProbeResult:
    if not allow:
        return SubtitleProbeResult(platform=platform, status=SUBTITLE_UNSUPPORTED, source="yt-dlp", reason="disabled")
    normalized = (platform or "").strip().lower()
    if normalized in {"bilibili", "b站", "bili", "youtube", "yt"}:
        return SubtitleProbeResult(platform=platform, status=SUBTITLE_UNKNOWN, source="yt-dlp subtitles")
    return SubtitleProbeResult(platform=platform, status=SUBTITLE_UNSUPPORTED, source="yt-dlp subtitles", reason="not validated for this platform")


def _select_local_asr_profile(request: AsrStrategyInput, policy: AsrPolicy) -> dict[str, Any]:
    device = (policy.device or "").strip().lower()
    compute = (policy.compute_type or "").strip().lower()
    models = tuple(policy.models or ())
    cpu_load = request.cpu_load_percent
    gpu_allowed = request.prefer_gpu or device in {"cuda", "gpu"}
    gpu_available = True if request.gpu_available is None else request.gpu_available

    if gpu_allowed and gpu_available and any("faster-whisper/base" in model for model in models) and compute in {"float16", "int8_float16"}:
        profile = dict(ASR_ENGINE_BENCHMARK_PROFILE["validated_paths"]["faster_whisper_gpu_base_float16"])
        profile["selection_reason"] = "GPU explicitly requested and benchmarked faster-whisper/base float16 is available"
        return profile

    if cpu_load is not None and cpu_load >= 75 and any("faster-whisper/tiny" in model for model in models):
        profile = dict(ASR_ENGINE_BENCHMARK_PROFILE["validated_paths"]["faster_whisper_cpu_tiny_int8"])
        profile["selection_reason"] = "CPU load is high; choose benchmarked faster CPU fallback"
        return profile

    if any("faster-whisper/base" in model for model in models):
        profile = dict(ASR_ENGINE_BENCHMARK_PROFILE["validated_paths"]["faster_whisper_cpu_base_int8"])
        profile["selection_reason"] = "default benchmarked CPU main path"
        return profile

    if any("faster-whisper/tiny" in model for model in models):
        profile = dict(ASR_ENGINE_BENCHMARK_PROFILE["validated_paths"]["faster_whisper_cpu_tiny_int8"])
        profile["selection_reason"] = "configured models omit base; use benchmarked tiny fallback"
        return profile

    profile = dict(ASR_ENGINE_BENCHMARK_PROFILE["validated_paths"]["faster_whisper_cpu_base_int8"])
    profile["selection_reason"] = "configured engine lacks benchmark support; degrade to default faster-whisper/base profile"
    return profile


def build_asr_strategy_plan(
    request: AsrStrategyInput,
    *,
    asr_policy: AsrPolicy | None = None,
    concurrency_policy: ResourceConcurrencyPolicy | None = None,
) -> AsrStrategyPlan:
    policy = asr_policy or AsrPolicy.from_env()
    concurrency = concurrency_policy or ResourceConcurrencyPolicy.from_env()
    platform_probe = request.platform_subtitle or _default_platform_probe(request.platform)
    downloader_probe = request.downloader_subtitle or _default_downloader_probe(request.platform, request.allow_downloader_subtitle)
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

    platform_step_status = "selected" if platform_probe.hit else ("candidate" if platform_status == SUBTITLE_UNKNOWN else "skipped")
    steps.append(
        AsrStrategyStep(
            id="platform_subtitle",
            kind="subtitle_probe",
            status=platform_step_status,
            resource_kind=RESOURCE_SUBTITLE_PROBE,
            reason=platform_probe.reason or ("platform subtitle hit" if platform_probe.hit else f"platform subtitle status={platform_status}"),
            blocks_final_report=request.needs_final_transcript,
            metadata=platform_probe.compact(),
        )
    )
    if platform_probe.hit:
        return _plan("platform_subtitle", request, steps, policy, concurrency)

    downloader_step_status = "selected" if downloader_probe.hit else ("candidate" if downloader_status == SUBTITLE_UNKNOWN else "skipped")
    steps.append(
        AsrStrategyStep(
            id="downloader_subtitle",
            kind="downloader_subtitle_probe",
            status=downloader_step_status,
            resource_kind=RESOURCE_SUBTITLE_PROBE,
            engine="yt-dlp",
            reason=downloader_probe.reason or ("downloader subtitle hit" if downloader_probe.hit else f"downloader subtitle status={downloader_status}"),
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
                engine=str(local_profile.get("model_ref") or "local:faster-whisper/base"),
                reason=f"subtitle/cache miss; {local_profile.get('selection_reason')}",
                blocks_final_report=request.needs_final_transcript,
                metadata={
                    "requires_media_download": True,
                    "download_resource_kind": RESOURCE_MEDIA_DOWNLOAD,
                    "media_duration_seconds": request.media_duration_seconds,
                    "audio_language": request.audio_language,
                    "benchmark_profile": local_profile,
                    "fallback_chain": [
                        "local:faster-whisper/base cpu/int8",
                        "local:faster-whisper/tiny cpu/int8",
                    ],
                    "not_selected_engines": {
                        "funasr": ASR_ENGINE_BENCHMARK_PROFILE["validated_paths"]["funasr_sensevoice_cpu"]["reason"],
                        "sherpa_onnx": ASR_ENGINE_BENCHMARK_PROFILE["validated_paths"]["sherpa_onnx_sensevoice_cpu"]["reason"],
                    },
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
        final_transcript_expected=request.needs_final_transcript if final_transcript_expected is None else final_transcript_expected,
        steps=tuple(steps),
        policy=policy.compact(),
        concurrency=concurrency.compact(),
    )


def asr_strategy_manifest() -> dict[str, Any]:
    sample = build_asr_strategy_plan(AsrStrategyInput(platform="bilibili", content_id="BV_SAMPLE"))
    return {
        "schema": "knowledgeradar-asr-strategy/v1",
        "status": "implemented_p2_2",
        "strategy_order": ["transcript_cache", "platform_subtitle", "downloader_subtitle", "local_asr"],
        "benchmark_profile": ASR_ENGINE_BENCHMARK_PROFILE,
        "platforms": {
            "bilibili": {
                "platform_subtitle": "implemented via Bilibili player subtitle API in collector fast path",
                "downloader_subtitle": "candidate via yt-dlp subtitles",
                "local_asr": "implemented via benchmarked faster-whisper path; FunASR and sherpa-onnx remain optional benchmark candidates and are currently uninstalled unless dependency preflight reports them available",
            },
            "youtube": {
                "platform_subtitle": "candidate via youtube-transcript-api or YouTube Captions metadata when authorized",
                "downloader_subtitle": "candidate via yt-dlp --write-subs/--write-auto-subs",
                "local_asr": "future execution hook; current environment may be unable to validate YouTube live paths",
            },
            "short_video_platforms": {
                "platform_subtitle": "degraded; no stable public subtitle API evidence",
                "local_asr": "future platform-specific media access required",
            },
        },
        "engine_candidates": list(ASR_ENGINE_BENCHMARK_PROFILE["validated_paths"].keys()),
        "default_engine": "local:faster-whisper/base",
        "active_fallback_chain": ["local:faster-whisper/base cpu/int8", "local:faster-whisper/tiny cpu/int8"],
        "disabled_after_real_run": {
            "local:funasr/sensevoice-small": ASR_ENGINE_BENCHMARK_PROFILE["validated_paths"]["funasr_sensevoice_cpu"]["reason"],
            "local:sherpa-onnx/sensevoice-int8": ASR_ENGINE_BENCHMARK_PROFILE["validated_paths"]["sherpa_onnx_sensevoice_cpu"]["reason"],
        },
        "cache_policy": "store strategy outcome, hit rates, timings, quality summary and failure tags; do not persist full transcript in evolution cache",
        "sample_plan": sample.compact(),
    }
