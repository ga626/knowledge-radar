"""Shared path helpers for runtime data migration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[2]
LEGACY_DATA_ROOT = REPO_ROOT / "data"
LEGACY_SRC_DATA_ROOT = REPO_ROOT / "src" / "data"
DEFAULT_RUNTIME_ROOT = REPO_ROOT / "runtime"
RUNTIME_ROOT = DEFAULT_RUNTIME_ROOT
RUNTIME_DATA_ROOT = DEFAULT_RUNTIME_ROOT / "data"
RUNTIME_MEDIA_ROOT = RUNTIME_DATA_ROOT / "media"
RUNTIME_MEDIA_CACHE_ROOT = DEFAULT_RUNTIME_ROOT / "media_cache"


def project_root() -> Path:
    return Path(os.environ.get("KR_PROJECT_ROOT") or REPO_ROOT)


def source_root() -> Path:
    return Path(os.environ.get("KR_SOURCE_ROOT") or (project_root() / "src"))


def runtime_state_dir() -> Path:
    return Path(os.environ.get("KR_STATE_DIR") or (project_root() / "runtime"))


def runtime_log_dir() -> Path:
    return Path(os.environ.get("KR_LOG_DIR") or (runtime_state_dir() / "logs"))


def browser_data_dir() -> Path:
    return Path(os.environ.get("KR_BROWSER_DATA_DIR") or (project_root() / "browser_data"))


def playwright_browsers_dir() -> Path:
    return Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or (runtime_state_dir() / "ms-playwright"))


def proxy_rule_cache_dir() -> Path:
    return Path(os.environ.get("KR_PROXY_RULE_CACHE_DIR") or (runtime_state_dir() / "proxy_rules"))


def whisper_model_cache_dir() -> Path:
    return Path(os.environ.get("KR_WHISPER_MODEL_DIR") or (runtime_state_dir() / "models" / "whisper"))


def runtime_media_dir() -> Path:
    return Path(os.environ.get("KR_RUNTIME_MEDIA_DIR") or (runtime_state_dir() / "data" / "media"))


def runtime_media_cache_dir() -> Path:
    return Path(
        os.environ.get("KR_MEDIA_CACHE_DIR")
        or os.environ.get("KR_RUNTIME_MEDIA_DIR")
        or (runtime_state_dir() / "media_cache")
    )


def legacy_runtime_media_dir() -> Path:
    return LEGACY_DATA_ROOT / "runtime_media"


def candidate_runtime_media_dirs() -> Iterable[Path]:
    """Return new-to-old media roots during the runtime/data migration window."""
    seen: set[Path] = set()
    for path in (runtime_media_cache_dir(), runtime_media_dir(), legacy_runtime_media_dir(), LEGACY_DATA_ROOT, LEGACY_SRC_DATA_ROOT):
        resolved = path
        if resolved not in seen:
            seen.add(resolved)
            yield resolved


def resolve_runtime_media_file(filename: str) -> Path:
    """Resolve a media/transcript file across new and legacy runtime locations."""
    explicit = Path(filename)
    if explicit.is_absolute():
        return explicit
    for base in candidate_runtime_media_dirs():
        candidate = base / filename
        if candidate.exists():
            return candidate
        if base == runtime_media_dir() and base.exists():
            for nested in base.glob(f"*/{filename}"):
                if nested.exists():
                    return nested
    return runtime_media_dir() / filename
