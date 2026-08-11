"""Adaptive concurrency planning for generic search provider waves."""

from __future__ import annotations

import os
from typing import Any, Iterable


def _int_env(name: str, default: int, *, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def compute_wave_concurrency(
    wave: Iterable[str],
    *,
    provider_status: dict[str, dict[str, Any]] | None = None,
    profiles: dict[str, Any] | None = None,
    include_raw_content: bool = False,
) -> dict[str, Any]:
    names = [str(name) for name in wave if str(name)]
    hard_cap = _int_env("KR_WEB_SEARCH_MAX_WORKERS", 5, low=1, high=16)
    reasons: list[str] = []
    if not names:
        return {"schema": "knowledgeradar-wave-concurrency/v1", "wave": [], "selected_workers": 1, "hard_cap": hard_cap, "reasons": ["empty_wave"]}

    workers = min(len(names), hard_cap)
    if workers < len(names):
        reasons.append("hard_cap_applied")
    if include_raw_content and workers > 2:
        workers = 2
        reasons.append("raw_content_request_limits_parallelism")

    available = 0
    statuses = provider_status or {}
    for name in names:
        row = statuses.get(name) or {}
        if row and row.get("available") is False:
            continue
        available += 1
    if statuses and available and available < workers:
        workers = available
        reasons.append("unavailable_providers_excluded_from_worker_count")

    paid_like = 0
    for name in names:
        profile = (profiles or {}).get(name) or {}
        text = f"{name} {profile}".lower()
        if any(marker in text for marker in ["paid", "quota", "credit", "tavily", "serpapi"]):
            paid_like += 1
    if paid_like and paid_like == len(names) and workers > 2:
        workers = 2
        reasons.append("quota_sensitive_wave_limited")

    if not reasons:
        reasons.append("wave_size_within_cap")
    return {
        "schema": "knowledgeradar-wave-concurrency/v1",
        "wave": names,
        "selected_workers": max(1, workers),
        "hard_cap": hard_cap,
        "reasons": reasons,
    }
