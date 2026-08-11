"""Runtime dependency preflight for imports and host executables."""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
from runtime.process import silent_subprocess_run
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Dict, List

from runtime.asr_policy import AsrPolicy
from runtime.asr_lifecycle import asr_lifecycle_summary, ctranslate2_lifecycle_probe_summary
from runtime.resource_concurrency import resource_concurrency_summary
from runtime.paths import playwright_browsers_dir, whisper_model_cache_dir
from runtime.paths import project_root
from runtime.executables import managed_chrome_resolution_summary, resolve_managed_chrome


@dataclass(frozen=True)
class DependencySpec:
    name: str
    import_name: str
    required: bool
    capability: str
    hint: str = ""


DEPENDENCIES: tuple[DependencySpec, ...] = (
    DependencySpec("mcp", "mcp", True, "MCP server runtime", "pip install mcp"),
    DependencySpec("httpx", "httpx", True, "HTTP collection", "pip install httpx"),
    DependencySpec("playwright", "playwright", False, "dynamic pages and legacy platform crawlers", "pip install playwright; playwright install"),
    DependencySpec("beautifulsoup4", "bs4", False, "generic web static fallback", "pip install beautifulsoup4"),
    DependencySpec("lxml", "lxml", False, "generic web HTML parsing", "pip install lxml"),
    DependencySpec("trafilatura", "trafilatura", False, "generic web article extraction", "pip install trafilatura"),
    DependencySpec("readability-lxml", "readability", False, "generic web readability fallback", "pip install readability-lxml"),
    DependencySpec("pypdf", "pypdf", False, "academic PDF text extraction", "pip install pypdf"),
    DependencySpec("youtube-transcript-api", "youtube_transcript_api", False, "YouTube transcript fallback", "pip install youtube-transcript-api"),
    DependencySpec("yt-dlp", "yt_dlp", False, "video download for frame extraction", "pip install yt-dlp"),
    DependencySpec("Pillow", "PIL", False, "QR/image utilities", "pip install Pillow"),
)


def _version(package: str) -> str:
    try:
        return metadata.version(package)
    except Exception:
        return ""


def _env_path(name: str) -> str:
    return str(os.environ.get(name) or "").strip()


def _first_existing(candidates: list[str]) -> str:
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return ""


def _run_version(exe: str, args: list[str]) -> str:
    if not exe:
        return ""
    try:
        result = silent_subprocess_run([exe, *args], capture_output=True, text=True, timeout=5)
    except Exception:
        return ""
    text = (result.stdout or result.stderr or "").strip().splitlines()
    return text[0][:160] if text else ""


def _ffmpeg_path() -> str:
    explicit = _env_path("KR_FFMPEG_EXE")
    if explicit:
        return explicit
    bin_dir = _env_path("KR_FFMPEG_BIN")
    suffix = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    candidates = [str(Path(bin_dir) / suffix)] if bin_dir else []
    candidates.append(str(project_root() / "runtime" / "tools" / "ffmpeg" / "bin" / suffix))
    candidates.append(shutil.which("ffmpeg") or "")
    return _first_existing(candidates)


def _chrome_path() -> str:
    selection = resolve_managed_chrome()
    return selection.path if selection else ""


