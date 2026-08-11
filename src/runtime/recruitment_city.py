"""Recruitment platform city parameter registry."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY = ROOT / "config" / "recruitment_city_registry.json"


def _registry_path() -> Path:
    configured = os.environ.get("KR_RECRUITMENT_CITY_REGISTRY", "").strip()
    return Path(configured) if configured else DEFAULT_REGISTRY


def load_city_registry() -> dict[str, Any]:
    try:
        payload = json.loads(_registry_path().read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _normalize_city(city: str) -> str:
    value = str(city or "").strip()
    if value.endswith("市") and len(value) > 1:
        value = value[:-1]
    return value


def resolve_recruitment_city(platform: str, city: str) -> dict[str, Any]:
    """Resolve a user city to the platform-specific query parameter.

    Unknown cities are explicit. Platforms that can safely accept the user's
    city text declare passthrough_when_missing=true in the registry.
    """
    platform_key = str(platform or "").strip().lower()
    requested = str(city or "").strip()
    normalized = _normalize_city(requested)
    registry = load_city_registry()
    platform_cfg = (registry.get("platforms") or {}).get(platform_key) or {}
    cities = registry.get("cities") if isinstance(registry.get("cities"), dict) else {}
    matched_name = ""
    city_cfg: dict[str, Any] = {}
    for name, cfg in cities.items():
        if not isinstance(cfg, dict):
            continue
        aliases = [str(item) for item in cfg.get("aliases") or []]
        candidates = {str(name), _normalize_city(str(name)), *aliases, *[_normalize_city(item) for item in aliases]}
        if normalized in candidates or requested in candidates:
            matched_name = str(name)
            city_cfg = cfg
            break
    params = city_cfg.get("platform_params") if isinstance(city_cfg.get("platform_params"), dict) else {}
    param_sources = city_cfg.get("platform_param_sources") if isinstance(city_cfg.get("platform_param_sources"), dict) else {}
    source = param_sources.get(platform_key) if isinstance(param_sources.get(platform_key), dict) else {}
    value = str(params.get(platform_key) or "").strip()
    passthrough = bool(platform_cfg.get("passthrough_when_missing"))
    if value:
        status = "mapped"
        query_value = value
    elif requested and passthrough:
        status = "passthrough"
        query_value = requested
    elif requested:
        status = "missing"
        query_value = ""
    else:
        status = "not_requested"
        query_value = ""
    param_name = str(platform_cfg.get("param_name") or "")
    configured_names = platform_cfg.get("param_names")
    query_param_names = [str(item) for item in configured_names if str(item).strip()] if isinstance(configured_names, list) else []
    if not query_param_names and param_name:
        query_param_names = [param_name]
    return {
        "schema": "knowledgeradar-recruitment-city-resolution/v1",
        "platform": platform_key,
        "requested_city": requested,
        "normalized_city": normalized,
        "matched_city": matched_name,
        "param_name": param_name,
        "query_param_names": query_param_names,
        "param_value": query_value,
        "value_kind": str(platform_cfg.get("value_kind") or ""),
        "source": str(platform_cfg.get("source") or ""),
        "source_url": str(source.get("url") or ""),
        "source_field": str(source.get("field") or ""),
        "verified_at": str(source.get("verified_at") or ""),
        "status": status,
        "passthrough_when_missing": passthrough,
        "registry_path": str(_registry_path()),
    }


def city_param_for_platform(platform: str, city: str) -> str:
    return str(resolve_recruitment_city(platform, city).get("param_value") or "")
