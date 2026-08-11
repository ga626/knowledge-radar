"""Search breadth / detail precision helper for Xiaohongshu reports."""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List

XHS_DETAIL_TOP_K_ENV = "KR_XHS_DETAIL_TOP_K"
XHS_DETAIL_RETRY_REPLACEMENTS_ENV = "KR_XHS_DETAIL_RETRY_REPLACEMENTS"
DEFAULT_XHS_DETAIL_TOP_K = 2
DEFAULT_XHS_DETAIL_RETRY_REPLACEMENTS = 1


def xhs_detail_selection_config(
    *,
    top_k: int | None = None,
    retry_replacements: int | None = None,
) -> Dict[str, int]:
    """Resolve detail precision from explicit args or environment defaults."""
    return {
        "top_k": _non_negative_int(top_k, XHS_DETAIL_TOP_K_ENV, DEFAULT_XHS_DETAIL_TOP_K),
        "retry_replacements": _non_negative_int(
            retry_replacements,
            XHS_DETAIL_RETRY_REPLACEMENTS_ENV,
            DEFAULT_XHS_DETAIL_RETRY_REPLACEMENTS,
        ),
    }


def select_xhs_detail_targets(
    search_result: Dict[str, Any],
    *,
    top_k: int | None = None,
    retry_replacements: int | None = None,
) -> List[Dict[str, Any]]:
    """Return ordered detail targets using configurable primary/replacement counts."""
    config = xhs_detail_selection_config(top_k=top_k, retry_replacements=retry_replacements)
    top_k_int = config["top_k"]
    replacement_int = config["retry_replacements"]
    items = search_result.get("items") if isinstance(search_result, dict) else []
    if not isinstance(items, list):
        items = []
    limit = top_k_int + replacement_int
    selected: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        note_id = str(item.get("note_id") or item.get("noteId") or url).strip()
        key = note_id or url
        if not url or not key or key in seen:
            continue
        seen.add(key)
        selected.append(
            {
                "url": url,
                "note_id": note_id,
                "rank": len(selected) + 1,
                "role": "primary" if len(selected) < top_k_int else "replacement",
                "title": str(item.get("title") or "")[:160],
            }
        )
        if len(selected) >= limit:
            break
    return selected


def read_xhs_details_with_replacement(
    search_result: Dict[str, Any],
    detail_reader: Callable[[str], Dict[str, Any]],
    *,
    top_k: int | None = None,
    retry_replacements: int | None = None,
) -> Dict[str, Any]:
    """Read up to top_k successful details, trying configured replacements."""
    config = xhs_detail_selection_config(top_k=top_k, retry_replacements=retry_replacements)
    top_k_int = config["top_k"]
    targets = select_xhs_detail_targets(
        search_result,
        top_k=top_k_int,
        retry_replacements=config["retry_replacements"],
    )
    successes: List[Dict[str, Any]] = []
    attempts: List[Dict[str, Any]] = []
    for target in targets:
        try:
            data = detail_reader(target["url"])
        except Exception as exc:
            data = {"error": str(exc), "failure_type": exc.__class__.__name__}
        ok = isinstance(data, dict) and not data.get("error")
        attempts.append({**target, "status": "ok" if ok else "failed", "failure_type": data.get("failure_type") if isinstance(data, dict) else ""})
        if ok:
            successes.append(data)
        if len(successes) >= top_k_int:
            break
    return {
        "schema": "knowledgeradar-xhs-detail-selection/v1",
        "top_k": top_k_int,
        "retry_replacements": config["retry_replacements"],
        "selected": targets,
        "attempts": attempts,
        "details": successes,
    }


def _non_negative_int(explicit: int | None, env_name: str, default: int) -> int:
    if explicit is not None:
        value = explicit
    else:
        value = os.environ.get(env_name, str(default))
    try:
        return max(0, int(value or 0))
    except Exception:
        return max(0, int(default))
