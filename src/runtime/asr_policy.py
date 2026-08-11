"""Runtime ASR policy helpers.

This module is intentionally side-effect-light: it reads environment variables
and normalizes faster-whisper options, but does not load models or touch media.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path

from runtime.paths import whisper_model_cache_dir


DEFAULT_ASR_MODELS = ("local:faster-whisper/base", "local:faster-whisper/tiny")
P2_ASR_ENGINE_CANDIDATES = (
    "local:faster-whisper/base",
    "local:faster-whisper/tiny",
    "local:funasr/sensevoice-small",
    "local:sherpa-onnx/sensevoice-int8",
)


def _split_models(value: str | None) -> tuple[str, ...]:
    models = tuple(item.strip() for item in (value or "").split(",") if item.strip())
    return models or DEFAULT_ASR_MODELS


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        return default


def faster_whisper_model_name(model_ref: str) -> str:
    value = (model_ref or "").strip()
    if value.startswith("local:faster-whisper/"):
        return value.split("/", 1)[1].strip() or "base"
    if value.startswith("faster-whisper/"):
        return value.split("/", 1)[1].strip() or "base"
    return value or "base"


def transcript_cache_key(
    *,
    platform: str,
    content_id: str,
    audio_path: str = "",
    audio_hash: str = "",
    engine: str = "faster-whisper",
    model: str = "base",
    device: str = "cpu",
    compute_type: str = "int8",
    language: str = "zh",
    vad: bool = True,
    beam: int = 5,
) -> dict[str, object]:
    """Return the canonical transcript cache key and its explicit fields."""
    resolved_audio_hash = audio_hash or _file_hash(audio_path)
    fields = {
        "platform": platform or "unknown",
        "content_id": content_id or "unknown",
        "audio_hash": resolved_audio_hash or "unknown",
        "engine": engine or "faster-whisper",
        "model": model or "base",
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
    beam_size: int = 5
    vad_enabled: bool = True
    language: str = "zh"
    model_cache_dir: str = ""
    batch_size: int = 8

    @classmethod
    def from_env(cls) -> "AsrPolicy":
        return cls(
            models=_split_models(os.environ.get("KR_ASR_MODELS")),
            device=(os.environ.get("KR_ASR_DEVICE") or "cpu").strip() or "cpu",
            compute_type=(os.environ.get("KR_ASR_COMPUTE_TYPE") or "int8").strip() or "int8",
            beam_size=max(1, _env_int("KR_ASR_BEAM_SIZE", 5)),
            vad_enabled=_env_bool("KR_ASR_VAD", True),
            language=(os.environ.get("KR_ASR_LANGUAGE") or "zh").strip() or "zh",
            model_cache_dir=str(whisper_model_cache_dir()),
            batch_size=max(1, _env_int("KR_ASR_BATCH_SIZE", 8)),
        )

    @property
    def primary_model(self) -> str:
        return faster_whisper_model_name(self.models[0] if self.models else DEFAULT_ASR_MODELS[0])

    def compact(self) -> dict[str, object]:
        return {
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
