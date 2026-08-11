"""Read-only Xiaohongshu API candidate configuration summary."""

from __future__ import annotations

import os
from typing import Any, Dict

from .env_loader import load_runtime_env


API_CANDIDATES = [
    {
        "id": "tikhub",
        "env_key": "TIKHUB_API_KEY",
        "base_url_env": "TIKHUB_BASE_URL",
        "default_base_url": "https://api.tikhub.dev",
        "priority": 1,
        "first_probe": "xiaohongshu_search_notes_limit_1",
        "docs": ["https://tikhub.io/pricing", "https://docs.tikhub.io/438852171e0"],
    },
]


def xhs_api_candidate_config_summary() -> Dict[str, Any]:
    """Report API candidate readiness without exposing secrets or calling APIs."""
    load_runtime_env()
    rows = []
    for candidate in API_CANDIDATES:
        env_key = candidate["env_key"]
        base_url_env = candidate["base_url_env"]
        has_key = bool(os.environ.get(env_key, "").strip())
        base_url = os.environ.get(base_url_env, "").strip() or candidate["default_base_url"]
        rows.append(
            {
                "id": candidate["id"],
                "priority": candidate["priority"],
                "key_env": env_key,
                "key_configured": has_key,
                "base_url_env": base_url_env,
                "base_url_configured": bool(os.environ.get(base_url_env, "").strip()),
                "base_url": base_url,
                "first_probe": candidate["first_probe"],
                "probe_permission": "auto_break_glass_when_native_routes_fail" if candidate["id"] == "tikhub" else "manual_confirm_required",
                "docs": candidate["docs"],
                "status": "ready_for_break_glass" if candidate["id"] == "tikhub" and has_key else ("ready_for_manual_minimal_probe" if has_key else "awaiting_api_key"),
            }
        )
    return {
        "schema": "knowledgeradar-xhs-api-candidate-config/v1",
        "status": "ready" if any(row["key_configured"] for row in rows) else "awaiting_api_key",
        "side_effects": {"api_call": False, "billing": False, "secret_exposed": False},
        "candidates": rows,
        "notes": [
            "This summary only checks environment variables; it never emits secret values.",
            "TikHub is a paid break-glass fallback and is hard-limited by daily search/detail counters before live calls.",
            "TikHub is currently the only admitted paid Xiaohongshu API fallback.",
        ],
    }
