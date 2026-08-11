"""Model and budget policy for media action planning.

The policy is intentionally side-effect free: loading it only reads environment
variables and never probes providers, fetches URLs, or touches media files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


DEFAULT_ASR_MODELS = ("local:faster-whisper/base", "local:faster-whisper/tiny")
DEFAULT_OCR_MODELS = ("PaddlePaddle/PaddleOCR-VL-1.5",)
DEFAULT_FRAME_VISION_MODELS = ("bailian:qwen3-vl-flash",)
DEFAULT_NATIVE_VIDEO_MODELS = ("bailian:qwen3-vl-flash",)
DEFAULT_NATIVE_AUDIO_VIDEO_MODELS = ("bailian:qwen3.5-omni-flash",)
# qwen-turbo previously sat first and could fail with HTTP 400/Access denied.
# Keep it out of the default blocking path; explicit legacy configuration is
# still accepted but filtered by ``ordered_models`` for video analysis.
DEFAULT_COMMENT_FILTER_MODELS = ("bailian:qwen3.5-flash",)
# Newer models are deliberately canary-only until this project records a
# successful provider/endpoint/region probe.  They are candidates, not proof
# of entitlement or availability.
CANARY_MODELS = (
    ("bailian:qwen3.7-flash", "video", "L2"),
    ("bailian:qwen3.7-plus", "video", "L3"),
    ("bailian:qwen3.8-max", "video", "L4"),
)

DEFAULT_NATIVE_AUTO_MAX_DURATION_SECONDS = 300.0
DEFAULT_NATIVE_AUTO_MAX_FRAMES = 16
DEFAULT_NATIVE_AUTO_FPS = 1.0
DEFAULT_NATIVE_AUTO_MAX_INPUT_TOKENS = 12000
DEFAULT_NATIVE_AUTO_TIMEOUT_SECONDS = 90.0


def parse_model_list(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class MediaModelPolicy:
    basic_text_models: tuple[str, ...] = ()
    asr_models: tuple[str, ...] = DEFAULT_ASR_MODELS
    ocr_models: tuple[str, ...] = DEFAULT_OCR_MODELS
    frame_vision_models: tuple[str, ...] = DEFAULT_FRAME_VISION_MODELS
    native_video_models: tuple[str, ...] = DEFAULT_NATIVE_VIDEO_MODELS
    native_audio_video_models: tuple[str, ...] = DEFAULT_NATIVE_AUDIO_VIDEO_MODELS
    comment_filter_models: tuple[str, ...] = DEFAULT_COMMENT_FILTER_MODELS

    native_auto_max_duration_seconds: float = DEFAULT_NATIVE_AUTO_MAX_DURATION_SECONDS
    native_auto_max_frames: int = DEFAULT_NATIVE_AUTO_MAX_FRAMES
    native_auto_fps: float = DEFAULT_NATIVE_AUTO_FPS
    native_auto_max_input_tokens: int = DEFAULT_NATIVE_AUTO_MAX_INPUT_TOKENS
    native_auto_timeout_seconds: float = DEFAULT_NATIVE_AUTO_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls) -> "MediaModelPolicy":
        return cls(
            basic_text_models=tuple(parse_model_list(os.environ.get("KR_BASIC_TEXT_MODELS"))),
            asr_models=tuple(parse_model_list(os.environ.get("KR_ASR_MODELS")) or DEFAULT_ASR_MODELS),
            ocr_models=tuple(parse_model_list(os.environ.get("KR_OCR_MODELS")) or DEFAULT_OCR_MODELS),
            frame_vision_models=tuple(
                parse_model_list(os.environ.get("KR_FRAME_VISION_MODELS")) or DEFAULT_FRAME_VISION_MODELS
            ),
            native_video_models=tuple(
                parse_model_list(os.environ.get("KR_NATIVE_VIDEO_MODELS")) or DEFAULT_NATIVE_VIDEO_MODELS
            ),
            native_audio_video_models=tuple(
                parse_model_list(os.environ.get("KR_NATIVE_AUDIO_VIDEO_MODELS"))
                or DEFAULT_NATIVE_AUDIO_VIDEO_MODELS
            ),
            comment_filter_models=tuple(
                parse_model_list(os.environ.get("KR_COMMENT_FILTER_MODELS"))
                or DEFAULT_COMMENT_FILTER_MODELS
            ),
            native_auto_max_duration_seconds=env_float(
                "KR_NATIVE_AUTO_MAX_DURATION_SECONDS", DEFAULT_NATIVE_AUTO_MAX_DURATION_SECONDS
            ),
            native_auto_max_frames=env_int("KR_NATIVE_AUTO_MAX_FRAMES", DEFAULT_NATIVE_AUTO_MAX_FRAMES),
            native_auto_fps=env_float("KR_NATIVE_AUTO_FPS", DEFAULT_NATIVE_AUTO_FPS),
            native_auto_max_input_tokens=env_int(
                "KR_NATIVE_AUTO_MAX_INPUT_TOKENS", DEFAULT_NATIVE_AUTO_MAX_INPUT_TOKENS
            ),
            native_auto_timeout_seconds=env_float(
                "KR_NATIVE_AUTO_TIMEOUT_SECONDS", DEFAULT_NATIVE_AUTO_TIMEOUT_SECONDS
            ),
        )

    def models_for(self, key: str) -> tuple[str, ...]:
        mapping = {
            "basic_text": self.basic_text_models,
            "asr": self.asr_models,
            "ocr": self.ocr_models,
            "frame_vision": self.frame_vision_models,
            "native_video": self.native_video_models,
            "native_audio_video": self.native_audio_video_models,
            "comment_filter": self.comment_filter_models,
        }
        return mapping.get(key, ())

    def default_model_for(self, key: str) -> str:
        models = self.models_for(key)
        return models[0] if models else ""

    def capability_registry(self) -> dict[str, dict[str, Any]]:
        """Return a small, provider-neutral capability registry.

        A quota or configured alias is not treated as proof of modality
        support. The registry is deliberately conservative until a provider
        canary records a stronger contract.
        """
        registry: dict[str, dict[str, Any]] = {}
        for key, modality, tier in (
            ("asr", "audio", "L1"),
            ("ocr", "image", "OCR"),
            ("frame_vision", "image", "L1"),
            ("native_video", "video", "L2"),
            ("native_audio_video", "audio+video", "L3"),
            ("comment_filter", "text", "L1"),
        ):
            for model in self.models_for(key):
                ref = str(model)
                registry[ref] = {
                    "model_ref": ref,
                    "capability": key,
                    "input_modality": modality,
                    "quality_tier": tier,
                    "canary_required": ref.startswith("bailian:"),
                    "blocked_default": ref.lower() in {"bailian:qwen-turbo", "qwen-turbo"} and key in {"native_video", "native_audio_video", "comment_filter"},
                }
        for ref, modality, tier in CANARY_MODELS:
            registry.setdefault(ref, {
                "model_ref": ref,
                "capability": "native_video",
                "input_modality": modality,
                "quality_tier": tier,
                "canary_required": True,
                "blocked_default": True,
                "availability_evidence": "not_probed",
            })
        return registry

    def ordered_models(self, key: str) -> tuple[str, ...]:
        """Return candidates with known-bad legacy video models removed."""
        models = self.models_for(key)
        return tuple(
            model
            for model in models
            if not (
                str(model).lower().removeprefix("bailian:") == "qwen-turbo"
                and key in {"native_video", "native_audio_video", "comment_filter"}
            )
        )
