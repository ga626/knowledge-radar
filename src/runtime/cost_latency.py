"""Cost and latency governance helpers for MCP tool handlers.

This module is intentionally lightweight: it provides stable capability
profiles, request budget defaults, small TTL caches, and runtime metadata
helpers without taking over model-side routing decisions.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import copy
import hashlib
import json
import os
import threading
import time
from typing import Any, Dict, Iterator, Optional


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_key(*parts: Any, length: int = 24) -> str:
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:length]


def estimate_json_bytes(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))
    except Exception:
        return len(str(value).encode("utf-8", errors="ignore"))


@dataclass(frozen=True)
class CapabilityCostProfile:
    capability_id: str
    tool_name: str
    operation: str
    cost_class: str
    latency_class: str
    freshness_class: str
    cache_policy: str
    default_timeout_s: float
    background_after_s: float
    max_sync_wait_s: float
    resource_kind: str = ""
    quota_kind: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "tool_name": self.tool_name,
            "operation": self.operation,
            "cost_class": self.cost_class,
            "latency_class": self.latency_class,
            "freshness_class": self.freshness_class,
            "cache_policy": self.cache_policy,
            "default_timeout_s": self.default_timeout_s,
            "background_after_s": self.background_after_s,
            "max_sync_wait_s": self.max_sync_wait_s,
            "resource_kind": self.resource_kind,
            "quota_kind": self.quota_kind,
            "notes": list(self.notes),
        }


_PROFILES: Dict[str, CapabilityCostProfile] = {
    "health.summary": CapabilityCostProfile(
        capability_id="health.summary",
        tool_name="health_check",
        operation="health_summary",
        cost_class="free_local",
        latency_class="fast",
        freshness_class="short_ttl",
        cache_policy="ttl_memory",
        default_timeout_s=2.0,
        background_after_s=0.0,
        max_sync_wait_s=2.0,
    ),
    "capabilities.agent_summary": CapabilityCostProfile(
        capability_id="capabilities.agent_summary",
        tool_name="get_capabilities",
        operation="capability_summary",
        cost_class="free_local",
        latency_class="fast",
        freshness_class="short_ttl",
        cache_policy="ttl_memory",
        default_timeout_s=2.0,
        background_after_s=0.0,
        max_sync_wait_s=2.0,
    ),
    "github.search": CapabilityCostProfile(
        capability_id="github.search",
        tool_name="search_github_repositories",
        operation="search",
        cost_class="network_free",
        latency_class="medium",
        freshness_class="short_ttl",
        cache_policy="ttl_memory",
        default_timeout_s=10.0,
        background_after_s=0.0,
        max_sync_wait_s=10.0,
        quota_kind="github_rest",
    ),
    "xhs.detail.image_ocr": CapabilityCostProfile(
        capability_id="xhs.detail.image_ocr",
        tool_name="get_content_detail",
        operation="image_ocr",
        cost_class="model_expensive",
        latency_class="background_preferred",
        freshness_class="derived_artifact",
        cache_policy="artifact_cache_by_media_url",
        default_timeout_s=8.0,
        background_after_s=8.0,
        max_sync_wait_s=12.0,
        resource_kind="frame_vision",
        quota_kind="vision_model",
        notes=["May create a background task and return a task reference."],
    ),
}


def capability_cost_profiles() -> Dict[str, Dict[str, Any]]:
    return {key: profile.to_dict() for key, profile in sorted(_PROFILES.items())}


def profile_for(capability_id: str) -> Dict[str, Any]:
    profile = _PROFILES.get(capability_id)
    if profile:
        return profile.to_dict()
    return {
        "capability_id": capability_id,
        "tool_name": "",
        "operation": "",
        "cost_class": "unknown",
        "latency_class": "unknown",
        "freshness_class": "unknown",
        "cache_policy": "none",
        "default_timeout_s": 0,
        "background_after_s": 0,
        "max_sync_wait_s": 0,
        "resource_kind": "",
        "quota_kind": "",
        "notes": [],
    }


_BUDGETS: Dict[str, Dict[str, Any]] = {
    "fast": {
        "mode": "fast",
        "max_wall_time_s": 20,
        "max_sync_wait_s": 4,
        "max_paid_calls": 0,
        "max_background_tasks": 2,
        "allow_stale_cache": True,
        "force_refresh": False,
        "allow_background": True,
        "diagnostic": False,
    },
    "balanced": {
        "mode": "balanced",
        "max_wall_time_s": 60,
        "max_sync_wait_s": 10,
        "max_paid_calls": 3,
        "max_background_tasks": 5,
        "allow_stale_cache": True,
        "force_refresh": False,
        "allow_background": True,
        "diagnostic": False,
    },
    "deep": {
        "mode": "deep",
        "max_wall_time_s": 180,
        "max_sync_wait_s": 20,
        "max_paid_calls": 10,
        "max_background_tasks": 12,
        "allow_stale_cache": True,
        "force_refresh": False,
        "allow_background": True,
        "diagnostic": False,
    },
    "diagnostic": {
        "mode": "diagnostic",
        "max_wall_time_s": 240,
        "max_sync_wait_s": 30,
        "max_paid_calls": 10,
        "max_background_tasks": 12,
        "allow_stale_cache": False,
        "force_refresh": True,
        "allow_background": True,
        "diagnostic": True,
    },
}


def budget_envelope(mode: str | None = None, **overrides: Any) -> Dict[str, Any]:
    selected = str(mode or os.environ.get("KR_DEFAULT_BUDGET_MODE", "balanced")).strip().lower()
    if selected not in _BUDGETS:
        selected = "balanced"
    envelope = dict(_BUDGETS[selected])
    for key, value in overrides.items():
        if value is not None:
            envelope[key] = value
    return envelope


def budget_manifest() -> Dict[str, Any]:
    return {
        "schema": "knowledgeradar-request-budget-envelope/v1",
        "default_mode": os.environ.get("KR_DEFAULT_BUDGET_MODE", "balanced"),
        "modes": copy.deepcopy(_BUDGETS),
        "decision_boundary": "The model chooses tools and depth; KR enforces execution budgets, cache, backgrounding and metadata.",
    }


class TTLCache:
    def __init__(self, name: str, ttl_s: float = 60.0, max_items: int = 128):
        self.name = name
        self.ttl_s = float(ttl_s)
        self.max_items = int(max_items)
        self._lock = threading.RLock()
        self._items: Dict[str, Dict[str, Any]] = {}
        self._stats: Dict[str, Any] = {
            "hits": 0,
            "misses": 0,
            "sets": 0,
            "stale_hits": 0,
            "expired": 0,
            "evictions": 0,
            "estimated_saved_s": 0.0,
            "last_hit_at": "",
            "last_miss_at": "",
            "last_set_at": "",
        }

    def get(self, key: str, *, allow_stale: bool = False) -> Optional[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            row = self._items.get(key)
            if not row:
                self._stats["misses"] += 1
                self._stats["last_miss_at"] = utc_now_iso()
                return None
            age_s = now - float(row.get("created_at", 0))
            stale = age_s > self.ttl_s
            if stale and not allow_stale:
                self._stats["misses"] += 1
                self._stats["expired"] += 1
                self._stats["last_miss_at"] = utc_now_iso()
                self._items.pop(key, None)
                return None
            self._stats["hits"] += 1
            if stale:
                self._stats["stale_hits"] += 1
            self._stats["last_hit_at"] = utc_now_iso()
            self._stats["estimated_saved_s"] = round(
                float(self._stats.get("estimated_saved_s") or 0.0) + float(row.get("compute_elapsed_s") or 0.0),
                3,
            )
            result = copy.deepcopy(row.get("value") or {})
        result.setdefault("runtime", {})
        result["runtime"].setdefault("cache", {})
        result["runtime"]["cache"].update(
            {
                "hit": True,
                "level": "L0",
                "name": self.name,
                "key": key,
                "ttl_s": self.ttl_s,
                "age_s": round(max(0.0, age_s), 3),
                "stale": bool(stale),
                "estimated_saved_s": round(float(row.get("compute_elapsed_s") or 0.0), 3),
            }
        )
        return result

    def set(self, key: str, value: Dict[str, Any], *, compute_elapsed_s: float | None = None) -> Dict[str, Any]:
        now = time.time()
        elapsed = float(compute_elapsed_s if compute_elapsed_s is not None else ((value.get("runtime") or {}).get("elapsed_s") if isinstance(value.get("runtime"), dict) else 0.0) or 0.0)
        with self._lock:
            if len(self._items) >= self.max_items:
                oldest = sorted(self._items.items(), key=lambda item: float(item[1].get("created_at", 0)))[: max(1, self.max_items // 10)]
                for old_key, _row in oldest:
                    self._items.pop(old_key, None)
                    self._stats["evictions"] += 1
            self._items[key] = {"created_at": now, "value": copy.deepcopy(value), "compute_elapsed_s": elapsed}
            self._stats["sets"] += 1
            self._stats["last_set_at"] = utc_now_iso()
        return {
            "hit": False,
            "level": "L0",
            "name": self.name,
            "key": key,
            "ttl_s": self.ttl_s,
            "age_s": 0,
            "stale": False,
            "compute_elapsed_s": round(elapsed, 3),
        }

    def summary(self) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            live = {key: row for key, row in self._items.items() if now - float(row.get("created_at", 0)) <= self.ttl_s}
            self._items = live
            hits = int(self._stats.get("hits") or 0)
            misses = int(self._stats.get("misses") or 0)
            total = hits + misses
            return {
                "schema": "knowledgeradar-ttl-cache-summary/v1",
                "name": self.name,
                "ttl_s": self.ttl_s,
                "size": len(self._items),
                "max_items": self.max_items,
                "stats": {
                    **self._stats,
                    "hit_rate": round(hits / total, 4) if total else None,
                    "estimated_saved_s": round(float(self._stats.get("estimated_saved_s") or 0.0), 3),
                },
            }


_CACHES: Dict[str, TTLCache] = {}
_CACHES_LOCK = threading.RLock()


def get_ttl_cache(name: str, *, ttl_s: float = 60.0, max_items: int = 128) -> TTLCache:
    with _CACHES_LOCK:
        cache = _CACHES.get(name)
        if cache is None:
            cache = TTLCache(name, ttl_s=ttl_s, max_items=max_items)
            _CACHES[name] = cache
        return cache


def cache_registry_summary() -> Dict[str, Any]:
    with _CACHES_LOCK:
        caches = {name: cache.summary() for name, cache in sorted(_CACHES.items())}
    return {
        "schema": "knowledgeradar-cache-registry-summary/v1",
        "caches": caches,
    }


def attach_runtime_metadata(
    result: Dict[str, Any],
    *,
    tool_name: str,
    capability_id: str,
    started: float,
    budget: Optional[Dict[str, Any]] = None,
    cache: Optional[Dict[str, Any]] = None,
    warnings: Optional[list[str]] = None,
    deferred_tasks: Optional[list[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    data = dict(result or {})
    profile = profile_for(capability_id)
    runtime = dict(data.get("runtime") or {})
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    elapsed_s = round(max(0.0, time.time() - started), 3)
    serialize_started = time.time()
    payload_bytes = estimate_json_bytes(data)
    serialize_elapsed_ms = round((time.time() - serialize_started) * 1000, 3)
    runtime.update(
        {
            "schema": "knowledgeradar-runtime-metadata/v1",
            "tool_name": tool_name,
            "capability_id": capability_id,
            "cost_class": profile.get("cost_class", "unknown"),
            "latency_class": profile.get("latency_class", "unknown"),
            "budget": budget or budget_envelope(),
            "elapsed_s": elapsed_s,
            "server_elapsed_s": elapsed_s,
            "handler_elapsed_s": elapsed_s,
            "serialize_elapsed_ms": serialize_elapsed_ms,
            "payload_bytes": payload_bytes,
            "generated_at": utc_now_iso(),
        }
    )
    if cache is not None:
        runtime["cache"] = cache
    else:
        runtime.setdefault("cache", {"hit": False})
    if warnings:
        runtime["warnings"] = warnings
    if deferred_tasks is not None:
        runtime["deferred_tasks"] = deferred_tasks
    metadata.setdefault("runtime", runtime)
    metadata.setdefault("cost_class", runtime["cost_class"])
    metadata.setdefault("latency_class", runtime["latency_class"])
    data["metadata"] = metadata
    data["runtime"] = runtime
    return data


@contextmanager
def governed_call(tool_name: str, capability_id: str, *, mode: str | None = None, **budget_overrides: Any) -> Iterator[Dict[str, Any]]:
    started = time.time()
    envelope = budget_envelope(mode, **budget_overrides)
    yield {"started": started, "budget": envelope, "profile": profile_for(capability_id)}
