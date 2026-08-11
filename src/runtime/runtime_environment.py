"""Read-only runtime environment and productization boundary manifest."""

from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path
from typing import Any, Dict

from runtime.dependency_preflight import dependency_preflight_summary
from runtime.executables import find_node_exe, managed_chrome_resolution_summary, resolve_managed_chrome
from runtime.paths import (
    browser_data_dir,
    playwright_browsers_dir,
    project_root,
    proxy_rule_cache_dir,
    runtime_log_dir,
    runtime_media_cache_dir,
    runtime_media_dir,
    runtime_state_dir,
    whisper_model_cache_dir,
)
from runtime.proxy_rules import proxy_rules_summary
from runtime.profile_registry import registry_path
from runtime.task_worker import task_worker_summary
from runtime.tasks import default_task_db_path


def _env(name: str, default: str = "") -> str:
    return str(os.environ.get(name) or default)


def _path_state(path: str | Path, *, configured_by: str = "", secret: bool = False) -> Dict[str, Any]:
    p = Path(path)
    return {
        "path": str(p),
        "exists": p.exists(),
        "is_dir": p.is_dir(),
        "is_file": p.is_file(),
        "configured_by": configured_by,
        "release_policy": "exclude_runtime_or_secret" if secret else "runtime_local_only",
    }


def _which_or_empty(name: str) -> str:
    return shutil.which(name) or ""


def _safe_find_node() -> str:
    try:
        return find_node_exe()
    except Exception:
        return ""


def _safe_find_chrome() -> str:
    selection = resolve_managed_chrome()
    return selection.path if selection else ""


def _safe_find_ffmpeg() -> str:
    explicit = _env("KR_FFMPEG_EXE")
    if explicit:
        return explicit
    suffix = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    ffmpeg_bin = _env("KR_FFMPEG_BIN")
    candidates = []
    if ffmpeg_bin:
        candidates.append(Path(ffmpeg_bin) / suffix)
    candidates.append(project_root() / "runtime" / "tools" / "ffmpeg" / "bin" / suffix)
    path_candidate = _which_or_empty("ffmpeg")
    if path_candidate:
        candidates.append(Path(path_candidate))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return ""


def runtime_environment_manifest() -> Dict[str, Any]:
    """Return a non-invasive manifest of local assumptions and configured paths."""
    state_dir = runtime_state_dir()
    log_dir = runtime_log_dir()
    media_cache = runtime_media_cache_dir()
    browser_dir = browser_data_dir()
    playwright_dir = playwright_browsers_dir()
    task_db = default_task_db_path()
    ffmpeg = _safe_find_ffmpeg()
    yt_dlp_available = shutil.which("yt-dlp") or ""
    chrome = _safe_find_chrome()
    node = _safe_find_node()
    is_windows = platform.system().lower() == "windows"
    return {
        "schema": "knowledgeradar-runtime-environment/v1",
        "status": "ok" if is_windows else "degraded",
        "platform_policy": {
            "primary": "Windows first",
            "current_os": platform.system(),
            "non_windows": "degraded; future support only",
        },
        "paths": {
            "project_root": _path_state(project_root(), configured_by="KR_PROJECT_ROOT"),
            "state_dir": _path_state(state_dir, configured_by="KR_STATE_DIR", secret=True),
            "log_dir": _path_state(log_dir, configured_by="KR_LOG_DIR", secret=True),
            "task_db": _path_state(task_db, configured_by="KR_TASK_DB_PATH", secret=True),
            "media_cache_dir": _path_state(media_cache, configured_by="KR_MEDIA_CACHE_DIR/KR_RUNTIME_MEDIA_DIR", secret=True),
            "runtime_media_dir": _path_state(runtime_media_dir(), configured_by="KR_RUNTIME_MEDIA_DIR", secret=True),
            "proxy_rule_cache_dir": _path_state(proxy_rule_cache_dir(), configured_by="KR_PROXY_RULE_CACHE_DIR", secret=True),
            "whisper_model_cache_dir": _path_state(whisper_model_cache_dir(), configured_by="KR_WHISPER_MODEL_DIR", secret=True),
            "browser_data_dir": _path_state(browser_dir, configured_by="KR_BROWSER_DATA_DIR", secret=True),
            "playwright_browsers_dir": _path_state(playwright_dir, configured_by="PLAYWRIGHT_BROWSERS_PATH", secret=True),
            "profile_registry": _path_state(registry_path(), configured_by="KR_PROFILE_REGISTRY_PATH", secret=True),
        },
        "executables": {
            "python": {"path": os.environ.get("PYTHONEXECUTABLE", ""), "available": True},
            "node": {"path": node, "available": bool(node), "configured_by": "KR_NODE_EXE"},
            "chrome": {
                "path": chrome,
                "available": bool(chrome),
                "configured_by": "managed_google_chrome_resolver",
                "resolution": managed_chrome_resolution_summary(),
            },
            "ffmpeg": {"path": ffmpeg, "available": bool(ffmpeg), "configured_by": "KR_FFMPEG_EXE/KR_FFMPEG_BIN/runtime-tools/PATH"},
            "yt_dlp_cli": {"path": yt_dlp_available, "available": bool(yt_dlp_available), "configured_by": "PATH"},
        },
        "network": {
            "mcp_host": _env("KR_MCP_HOST", "127.0.0.1"),
            "mcp_port": int(_env("KR_MCP_PORT", "18765")),
            "mcp_transport": _env("KR_MCP_TRANSPORT", "streamable-http"),
            "http_proxy_configured": bool(_env("HTTP_PROXY")),
            "https_proxy_configured": bool(_env("HTTPS_PROXY")),
        },
        "planning_tools": planning_tools_manifest(),
        "proxy_rules": proxy_rules_summary(),
        "task_worker": task_worker_summary(),
        "dependencies": dependency_preflight_summary(),
        "release_boundaries": {
            "exclude": [
                ".env",
                "local/**",
                "browser_data/**",
                "runtime/logs/**",
                "runtime/reports/**",
                "runtime/media_cache/**",
                "runtime/*.sqlite*",
                "config/profile_registry.json",
            ],
            "include_templates_only": [
                "config/*.example",
                "config/profile_registry.example.json",
                "config/package.env.example",
                "config/runtime.env.example",
            ],
        },
    }


def planning_tools_enabled() -> bool:
    return _env("KR_EXPOSE_PLANNING_TOOLS", "false").strip().lower() in {"1", "true", "yes", "on"}


def planning_tools_manifest() -> Dict[str, Any]:
    configured_enabled = planning_tools_enabled()
    return {
        "schema": "knowledgeradar-planning-tools-policy/v1",
        "enabled": False,
        "configured_enabled": configured_enabled,
        "configured_by": "KR_EXPOSE_PLANNING_TOOLS",
        "default": "not_mcp_registered",
        "mode": "legacy_local_helpers_requested" if configured_enabled else "legacy_local_helpers_not_requested",
        "tools": ["expand_keywords", "plan_research"],
        "mcp_registered": False,
        "compatibility": "KR_EXPOSE_PLANNING_TOOLS is retained only as legacy local-helper configuration; it does not publish MCP tools.",
        "canary_requirement": "Compare trace, coverage and report quality between native Agent planning and the registered research tools; do not treat local helper configuration as an MCP tool-count change.",
    }
