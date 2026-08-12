"""Safe, minimal updates to the supported product .env file."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / ".env"
KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
ASSIGNMENT_PATTERN = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=")

FIELDS = (
    {"key": "TAVILY_API_KEY", "label": "Tavily", "group": "网页搜索", "kind": "secret"},
    {"key": "ANYSEARCH_API_KEY", "label": "AnySearch", "group": "网页搜索", "kind": "secret"},
    {"key": "BRAVE_SEARCH_API_KEY", "label": "Brave Search", "group": "网页搜索", "kind": "secret"},
    {"key": "EXA_API_KEY", "label": "Exa", "group": "网页搜索", "kind": "secret"},
    {"key": "SEARXNG_BASE_URL", "label": "SearXNG 本地地址", "group": "网页搜索", "kind": "url"},
    {"key": "YOUTUBE_API_KEY", "label": "YouTube Data API", "group": "内容与学术", "kind": "secret"},
    {"key": "OPENALEX_API_KEY", "label": "OpenAlex", "group": "内容与学术", "kind": "secret"},
    {"key": "SEMANTIC_SCHOLAR_API_KEY", "label": "Semantic Scholar", "group": "内容与学术", "kind": "secret"},
    {"key": "DASHSCOPE_API_KEY", "label": "DashScope/百炼", "group": "理解与多模态", "kind": "secret"},
    {"key": "SILICONFLOW_API_KEY", "label": "SiliconFlow", "group": "理解与多模态", "kind": "secret"},
    {"key": "LLM_API_KEY", "label": "兼容 LLM 服务", "group": "理解与多模态", "kind": "secret"},
)
ALLOWED_KEYS = {field["key"] for field in FIELDS}


def _read_values(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if KEY_PATTERN.fullmatch(key):
            values[key] = value.strip().strip('"').strip("'")
    return values


def runtime_env_path() -> Path:
    return Path(__import__("os").environ.get("KR_RUNTIME_ENV_PATH", str(ENV_PATH))).expanduser()


def public_snapshot(path: Path | None = None) -> dict[str, Any]:
    """Return configuration presence only; values never leave the process."""
    path = path or runtime_env_path()
    values = _read_values(path)
    fields = [
        {**field, "configured": bool(values.get(field["key"], "").strip())}
        for field in FIELDS
    ]
    return {"env_exists": path.is_file(), "fields": fields}


def _validated_updates(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("配置内容必须是对象。")
    updates: dict[str, str] = {}
    for key, value in payload.items():
        if key not in ALLOWED_KEYS:
            raise ValueError("包含不支持的配置项。")
        if not isinstance(value, str):
            raise ValueError("配置值必须是文本。")
        value = value.strip()
        if not value:
            continue
        if len(value) > 4096 or any(character in value for character in "\r\n\x00"):
            raise ValueError("配置值格式无效。")
        updates[key] = value
    return updates


def apply_updates(payload: Any, path: Path | None = None) -> list[str]:
    """Merge intentionally supplied nonempty values while preserving all other lines."""
    path = path or runtime_env_path()
    updates = _validated_updates(payload)
    if not updates:
        return []

    original = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    newline = "\r\n" if "\r\n" in original else "\n"
    lines = original.splitlines()
    remaining = set(updates)
    output: list[str] = []
    for line in lines:
        match = ASSIGNMENT_PATTERN.match(line)
        key = match.group(1) if match else ""
        if key in updates:
            output.append(f"{key}={updates[key]}")
            remaining.discard(key)
        else:
            output.append(line)
    if output and output[-1] != "":
        output.append("")
    if remaining:
        output.extend(["# Added by the local KnowledgeRadar setup wizard"])
        output.extend(f"{key}={updates[key]}" for key in sorted(remaining))
    path.write_text(newline.join(output).rstrip() + newline, encoding="utf-8")
    return sorted(updates)
