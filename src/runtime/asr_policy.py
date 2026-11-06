"""Runtime ASR policy helpers.

This module is intentionally side-effect-light: it reads environment variables
and normalizes the selected Qwen3-ASR worker options, but does not load models or touch media.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path

from runtime.paths import runtime_state_dir
from runtime.model_roles import ASR_COMPONENT, ASR_MODEL_REF


DEFAULT_ASR_MODELS = (ASR_MODEL_REF,)
P2_ASR_ENGINE_CANDIDATES = DEFAULT_ASR_MODELS

# 字幕偏好与音频识别分开；这里规范化模型已知的语言，不限制字幕为中英。
LANGUAGE_NAMES = dict(zip(
    'zh en yue ar de es fr hi id it ja ko ms nl pl pt ru sv th tr vi cs da fi fil hu el fa ro mk'.split(),
    ('Chinese English Cantonese Arabic German Spanish French Hindi Indonesian Italian Japanese Korean '
     'Malay Dutch Polish Portuguese Russian Swedish Thai Turkish Vietnamese Czech Danish Finnish Filipino '
     'Hungarian Greek Persian Romanian Macedonian').split()))


def normalize_language(value):
    value = str(value or '').strip().lower()
    if value in {'', 'auto', 'mixed', 'zh-en'}:
        return ''
    names = {name.lower(): code for code, name in LANGUAGE_NAMES.items()}
    code = names.get(value, value.replace('_', '-').split('-')[0])
    if code not in LANGUAGE_NAMES:
        raise ValueError('invalid_asr_language')
    return code


def _split_models(value: str | None) -> tuple[str, ...]:
    # 旧版环境覆盖不再唤起已退役引擎；新版本只提供已批准的单一职责。
    return DEFAULT_ASR_MODELS


def transcript_cache_key(
    *,
    platform: str,
    content_id: str,
    audio_path: str = "",
    audio_hash: str = "",
    engine: str = "sherpa_onnx_int8",
    model: str = ASR_COMPONENT,
    device: str = "cpu",
    compute_type: str = "int8",
    language: str = "",
    vad: bool = False,
    beam: int = 1,
) -> dict[str, object]:
    """Return the canonical transcript cache key and its explicit fields."""
    resolved_audio_hash = audio_hash or _file_hash(audio_path)
    fields = {
        "platform": platform or "unknown",
        "content_id": content_id or "unknown",
        "audio_hash": resolved_audio_hash or "unknown",
        "engine": engine or "sherpa_onnx_int8",
        "model": model or ASR_COMPONENT,
        "device": device or "cpu",
        "compute": compute_type or "int8",
        "language": language or "",
        "vad": bool(vad),
        "beam": int(beam or 1),
    }
    raw = "|".join(f"{name}={fields[name]}" for name in fields)
    return {
        "schema": "knowledgeradar-transcript-cache-key/v1",
        "fields": fields,
        "key": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32],
    }


def _file_hash(path: str) -> str:
    if not path:
        return ""
    file_path = Path(path)
    if not file_path.is_file():
        return ""
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class AsrPolicy:
    models: tuple[str, ...] = DEFAULT_ASR_MODELS
    device: str = "cpu"
    compute_type: str = "int8"
    beam_size: int = 1
    vad_enabled: bool = False
    language: str = ""
    model_cache_dir: str = ""
    batch_size: int = 1

    @classmethod
    def from_env(cls, *, language: str | None = None) -> "AsrPolicy":
        if (os.environ.get('KR_ASR_DEVICE') or 'cpu').strip() != 'cpu':
            raise ValueError('unsupported_asr_device:CPU_INT8_required')
        return cls(
            models=_split_models(os.environ.get("KR_ASR_MODELS")),
            device="cpu",
            compute_type="int8",
            beam_size=1,
            vad_enabled=False,
            language=normalize_language(os.environ.get("KR_ASR_LANGUAGE") if language is None else language),
            model_cache_dir=str(runtime_state_dir() / "optional_components"),
            batch_size=1,
        )

    @property
    def primary_model(self) -> str:
        return ASR_COMPONENT

    @classmethod
    def for_request(cls, language: str | None = None) -> "AsrPolicy":
        return cls.from_env(language=language)

    def compact(self) -> dict[str, object]:
        return {
            "selected_component": ASR_COMPONENT,
            "models": list(self.models),
            "model": self.primary_model,
            "engine_candidates": list(P2_ASR_ENGINE_CANDIDATES),
            "device": self.device,
            "compute_type": self.compute_type,
            "beam_size": self.beam_size,
            "vad_enabled": self.vad_enabled,
            "language": self.language,
            "model_cache_dir": self.model_cache_dir,
            "batch_size": self.batch_size,
            "transcript_cache_key_fields": [
                "platform",
                "content_id",
                "audio_hash",
                "engine",
                "model",
                "device",
                "compute",
                "language",
                "vad",
                "beam",
            ],
        }