def external_dependency_preflight_summary() -> Dict[str, object]:
    ffmpeg = _ffmpeg_path()
    node = _env_path("KR_NODE_EXE") or shutil.which("node") or ""
    chrome = _chrome_path()
    camoufox = _env_path("KR_CAMOUFOX_EXE")
    yt_dlp_cli = shutil.which("yt-dlp") or ""
    yt_dlp_import = importlib.util.find_spec("yt_dlp") is not None
    asr_policy = AsrPolicy.from_env()
    tools = {
        "ffmpeg": {
            "available": bool(ffmpeg),
            "path": ffmpeg,
            "configured_by": "KR_FFMPEG_EXE/KR_FFMPEG_BIN/PATH",
            "version": _run_version(ffmpeg, ["-version"]),
        },
        "yt_dlp": {
            "available": bool(yt_dlp_import or yt_dlp_cli),
            "cli_path": yt_dlp_cli,
            "import_available": yt_dlp_import,
            "version": _version("yt-dlp"),
            "configured_by": "Python import/PATH",
        },
        "faster_whisper": {
            "available": importlib.util.find_spec("faster_whisper") is not None,
            "version": _version("faster-whisper"),
            "model_cache_dir": str(whisper_model_cache_dir()),
            "policy": asr_policy.compact(),
            "concurrency": resource_concurrency_summary(),
            "ctranslate2": ctranslate2_lifecycle_probe_summary(),
            "lifecycle": asr_lifecycle_summary(),
            "configured_by": ["KR_WHISPER_MODEL_DIR", "KR_ASR_*"],
        },
        "funasr": {
            "available": importlib.util.find_spec("funasr") is not None,
            "version": _version("funasr"),
            "role": "P2.2 candidate; isolated install/smoke required before enabling",
            "default_enabled": False,
        },
        "sherpa_onnx": {
            "available": importlib.util.find_spec("sherpa_onnx") is not None,
            "version": _version("sherpa-onnx"),
            "role": "P2.2 candidate; CPU wheel smoke first, GPU experimental",
            "default_enabled": False,
        },
        "chrome": {
            "available": bool(chrome),
            "path": chrome,
            "configured_by": "managed_google_chrome_resolver",
            "resolution": managed_chrome_resolution_summary(),
        },
        "playwright_browsers": {
            "available": Path(playwright_browsers_dir()).exists(),
            "path": str(playwright_browsers_dir()),
            "configured_by": "PLAYWRIGHT_BROWSERS_PATH",
        },
        "camoufox": {
            "available": bool(camoufox and Path(camoufox).is_file()),
            "path": camoufox,
            "configured_by": "KR_CAMOUFOX_EXE",
            "role": "candidate only",
        },
        "node": {
            "available": bool(node and Path(node).is_file()),
            "path": node,
            "configured_by": "KR_NODE_EXE/PATH",
            "version": _run_version(node, ["--version"]),
        },
    }
    critical = ["ffmpeg", "yt_dlp", "faster_whisper", "chrome"]
    missing_critical = [name for name in critical if not bool(tools[name]["available"])]
    os_name = platform.system()
    status = "ok" if not missing_critical and os_name == "Windows" else "degraded"
    if missing_critical and os_name == "Windows":
        detail = "Windows 首发依赖缺失: " + ", ".join(missing_critical)
    elif os_name != "Windows":
        detail = "非 Windows 暂按 degraded/disabled 处理，未来再做跨平台支持"
    else:
        detail = "Windows 首发外部二进制和 ASR 配置可用"
    return {
        "schema": "knowledgeradar-external-dependency-preflight/v1",
        "status": status,
        "detail": detail,
        "platform_policy": {
            "primary": "Windows first",
            "current_os": os_name,
            "non_windows": "degraded; future support only",
        },
        "missing_critical": missing_critical,
        "tools": tools,
    }


def dependency_preflight_summary() -> Dict[str, object]:
    rows: List[Dict[str, object]] = []
    missing_required: List[str] = []
    missing_optional: List[str] = []
    for spec in DEPENDENCIES:
        available = importlib.util.find_spec(spec.import_name) is not None
        row = {
            "name": spec.name,
            "import_name": spec.import_name,
            "available": available,
            "required": spec.required,
            "capability": spec.capability,
            "hint": "" if available else spec.hint,
        }
        rows.append(row)
        if not available and spec.required:
            missing_required.append(spec.name)
        elif not available:
            missing_optional.append(spec.name)

    if missing_required:
        status = "down"
        detail = "关键依赖缺失: " + ", ".join(missing_required)
    elif missing_optional:
        status = "degraded"
        detail = "可选依赖缺失: " + ", ".join(missing_optional)
    else:
        status = "ok"
        detail = "关键与可选运行依赖均可导入"

    return {
        "status": status,
        "detail": detail,
        "missing_required": missing_required,
        "missing_optional": missing_optional,
        "dependencies": rows,
        "external": external_dependency_preflight_summary(),
    }
