"""Candidate channel admission gates.

This module is intentionally read-only. It summarizes whether a candidate has
enough evidence for safe_auto account switching; execution is still delegated
to the account switcher and Chrome manager.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List


def current_browser_diagnostics(registry, *, opencli, native):
    """诊断只展示现行通道；历史账号池不再获得主链准入声明。"""
    borrowed = {'xiaohongshu': 'opencli', 'xhs': 'opencli', 'boss': 'opencli',
                'zhihu': 'browser_link', 'liepin': 'browser_link', 'maimai': 'browser_link'}
    active = [dict(row) for row in registry.get('profiles', []) if row.get('platform') not in borrowed]
    result = {**registry, 'profiles': active, 'retired_profile_count': len(registry.get('profiles', [])) - len(active),
              'policy': {'routing': 'platform_adapter_owned', 'borrowed_account_switching': 'disabled'}}
    result['counts'] = {'total': len(active), 'available': sum(row.get('profile_exists') is True for row in active),
                        'missing': sum(row.get('profile_exists') is not True for row in active)}
    channels = []
    for platform in ('xiaohongshu', 'boss', 'zhihu', 'liepin', 'maimai'):
        driver = borrowed[platform]
        connected = opencli.get('connected') is True if driver == 'opencli' else native.get('ok') is True
        channels.append({'platform': platform, 'driver': driver, 'ownership': 'borrowed', 'connected': connected,
                         'auth_state': 'not_probed', 'main_chain_allowed': False,
                         'admission_gate': 'requires_platform_request' if connected else 'channel_not_ready',
                         'reason': opencli.get('connection_reason') if driver == 'opencli' else native.get('code', ''),
                         'automatic_account_switching': False})
    return result, {'schema': 'knowledgeradar-channel-admission-summary/v2', 'status': 'ok',
                    'production_routing': 'platform_adapter_owned', 'auto_admission': 'disabled', 'channels': channels}


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
