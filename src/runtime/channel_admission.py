"""Candidate channel admission gates.

This module is intentionally read-only. It summarizes whether a candidate has
enough evidence for safe_auto account switching; execution is still delegated
to the account switcher and Chrome manager.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List


def build_channel_admission_summary(profiles: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    channels: List[Dict[str, Any]] = []
    for profile in profiles or []:
        if not isinstance(profile, dict):
            continue
        profile_id = str(profile.get("profile_id") or "")
        status = str(profile.get("status") or "")
        role = str(profile.get("role") or "")
        channel_id = str(profile.get("channel_id") or "")
        runtime_state = str(profile.get("runtime_state") or "")
        main_chain_allowed = bool(profile.get("main_chain_allowed", False)) if "main_chain_allowed" in profile else role == "primary"
        notes = [str(item) for item in profile.get("notes", []) or []]
        gate = _admission_gate(
            profile_id=profile_id,
            role=role,
            status=status,
            runtime_state=runtime_state,
            notes=notes,
            main_chain_allowed=main_chain_allowed,
        )
        channels.append(
            {
                "profile_id": profile_id,
                "channel_id": channel_id,
                "role": role,
                "status": status,
                **gate,
            }
        )
    return {
        "schema": "knowledgeradar-channel-admission-summary/v1",
        "status": "ok",
        "production_routing": "safe_auto_for_readonly",
        "auto_admission": "safe_auto_enabled",
        "channels": channels,
        "notes": [
            "admission gates are read-only summaries; account switcher executes eligible safe_auto switches",
            "readonly search/detail/main_chain can switch profiles when registry policy allows it",
        ],
    }


def _admission_gate(*, profile_id: str, role: str, status: str, runtime_state: str, notes: List[str], main_chain_allowed: bool = False) -> Dict[str, Any]:
    joined = " ".join([status, runtime_state, *notes]).lower()
    requirements = {
        "profile_registered": bool(profile_id),
        "login_persistence_ok": _has_any(
            joined,
            [
                "login persistence",
                "login_persistence",
                "restart persistence",
                "cookie persistence",
                "required cookies present",
                "available",
            ],
        ),
        "search_minimum_ok": _has_any(joined, ["search", "candidate_bundled_search", "three low-frequency searches"]),
        "detail_selectors_ok": _has_any(joined, ["detail selectors", "detail_content_selectors", "detail selector"]),
        "runtime_state_wired": _has_any(joined, ["runtime state", "profile-state", "healthy", "available"])
        or profile_id == "xhs-p7-playwright",
        "main_chain_allowed": bool(main_chain_allowed),
    }
    if main_chain_allowed:
        gate = "primary_active"
        missing: List[str] = []
    else:
        missing = [name for name, ok in requirements.items() if name != "main_chain_allowed" and not ok]
        gate = "explicit_probe_ready" if not missing else "candidate_observe"
    if "abandoned" in joined or "disabled" in joined:
        gate = "rejected"
    return {
        "admission_gate": gate,
        "requirements": requirements,
        "missing": missing,
        "main_chain_admission": "allowed_primary_only" if main_chain_allowed else "denied",
    }


def _has_any(text: str, needles: List[str]) -> bool:
    return any(needle in text for needle in needles)
