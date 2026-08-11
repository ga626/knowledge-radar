"""Managed Chrome/CDP lifecycle helpers.

This module owns browser process startup, CDP endpoint discovery, and cleanup.
It intentionally does not know about platform search/detail logic.
"""

from __future__ import annotations

import atexit
from contextlib import contextmanager
import json
import logging
import os
import re
import subprocess
import threading
import time
from typing import Dict, Optional

import httpx

from .executables import managed_chrome_resolution_summary, resolve_managed_chrome
from .process import silent_subprocess_popen, silent_subprocess_run
from .profile_registry import account_pool_selection_summary, profile_registry_internal, raw_registry_for_platform, select_main_chain_profile
from .xhs_account_identity import identity_for_profile
from .xhs_account_events import record_xhs_account_event
from .browser_sessions import (
    browser_sessions_summary,
    manual_action_request_from_session,
    record_browser_event,
    set_browser_session_deadline,
    transition_browser_session,
    upsert_browser_session,
)
from .leases import default_owner, get_runtime_lease_coordinator

SRC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(SRC_ROOT)

log = logging.getLogger("mcp-server")

MCP_TRANSPORT = os.environ.get("KR_MCP_TRANSPORT", "streamable-http").strip().lower()

# Dedicated runtime ports. XHS and Zhihu must not share one CDP port because
# OpenClaw may call both tools concurrently with different persistent profiles.
XHS_CHROME_DEBUG_PORT = os.environ.get("KR_XHS_CHROME_DEBUG_PORT") or os.environ.get("KR_CHROME_DEBUG_PORT", "12733")
ZHIHU_CHROME_DEBUG_PORT = os.environ.get("KR_ZHIHU_CHROME_DEBUG_PORT", "12734")
CHROME_DEBUG_PORT = XHS_CHROME_DEBUG_PORT
CHROME_DEBUG_URL = f"http://127.0.0.1:{CHROME_DEBUG_PORT}"
BROWSER_DATA_ROOT = os.path.join(REPO_ROOT, "browser_data", "browser_data")
XHS_USER_DATA_DIR = os.path.join(BROWSER_DATA_ROOT, "xhs_user_data_dir")
ZHIHU_USER_DATA_DIR = os.environ.get(
    "KR_ZHIHU_USER_DATA_DIR",
    os.path.join(REPO_ROOT, "browser_data", "profiles", "zhihu", "account_a"),
)
MANAGED_CHROME_USER_DATA_DIR = os.environ.get("KR_CHROME_USER_DATA_DIR", "")
XHS_CHROME_USER_DATA_DIR = os.environ.get("KR_XHS_CHROME_USER_DATA_DIR") or os.environ.get("KR_CHROME_USER_DATA_DIR_XHS", "")
ZHIHU_CHROME_USER_DATA_DIR = os.environ.get("KR_ZHIHU_CHROME_USER_DATA_DIR") or os.environ.get("KR_CHROME_USER_DATA_DIR_ZHIHU", "")
MANAGED_CHROME_PROFILE_DIRECTORY = os.environ.get("KR_CHROME_PROFILE_DIRECTORY", "Default")
XHS_CHROME_PROFILE_DIRECTORY = os.environ.get("KR_XHS_CHROME_PROFILE_DIRECTORY") or os.environ.get("KR_CHROME_PROFILE_DIRECTORY_XHS", "")
ZHIHU_CHROME_PROFILE_DIRECTORY = os.environ.get("KR_ZHIHU_CHROME_PROFILE_DIRECTORY") or os.environ.get("KR_CHROME_PROFILE_DIRECTORY_ZHIHU", "")
XHS_STARTUP_URL = os.environ.get("KR_XHS_STARTUP_URL", "https://www.xiaohongshu.com/explore")
ZHIHU_STARTUP_URL = os.environ.get("KR_ZHIHU_STARTUP_URL", "https://www.zhihu.com")

# BOSS直聘专用配置（端口 12737，隔离试验）
BOSS_CHROME_DEBUG_PORT = os.environ.get("KR_BOSS_CHROME_DEBUG_PORT", "12737")
BOSS_USER_DATA_DIR = os.path.join(BROWSER_DATA_ROOT, "boss_user_data_dir")
BOSS_CHROME_USER_DATA_DIR = os.environ.get("KR_BOSS_CHROME_USER_DATA_DIR") or os.environ.get("KR_CHROME_USER_DATA_DIR_BOSS", "")
BOSS_CHROME_PROFILE_DIRECTORY = os.environ.get("KR_BOSS_CHROME_PROFILE_DIRECTORY") or os.environ.get("KR_CHROME_PROFILE_DIRECTORY_BOSS", "")
BOSS_STARTUP_URL = os.environ.get("KR_BOSS_STARTUP_URL", "https://www.zhipin.com/web/geek/jobs?city=101210100")
BOSS_STEALTH_JS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stealth.js")

# 猎聘专用配置（端口 12738）
LIEPIN_CHROME_DEBUG_PORT = os.environ.get("KR_LIEPIN_CHROME_DEBUG_PORT", "12738")
LIEPIN_USER_DATA_DIR = os.path.join(BROWSER_DATA_ROOT, "liepin_user_data_dir")
LIEPIN_CHROME_USER_DATA_DIR = os.environ.get("KR_LIEPIN_CHROME_USER_DATA_DIR") or os.environ.get("KR_CHROME_USER_DATA_DIR_LIEPIN", "")
LIEPIN_CHROME_PROFILE_DIRECTORY = os.environ.get("KR_LIEPIN_CHROME_PROFILE_DIRECTORY") or os.environ.get("KR_CHROME_PROFILE_DIRECTORY_LIEPIN", "")
LIEPIN_STARTUP_URL = os.environ.get("KR_LIEPIN_STARTUP_URL", "https://www.liepin.com/zhaopin/?key=Python")

# 智联招聘专用配置（端口 12749）
ZHILIAN_CHROME_DEBUG_PORT = os.environ.get("KR_ZHILIAN_CHROME_DEBUG_PORT", "12749")
ZHILIAN_USER_DATA_DIR = os.path.join(BROWSER_DATA_ROOT, "zhilian_user_data_dir")
ZHILIAN_CHROME_USER_DATA_DIR = os.environ.get("KR_ZHILIAN_CHROME_USER_DATA_DIR") or os.environ.get("KR_CHROME_USER_DATA_DIR_ZHILIAN", "")
ZHILIAN_CHROME_PROFILE_DIRECTORY = os.environ.get("KR_ZHILIAN_CHROME_PROFILE_DIRECTORY") or os.environ.get("KR_CHROME_PROFILE_DIRECTORY_ZHILIAN", "")
ZHILIAN_STARTUP_URL = os.environ.get("KR_ZHILIAN_STARTUP_URL", "https://sou.zhaopin.com/")

# 脉脉专用配置（端口 12739）
MAIMAI_CHROME_DEBUG_PORT = os.environ.get("KR_MAIMAI_CHROME_DEBUG_PORT", "12739")
MAIMAI_USER_DATA_DIR = os.path.join(BROWSER_DATA_ROOT, "maimai_user_data_dir")
MAIMAI_CHROME_USER_DATA_DIR = os.environ.get("KR_MAIMAI_CHROME_USER_DATA_DIR") or os.environ.get("KR_CHROME_USER_DATA_DIR_MAIMAI", "")
MAIMAI_CHROME_PROFILE_DIRECTORY = os.environ.get("KR_MAIMAI_CHROME_PROFILE_DIRECTORY") or os.environ.get("KR_CHROME_PROFILE_DIRECTORY_MAIMAI", "")
MAIMAI_STARTUP_URL = os.environ.get("KR_MAIMAI_STARTUP_URL", "https://maimai.cn/")

# CNKI 授权浏览器专用配置（端口 12740）。只用于用户授权的只读元数据/引用导出流程。
CNKI_CHROME_DEBUG_PORT = os.environ.get("KR_CNKI_CHROME_DEBUG_PORT", "12740")
CNKI_USER_DATA_DIR = os.path.join(BROWSER_DATA_ROOT, "cnki_user_data_dir")
CNKI_CHROME_USER_DATA_DIR = os.environ.get("KR_CNKI_CHROME_USER_DATA_DIR") or os.environ.get("KR_CHROME_USER_DATA_DIR_CNKI", "")
CNKI_CHROME_PROFILE_DIRECTORY = os.environ.get("KR_CNKI_CHROME_PROFILE_DIRECTORY") or os.environ.get("KR_CHROME_PROFILE_DIRECTORY_CNKI", "")
CNKI_STARTUP_URL = os.environ.get("KR_CNKI_STARTUP_URL", "https://kns.cnki.net/kns8s/search")

_MANAGED_CHROME_PROCS: Dict[str, subprocess.Popen] = {}
_MANAGED_CHROME_PROFILE_DIRS: Dict[str, str] = {}
_MANAGED_CHROME_PIDS: Dict[str, int] = {}
_MANAGED_CHROME_OWNED_PIDS: set[int] = set()
_CHROME_PLATFORM_LOCKS: Dict[str, threading.RLock] = {}
_CHROME_IDLE_TIMERS: Dict[str, threading.Timer] = {}
_CHROME_IDLE_SECONDS = int(os.environ.get("KR_CHROME_IDLE_SECONDS", "120"))
_CHROME_CLOSE_AFTER_OPERATION = os.environ.get("KR_CHROME_CLOSE_AFTER_OPERATION", "0").strip().lower() not in {"0", "false", "no", "off"}
_CHROME_KEEP_ALIVE: Dict[str, bool] = {}  # 当需要用户扫码时，暂停空闲超时
_CHROME_ACTIVE_OPERATIONS: Dict[str, int] = {}
_CHROME_MANUAL_INTERACTION_LEASES: Dict[str, str] = {}
_CHROME_MANUAL_INTERACTION_EXPIRY_TIMERS: Dict[str, threading.Timer] = {}
_CHROME_SILENT_OFFSCREEN = os.environ.get("KR_CHROME_SILENT_OFFSCREEN", "1").strip().lower() not in {"0", "false", "no", "off"}
_CHROME_SILENT_WINDOW_POSITION = os.environ.get("KR_CHROME_SILENT_WINDOW_POSITION", "-32000,-32000")
_CHROME_VISIBLE_WINDOW_POSITION = os.environ.get("KR_CHROME_VISIBLE_WINDOW_POSITION", "80,80")
_CHROME_WINDOW_SIZE = os.environ.get("KR_CHROME_WINDOW_SIZE", "1280,900")
STEALTH_CHROME_PLATFORMS = {
    platform.strip().lower()
    for platform in os.environ.get("KR_STEALTH_CHROME_PLATFORMS", "").split(",")
    if platform.strip()
}
CORE_MANAGED_BROWSER_PLATFORMS = ("xhs", "zhihu", "boss", "liepin", "zhilian", "maimai", "cnki")
MANUAL_BROWSER_DEFAULT_PORTS = {
    "vip_oa": "12741",
    "coaj": "12742",
    "ucdrs": "12743",
    "calis_thesis": "12744",
    "nstrs": "12745",
    "pubscholar": "12746",
    "socolar": "12747",
}
MANUAL_BROWSER_STARTUP_URLS = {
    "vip_oa": "https://www.cqvip.com/search?k=ai",
    "coaj": "https://www.coaj.cn/login",
    "ucdrs": "http://www.ucdrs.superlib.net/",
    "calis_thesis": "https://etd2.calis.edu.cn/",
    "nstrs": "https://www.nstrs.cn/cas",
    "pubscholar": "https://pubscholar.cn/",
    "socolar": "https://www.socolar.com/",
}
MANAGED_BROWSER_PLATFORMS = CORE_MANAGED_BROWSER_PLATFORMS

MANUAL_BROWSER_AUTH_RULES = {
    "liepin": {
        "required_cookies": ("liepin_login_valid", "lt_auth"),
        "required_local_storage_keys": ("se_fe_c_user_info",),
        "cookie_domain_markers": ("liepin.com",),
        "auth_state": "authenticated_with_liepin_session",
    },
    "vip_oa": {
        "required_cookies": ("journalOA-token",),
        "cookie_domain_markers": ("cqvip.com", "fanyu.com"),
        "auth_state": "authenticated_with_journal_oa_token",
    },
    "coaj": {
        "required_cookies": (),
        "auth_state": "login_probe_not_configured",
    },
    "ucdrs": {
        "required_cookies": (),
        "auth_state": "login_probe_not_configured",
    },
    "calis_thesis": {
        "required_cookies": (),
        "auth_state": "login_probe_not_configured",
    },
    "nstrs": {
        "required_cookies": (),
        "auth_state": "login_probe_not_configured",
    },
    "pubscholar": {
        "required_cookies": (),
        "required_local_storage_keys": ("x-finger", "client_finger"),
        "auth_state": "browser_fingerprint_session_present",
    },
    "socolar": {
        "required_cookies": (),
        "required_local_storage_keys": ("token", "refreshToken"),
        "auth_state": "authenticated_with_socolar_token",
    },
}


def _parse_window_pair(value: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        left, top = str(value or "").split(",", 1)
        return int(left.strip()), int(top.strip())
    except Exception:
        return default


def _parse_window_size(value: str, default: tuple[int, int] = (1280, 900)) -> tuple[int, int]:
    return _parse_window_pair(value, default)


def _chrome_debug_port(platform: str = "xhs") -> str:
    platform = str(platform or "xhs").strip().lower()
    base_platform, profile_id = _split_browser_resource(platform)
    if base_platform == "xhs" and profile_id:
        # A separate loopback port is the boundary that makes the three
        # persistent XHS profiles genuinely independent managed browsers.  The
        # registry can override these defaults without placing credentials in
        # source control. B keeps the legacy port for a non-disruptive cutover.
        for row in raw_registry_for_platform("xiaohongshu").get("profiles", []) or []:
            if isinstance(row, dict) and str(row.get("profile_id") or "") == profile_id:
                configured = str(row.get("debug_port") or "").strip()
                if configured:
                    return configured
                slot = str(row.get("account_slot") or "").lower()
                if slot.endswith("_a"):
                    return os.environ.get("KR_XHS_ACCOUNT_A_CHROME_DEBUG_PORT", "12735")
                if slot.endswith("_c"):
                    return os.environ.get("KR_XHS_ACCOUNT_C_CHROME_DEBUG_PORT", "12736")
                return os.environ.get("KR_XHS_ACCOUNT_B_CHROME_DEBUG_PORT", XHS_CHROME_DEBUG_PORT)
        return XHS_CHROME_DEBUG_PORT
    platform = base_platform
    if platform == "zhihu":
        return ZHIHU_CHROME_DEBUG_PORT
    if platform == "boss":
        return BOSS_CHROME_DEBUG_PORT
    if platform == "liepin":
        return LIEPIN_CHROME_DEBUG_PORT
    if platform == "zhilian":
        return ZHILIAN_CHROME_DEBUG_PORT
    if platform == "maimai":
        return MAIMAI_CHROME_DEBUG_PORT
    if platform == "cnki":
        return CNKI_CHROME_DEBUG_PORT
    env_key = f"KR_{platform.upper()}_CHROME_DEBUG_PORT"
    if os.environ.get(env_key):
        return str(os.environ[env_key])
    if platform in MANUAL_BROWSER_DEFAULT_PORTS:
        return MANUAL_BROWSER_DEFAULT_PORTS[platform]
    return XHS_CHROME_DEBUG_PORT


def _registry_platform(platform: str) -> str:
    base_platform, _ = _split_browser_resource(platform)
    return {"xhs": "xiaohongshu"}.get(base_platform, base_platform)


def _browser_platform(platform: str) -> str:
    raw = str(platform or "").strip().lower()
    base, profile_id = _split_browser_resource(raw)
    normalized = {"xiaohongshu": "xhs"}.get(base, base)
    return f"{normalized}:{profile_id}" if profile_id else normalized


def _split_browser_resource(platform: str) -> tuple[str, str]:
    """Return the base platform and an optional profile-scoped resource id."""
    raw = str(platform or "").strip().lower()
    if ":" not in raw:
        return raw, ""
    base, profile_id = raw.split(":", 1)
    return base, profile_id


def _active_xhs_profile_resources() -> tuple[str, ...]:
    """Return currently live profile-scoped XHS resource keys.

    A bare ``xhs`` call is only safe to alias when exactly one profile-scoped
    browser is active.  The in-memory maps are the authoritative ownership
    boundary for the current server process; they avoid selecting an account
    from registry order or from a stale persisted session.
    """
    keys: set[str] = set()
    for mapping in (
        _MANAGED_CHROME_PROCS,
        _MANAGED_CHROME_PROFILE_DIRS,
        _MANAGED_CHROME_PIDS,
        _CHROME_IDLE_TIMERS,
        _CHROME_KEEP_ALIVE,
        _CHROME_ACTIVE_OPERATIONS,
        _CHROME_MANUAL_INTERACTION_LEASES,
    ):
        for raw_key in mapping:
            normalized = _browser_platform(raw_key)
            base, profile_id = _split_browser_resource(normalized)
            if base == "xhs" and profile_id:
                keys.add(f"xhs:{profile_id}")
    return tuple(sorted(keys))


def _browser_resource_key(platform: str, target_profile_id: str = "") -> str:
    normalized = _browser_platform(platform)
    base, embedded_profile_id = _split_browser_resource(normalized)
    if base == "xhs" and (target_profile_id or embedded_profile_id):
        return f"xhs:{target_profile_id or embedded_profile_id}"
    if base == "xhs" and not target_profile_id:
        active = _active_xhs_profile_resources()
        if len(active) == 1:
            # Auxiliary XHS helpers historically used the bare platform name.
            # Once a single account-scoped browser is live, bind those helpers
            # to it so they cannot create a second timer/cleanup owner for the
            # same CDP port.  With multiple accounts we deliberately refuse to
            # guess and retain the legacy key for the caller's explicit guard.
            return active[0]
    return normalized


def _platform_env_name(platform: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(platform or "").upper()).strip("_")


def _resolve_registry_profile_dir(profile_dir: str) -> str:
    if not profile_dir:
        return ""
    expanded = os.path.expandvars(os.path.expanduser(str(profile_dir)))
    if os.path.isabs(expanded):
        return os.path.abspath(expanded)
    return os.path.abspath(os.path.join(REPO_ROOT, expanded))


def _manual_profile_row_for_platform(platform: str) -> Dict:
    rows = raw_registry_for_platform(platform).get("profiles") or []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("channel_id") or "") == "authorized_browser_profile":
            return row
        if str(row.get("browser_base") or "") in {"chrome_manual", "authorized_browser"}:
            return row
        if str(row.get("role") or "") == "manual_candidate":
            return row
    return {}


def _registered_browser_platforms() -> tuple[str, ...]:
    platforms: list[str] = [*CORE_MANAGED_BROWSER_PLATFORMS, *MANUAL_BROWSER_DEFAULT_PORTS]
    try:
        raw = (profile_registry_internal().get("raw") or {}).get("profiles") or []
        for row in raw:
            if not isinstance(row, dict):
                continue
            platform = _browser_platform(str(row.get("platform") or ""))
            if not platform:
                continue
            is_browser_profile = bool(row.get("main_chain_allowed", False)) or str(row.get("channel_id") or "") == "authorized_browser_profile"
            is_browser_profile = is_browser_profile or str(row.get("browser_base") or "") in {"chrome_manual", "authorized_browser"}
            is_browser_profile = is_browser_profile or str(row.get("role") or "") == "manual_candidate"
            if is_browser_profile and platform not in platforms:
                platforms.append(platform)
    except Exception as exc:
        log.debug(f"读取 registry 浏览器平台失败: {exc}")
    return tuple(platforms)


def managed_browser_platforms() -> tuple[str, ...]:
    return _registered_browser_platforms()


def _startup_url_for_platform(platform: str) -> str:
    platform, _ = _split_browser_resource(platform)
    env_key = f"KR_{_platform_env_name(platform)}_STARTUP_URL"
    if os.environ.get(env_key):
        return os.environ[env_key]
    return {
        "xhs": XHS_STARTUP_URL,
        "zhihu": ZHIHU_STARTUP_URL,
        "boss": BOSS_STARTUP_URL,
        "liepin": LIEPIN_STARTUP_URL,
        "zhilian": ZHILIAN_STARTUP_URL,
        "maimai": MAIMAI_STARTUP_URL,
        "cnki": CNKI_STARTUP_URL,
        **MANUAL_BROWSER_STARTUP_URLS,
    }.get(platform, XHS_STARTUP_URL)


def _profile_metadata_for_platform(
    platform: str,
    *,
    profile_dir: str = "",
    target_profile_id: str = "",
) -> Dict[str, str]:
    """Resolve metadata from the actual/explicit profile before policy fallback."""
    registry_platform = _registry_platform(platform)
    resolved_dir = os.path.abspath(profile_dir).lower() if profile_dir else ""
    for row in raw_registry_for_platform(registry_platform).get("profiles", []) or []:
        if not isinstance(row, dict):
            continue
        profile_id = str(row.get("profile_id") or "")
        configured_dir = str(row.get("profile_dir") or "")
        configured_dir = os.path.abspath(os.path.expandvars(os.path.expanduser(configured_dir))).lower() if configured_dir else ""
        if target_profile_id and profile_id != str(target_profile_id):
            continue
        if resolved_dir and configured_dir != resolved_dir:
            continue
        if not target_profile_id and not resolved_dir:
            continue
        identity = identity_for_profile(profile_id) if registry_platform == "xiaohongshu" else {}
        return {
            "profile_id": profile_id,
            "account_slot": str(row.get("account_slot") or ""),
            "channel_id": str(row.get("channel_id") or ""),
            "display_label": str(identity.get("display_label") or ""),
            "masked_hint": str(identity.get("masked_hint") or ""),
        }
    admitted = select_main_chain_profile(registry_platform)
    if admitted.get("status") == "ok":
        return {
            "profile_id": str(admitted.get("profile_id") or ""),
            "account_slot": str(admitted.get("account_slot") or ""),
            "channel_id": str(admitted.get("channel_id") or ""),
        }
    manual = _manual_profile_row_for_platform(_registry_platform(platform))
    if manual:
        return {
            "profile_id": str(manual.get("profile_id") or ""),
            "account_slot": str(manual.get("account_slot") or ""),
            "channel_id": str(manual.get("channel_id") or ""),
        }
    return {}


def _chrome_debug_url(platform: str = "xhs") -> str:
    return f"http://127.0.0.1:{_chrome_debug_port(platform)}"


def _chrome_platform_lock(platform: str) -> threading.RLock:
    lock = _CHROME_PLATFORM_LOCKS.get(platform)
    if lock is None:
        lock = threading.RLock()
        _CHROME_PLATFORM_LOCKS[platform] = lock
    return lock


def _managed_chrome_profile_dir(platform: str = "xhs", *, target_profile_id: str = "") -> str:
    """Return the persistent Chrome profile for the requested platform."""
    platform, embedded_profile_id = _split_browser_resource(platform)
    target_profile_id = target_profile_id or embedded_profile_id
    if platform == "xhs" and target_profile_id:
        for row in raw_registry_for_platform("xiaohongshu").get("profiles", []) or []:
            if not isinstance(row, dict) or str(row.get("profile_id") or "") != str(target_profile_id):
                continue
            profile_dir = _resolve_registry_profile_dir(str(row.get("profile_dir") or ""))
            if profile_dir:
                return profile_dir
        return ""
    env_specific = os.environ.get(f"KR_{_platform_env_name(platform)}_CHROME_USER_DATA_DIR", "").strip()
    if env_specific:
        return env_specific
    if (
        platform == "xhs"
        and XHS_CHROME_USER_DATA_DIR
        and os.environ.get("KR_XHS_FORCE_CHROME_USER_DATA_DIR", "0").strip().lower() in {"1", "true", "yes", "on"}
    ):
        return XHS_CHROME_USER_DATA_DIR
    if platform == "zhihu" and ZHIHU_CHROME_USER_DATA_DIR:
        return ZHIHU_CHROME_USER_DATA_DIR
    if platform == "boss" and BOSS_CHROME_USER_DATA_DIR:
        return BOSS_CHROME_USER_DATA_DIR
    if platform == "liepin" and LIEPIN_CHROME_USER_DATA_DIR:
        return LIEPIN_CHROME_USER_DATA_DIR
    if platform == "zhilian" and ZHILIAN_CHROME_USER_DATA_DIR:
        return ZHILIAN_CHROME_USER_DATA_DIR
    if platform == "maimai" and MAIMAI_CHROME_USER_DATA_DIR:
        return MAIMAI_CHROME_USER_DATA_DIR
    if platform == "cnki" and CNKI_CHROME_USER_DATA_DIR:
        return CNKI_CHROME_USER_DATA_DIR
    if MANAGED_CHROME_USER_DATA_DIR:
        return MANAGED_CHROME_USER_DATA_DIR
    if platform == "xhs":
        admitted = select_main_chain_profile("xiaohongshu")
        if admitted.get("status") == "ok" and admitted.get("profile_dir"):
            return str(admitted["profile_dir"])
        selected = _select_xhs_account_pool_profile()
        if selected:
            return selected
        if XHS_CHROME_USER_DATA_DIR:
            return XHS_CHROME_USER_DATA_DIR
        if os.environ.get("KR_XHS_ALLOW_LEGACY_PROFILE_FALLBACK", "0").strip().lower() not in {"1", "true", "yes", "on"}:
            return ""
    if platform == "zhihu":
        admitted = select_main_chain_profile("zhihu")
        if admitted.get("status") == "ok" and admitted.get("profile_dir"):
            return str(admitted["profile_dir"])
        return ZHIHU_USER_DATA_DIR
    if platform == "boss":
        admitted = select_main_chain_profile("boss")
        if admitted.get("status") == "ok" and admitted.get("profile_dir"):
            return str(admitted["profile_dir"])
        return BOSS_USER_DATA_DIR
    if platform == "liepin":
        admitted = select_main_chain_profile("liepin")
        if admitted.get("status") == "ok" and admitted.get("profile_dir"):
            return str(admitted["profile_dir"])
        return LIEPIN_USER_DATA_DIR
    if platform == "zhilian":
        admitted = select_main_chain_profile("zhilian")
        if admitted.get("status") == "ok" and admitted.get("profile_dir"):
            return str(admitted["profile_dir"])
        return ZHILIAN_USER_DATA_DIR
    if platform == "maimai":
        return MAIMAI_USER_DATA_DIR
    if platform == "cnki":
        admitted = select_main_chain_profile("cnki")
        if admitted.get("status") == "ok" and admitted.get("profile_dir"):
            return str(admitted["profile_dir"])
        return CNKI_USER_DATA_DIR
    manual = _manual_profile_row_for_platform(platform)
    if manual:
        profile_dir = _resolve_registry_profile_dir(str(manual.get("profile_dir") or ""))
        if profile_dir:
            return profile_dir
    if platform in MANUAL_BROWSER_DEFAULT_PORTS:
        return os.path.join(REPO_ROOT, "browser_data", "profiles", platform, "account_a")
    return XHS_USER_DATA_DIR


def _select_xhs_account_pool_profile() -> str:
    try:
        registry = profile_registry_internal()
        selection = account_pool_selection_summary(platform="xiaohongshu", registry=registry)
        recommended = str(selection.get("recommended_profile_id") or "")
        if not recommended:
            return ""
        for profile in raw_registry_for_platform("xiaohongshu").get("profiles", []) or []:
            if not isinstance(profile, dict):
                continue
            if str(profile.get("profile_id") or "") != recommended:
                continue
            profile_dir = str(profile.get("profile_dir") or "")
            if profile_dir and os.path.isdir(os.path.abspath(os.path.expandvars(os.path.expanduser(profile_dir)))):
                return profile_dir
    except Exception as exc:
        log.debug(f"XHS account-pool profile selection failed: {exc}")
    return ""


def _managed_chrome_profile_name(platform: str = "xhs") -> str:
    """Return Chrome's profile-directory name under the user-data-dir."""
    env_specific = os.environ.get(f"KR_{_platform_env_name(platform)}_CHROME_PROFILE_DIRECTORY", "").strip()
    if env_specific:
        return env_specific
    if platform == "xhs" and XHS_CHROME_PROFILE_DIRECTORY:
        return XHS_CHROME_PROFILE_DIRECTORY
    if platform == "zhihu" and ZHIHU_CHROME_PROFILE_DIRECTORY:
        return ZHIHU_CHROME_PROFILE_DIRECTORY
    if platform == "boss" and BOSS_CHROME_PROFILE_DIRECTORY:
        return BOSS_CHROME_PROFILE_DIRECTORY
    if platform == "liepin" and LIEPIN_CHROME_PROFILE_DIRECTORY:
        return LIEPIN_CHROME_PROFILE_DIRECTORY
    if platform == "zhilian" and ZHILIAN_CHROME_PROFILE_DIRECTORY:
        return ZHILIAN_CHROME_PROFILE_DIRECTORY
    if platform == "maimai" and MAIMAI_CHROME_PROFILE_DIRECTORY:
        return MAIMAI_CHROME_PROFILE_DIRECTORY
    if platform == "cnki" and CNKI_CHROME_PROFILE_DIRECTORY:
        return CNKI_CHROME_PROFILE_DIRECTORY
    return MANAGED_CHROME_PROFILE_DIRECTORY or "Default"


def _cleanup_managed_chrome() -> None:
    """Close every managed Chrome instance while preserving profile folders."""
    global _MANAGED_CHROME_PROCS, _MANAGED_CHROME_PROFILE_DIRS, _MANAGED_CHROME_PIDS, _MANAGED_CHROME_OWNED_PIDS
    for platform, _proc in list(_MANAGED_CHROME_PROCS.items()):
        _cleanup_managed_chrome_platform(platform)
    _MANAGED_CHROME_PROCS = {}
    _MANAGED_CHROME_PROFILE_DIRS = {}
    _MANAGED_CHROME_PIDS = {}
    _MANAGED_CHROME_OWNED_PIDS = set()


def _cleanup_managed_chrome_platform(platform: str, *, expected_profile_dir: str = "") -> None:
    """Close one managed Chrome instance while preserving the profile folder.
    
    Shutdown order (ensures cookies are flushed to SQLite):
    1. CDP Browser.close → Chrome flushes cookies and exits cleanly
    2. proc.terminate() → graceful SIGTERM
    3. proc.kill() → last resort
    """
    normalized_platform = _browser_resource_key(platform)
    if normalized_platform == "xhs" and len(_active_xhs_profile_resources()) > 1:
        record_browser_event(
            platform,
            "cleanup_skipped_ambiguous_xhs_resource",
            {"active_resources": list(_active_xhs_profile_resources())},
        )
        return
    platform = normalized_platform
    timer = _CHROME_IDLE_TIMERS.pop(platform, None)
    if timer:
        timer.cancel()
    record_browser_event(platform, "cleanup_started", {"timer_cancelled": bool(timer)})
    # 知乎平台清理时，同时取消会话刷新定时器
    if platform == "zhihu":
        global _zhihu_refresh_timer
        if _zhihu_refresh_timer:
            _zhihu_refresh_timer.cancel()
            _zhihu_refresh_timer = None
    proc = _MANAGED_CHROME_PROCS.pop(platform, None)
    known_pid = _MANAGED_CHROME_PIDS.pop(platform, None)
    pid = known_pid or _find_chrome_with_debug_port(platform)
    expected_profile = os.path.abspath(expected_profile_dir or _managed_chrome_profile_dir(platform)).lower()
    actual_profile = _chrome_user_data_dir_for_pid(pid).lower() if pid else ""
    should_close = bool(pid and (pid in _MANAGED_CHROME_OWNED_PIDS or _same_windows_path(actual_profile, expected_profile)))
    if should_close:
        profile_meta = _profile_metadata_for_platform(platform, profile_dir=actual_profile)
        set_browser_session_deadline(
            platform,
            None,
            profile_id=profile_meta.get("profile_id", ""),
            account_slot=profile_meta.get("account_slot", ""),
            profile_dir=actual_profile,
            metadata={"cleanup_reason": "managed_chrome_platform"},
            event="idle_cleanup_deadline_cleared",
        )
        transition_browser_session(
            platform,
            "QUIESCING",
            desired_visibility="silent",
            pid=pid,
            profile_id=profile_meta.get("profile_id", ""),
            account_slot=profile_meta.get("account_slot", ""),
            profile_dir=actual_profile,
            metadata={"cleanup_reason": "managed_chrome_platform"},
            event="cleanup_quiescing",
        )
        # Step 1: Try CDP clean close (flushes cookies to SQLite)
        try:
            _close_chrome_via_cdp(platform)
        except Exception:
            pass
        # Step 2: Wait for clean exit
        if proc and proc.poll() is None:
            try:
                proc.wait(timeout=8)
            except Exception:
                pass
        # Step 3: Terminate if still running
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                pass
        # Step 4: Force kill as last resort
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        residual_pid = _find_chrome_with_debug_port(platform)
        if residual_pid:
            _force_kill_chrome_tree(platform, residual_pid, "cleanup_residual_port")
        residual_pid = _find_chrome_with_debug_port(platform)
        _MANAGED_CHROME_OWNED_PIDS.discard(pid)
        if residual_pid:
            transition_browser_session(
                platform,
                "FAILED",
                desired_visibility="silent",
                pid=residual_pid,
                profile_id=profile_meta.get("profile_id", ""),
                account_slot=profile_meta.get("account_slot", ""),
                profile_dir=actual_profile,
                metadata={
                    "cleanup_reason": "managed_chrome_platform",
                    "failure": "chrome_process_still_running_after_cleanup",
                },
                event="cleanup_failed_residual_process",
            )
        else:
            transition_browser_session(
                platform,
                "CLOSED",
                desired_visibility="silent",
                pid=pid,
                profile_id=profile_meta.get("profile_id", ""),
                account_slot=profile_meta.get("account_slot", ""),
                profile_dir=actual_profile,
                metadata={"cleanup_reason": "managed_chrome_platform"},
                event="cleanup_closed",
            )
    _MANAGED_CHROME_PROFILE_DIRS.pop(platform, None)


atexit.register(_cleanup_managed_chrome)


def _idle_cleanup_profile_dir(platform: str) -> str:
    """Bind idle cleanup to the profile that actually owns the CDP port."""
    pid = _MANAGED_CHROME_PIDS.get(platform) or _find_chrome_with_debug_port(platform)
    actual_profile = _chrome_user_data_dir_for_pid(pid) if pid else ""
    return os.path.abspath(actual_profile or _MANAGED_CHROME_PROFILE_DIRS.get(platform) or _managed_chrome_profile_dir(platform))


def _schedule_chrome_idle_cleanup(
    platform: str,
    *,
    profile_dir: str = "",
    deadline_at: Optional[float] = None,
    restored: bool = False,
) -> None:
    """Close managed Chrome after a quiet period while preserving profile state.
    
    CDP Browser.close flushes cookies to SQLite before exit, so Chrome can be
    safely closed in all transport modes (stdio/sse/streamable-http). Cookies
    persist in the profile folder and are automatically restored on next launch.
    """
    normalized_platform = _browser_resource_key(platform)
    if normalized_platform == "xhs" and len(_active_xhs_profile_resources()) > 1:
        record_browser_event(
            platform,
            "idle_cleanup_skipped_ambiguous_xhs_resource",
            {"active_resources": list(_active_xhs_profile_resources())},
        )
        return
    platform = normalized_platform
    if _CHROME_IDLE_SECONDS <= 0:
        record_browser_event(platform, "idle_cleanup_disabled", {"idle_seconds": _CHROME_IDLE_SECONDS})
        return
    target_profile = os.path.abspath(profile_dir or _idle_cleanup_profile_dir(platform))
    target_meta = _profile_metadata_for_platform(platform, profile_dir=target_profile)
    deadline = float(deadline_at) if deadline_at is not None else time.time() + _CHROME_IDLE_SECONDS
    delay_seconds = max(0.05, deadline - time.time())
    timer = _CHROME_IDLE_TIMERS.pop(platform, None)
    if timer:
        timer.cancel()

    def _close_if_idle() -> None:
        # CDP Browser.close flushes cookies to SQLite before exit, so stdio mode
        # can safely close Chrome — cookies persist in the profile folder.

        # 如果平台工具仍在使用 CDP，不要在长详情/转写/验证中途关掉浏览器。
        if _CHROME_ACTIVE_OPERATIONS.get(platform, 0) > 0:
            log.info(f"Chrome 活跃操作中，延后空闲清理: platform={platform}")
            _schedule_chrome_idle_cleanup(platform, profile_dir=target_profile)
            return
        
        # An explicit manual interaction owns its own absolute deadline.  Do
        # not renew it from the ordinary idle loop: that was the source of
        # abandoned BOSS windows being kept alive forever.
        if _CHROME_KEEP_ALIVE.get(platform, False):
            log.info(f"Chrome 人工交互窗口等待明确完成或绝对到期: platform={platform}")
            return
        
        with _chrome_platform_lock(platform):
            pid = _MANAGED_CHROME_PIDS.get(platform) or _find_chrome_with_debug_port(platform)
            if not pid:
                return
            expected_profile = target_profile.lower()
            actual_profile = _chrome_user_data_dir_for_pid(pid).lower()
            if _same_windows_path(actual_profile, expected_profile):
                log.info(f"Chrome 空闲超时，关闭受管实例: platform={platform}, pid={pid}")
                _MANAGED_CHROME_PIDS[platform] = pid
                _cleanup_managed_chrome_platform(platform, expected_profile_dir=target_profile)
            else:
                record_browser_event(
                    platform,
                    "idle_cleanup_skipped_profile_changed",
                    {
                        "expected_profile_hash": _profile_hash(target_profile),
                        "actual_profile_hash": _profile_hash(actual_profile),
                    },
                )

    set_browser_session_deadline(
        platform,
        deadline,
        profile_id=target_meta.get("profile_id", ""),
        account_slot=target_meta.get("account_slot", ""),
        profile_dir=target_profile,
        metadata={"idle_seconds": _CHROME_IDLE_SECONDS, "restored": restored},
        event="idle_cleanup_restored" if restored else "idle_cleanup_scheduled",
    )
    idle_timer = threading.Timer(delay_seconds, _close_if_idle)
    idle_timer.daemon = True
    _CHROME_IDLE_TIMERS[platform] = idle_timer
    idle_timer.start()


def _profile_hash(profile_dir: str) -> str:
    import hashlib

    return hashlib.sha256(str(profile_dir or "").lower().encode("utf-8", errors="ignore")).hexdigest()[:12]


def restore_chrome_idle_cleanups() -> Dict[str, int]:
    """Rehydrate persisted idle deadlines after a server restart without launching Chrome."""
    restored = 0
    overdue = 0
    for session in browser_sessions_summary(limit=50).get("sessions", []):
        if session.get("state") != "READY_SILENT":
            continue
        platform = str(session.get("platform") or "")
        if not platform or platform in _CHROME_IDLE_TIMERS or _CHROME_KEEP_ALIVE.get(platform, False):
            continue
        pid = _MANAGED_CHROME_PIDS.get(platform) or _find_chrome_with_debug_port(platform)
        actual_profile = _chrome_user_data_dir_for_pid(pid) if pid else ""
        actual_meta = _profile_metadata_for_platform(platform, profile_dir=actual_profile)
        if not actual_profile or str(session.get("profile_id") or "") != str(actual_meta.get("profile_id") or ""):
            continue
        # Older sessions predate persisted deadlines. READY_SILENT is the
        # explicit post-operation state, so it is safe to treat such a session
        # as already due instead of leaving a managed browser behind forever.
        deadline = float(session["deadline_at"]) if session.get("deadline_at") is not None else time.time()
        _schedule_chrome_idle_cleanup(
            platform,
            profile_dir=actual_profile,
            deadline_at=deadline,
            restored=True,
        )
        restored += 1
        overdue += int(deadline <= time.time())
    return {"restored": restored, "overdue": overdue}


def reconcile_stale_xhs_manual_interactions() -> Dict[str, int]:
    """Retire a persisted XHS manual prompt when its CDP port owns another profile.

    A prompt is deliberately left untouched unless both the requested profile
    and the live profile can be read and they disagree.  This prevents a
    server restart from keeping an old, wrongly-bound window in the foreground.
    """
    scanned = 0
    rejected = 0
    for session in browser_sessions_summary(limit=50).get("sessions", []):
        if str(session.get("platform") or "") != "xhs":
            continue
        if str(session.get("state") or "") not in {"NEEDS_USER", "USER_INTERACTING"}:
            continue
        expected_profile_id = str(session.get("profile_id") or "")
        if not expected_profile_id:
            continue
        scanned += 1
        expected_profile_dir = _managed_chrome_profile_dir("xhs", target_profile_id=expected_profile_id)
        pid = _MANAGED_CHROME_PIDS.get("xhs") or _find_chrome_with_debug_port("xhs")
        actual_profile_dir = _chrome_user_data_dir_for_pid(pid) if pid else ""
        if not actual_profile_dir or _same_windows_path(actual_profile_dir, expected_profile_dir):
            continue
        _CHROME_KEEP_ALIVE.pop("xhs", None)
        if pid:
            _minimize_chrome_windows(pid)
        transition_browser_session(
            "xhs",
            "FAILED",
            desired_visibility="silent",
            reason="profile_binding_mismatch_reconciled",
            pid=pid,
            profile_id=expected_profile_id,
            profile_dir=expected_profile_dir,
            metadata={
                "expected_profile_hash": _profile_hash(expected_profile_dir),
                "actual_profile_hash": _profile_hash(actual_profile_dir),
                "reconciled_at_bootstrap": True,
            },
            event="stale_manual_interaction_profile_binding_rejected",
        )
        rejected += 1
    return {"scanned": scanned, "rejected": rejected}


def finish_chrome_automation(platform: str, reason: str = "operation_end") -> None:
    """Close managed Chrome after an automatic task, unless manual interaction is active."""
    if _CHROME_KEEP_ALIVE.get(platform, False):
        log.info(f"Chrome keep_alive 模式，保留人工交互窗口: platform={platform}")
        _schedule_chrome_idle_cleanup(platform)
        return

    pid = _MANAGED_CHROME_PIDS.get(platform) or _find_chrome_with_debug_port(platform)
    expected_profile = os.path.abspath(_managed_chrome_profile_dir(platform)).lower()
    actual_profile = _chrome_user_data_dir_for_pid(pid).lower() if pid else ""
    if _CHROME_CLOSE_AFTER_OPERATION and pid and _same_windows_path(actual_profile, expected_profile):
        log.info(f"自动任务结束，立即关闭受管 Chrome: platform={platform}, pid={pid}")
        record_browser_event(
            platform,
            "operation_end_immediate_cleanup",
            {"pid": pid, "reason": reason, "close_after_operation": True},
        )
        _MANAGED_CHROME_PIDS[platform] = pid
        _cleanup_managed_chrome_platform(platform)
    else:
        _schedule_chrome_idle_cleanup(platform)


@contextmanager
def chrome_active_operation(platform: str):
    """Keep a managed Chrome instance alive while a platform operation is running."""
    profile_dir = _managed_chrome_profile_dir(platform)
    profile_meta = _profile_metadata_for_platform(platform)
    lease_key = ":".join(
        part
        for part in [
            str(platform or "unknown"),
            str(profile_meta.get("profile_id") or ""),
            str(profile_meta.get("account_slot") or ""),
            _chrome_debug_port(platform),
            profile_dir,
        ]
        if part
    )
    lease = get_runtime_lease_coordinator().acquire_exclusive(
        "browser_profile",
        lease_key,
        owner=default_owner("chrome_active_operation"),
        ttl_s=max(60, _CHROME_IDLE_SECONDS + 60),
        metadata={"platform": platform, "profile_id": profile_meta.get("profile_id", ""), "account_slot": profile_meta.get("account_slot", "")},
    )
    if not lease.acquired:
        raise RuntimeError(f"browser profile busy for {platform}; retry_after_s={lease.retry_after_s}")
    _CHROME_ACTIVE_OPERATIONS[platform] = _CHROME_ACTIVE_OPERATIONS.get(platform, 0) + 1
    timer = _CHROME_IDLE_TIMERS.pop(platform, None)
    if timer:
        timer.cancel()
    try:
        yield
    finally:
        try:
            remaining = max(0, _CHROME_ACTIVE_OPERATIONS.get(platform, 0) - 1)
            if remaining:
                _CHROME_ACTIVE_OPERATIONS[platform] = remaining
            else:
                _CHROME_ACTIVE_OPERATIONS.pop(platform, None)
                finish_chrome_automation(platform, reason="chrome_active_operation")
        finally:
            get_runtime_lease_coordinator().release(lease.lease_id)


@contextmanager
def _managed_chrome_session(platform: str):
    """Run one platform task with an auto-started Chrome, then close it if owned."""
    try:
        yield
    finally:
        # CDP Browser.close flushes cookies to SQLite before exit, so all transport
        # modes can safely close Chrome — cookies persist in the profile folder.
        pid = _MANAGED_CHROME_PIDS.get(platform) or _find_chrome_with_debug_port(platform)
        expected_profile = os.path.abspath(_managed_chrome_profile_dir(platform)).lower()
        actual_profile = _chrome_user_data_dir_for_pid(pid).lower() if pid else ""
        if pid and _same_windows_path(actual_profile, expected_profile):
            log.info(f"关闭受管 Chrome: platform={platform}, pid={pid}")
            _MANAGED_CHROME_PIDS[platform] = pid
            _cleanup_managed_chrome_platform(platform)


def _chrome_startupinfo():
    """Return Windows startup info that avoids stealing foreground focus."""
    if os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    # SW_SHOWMINNOACTIVE minimizes without activating the window.
    startupinfo.wShowWindow = 7
    return startupinfo


def _chrome_stealth_flags_for_platform(platform: str) -> list[str]:
    """Return opt-in experimental Chrome flags for isolated browser experiments."""
    if str(platform or "").strip().lower() in STEALTH_CHROME_PLATFORMS:
        return ["--disable-blink-features=AutomationControlled"]
    return []


def _platform_for_chrome_pid(pid: int) -> str:
    for platform, known_pid in _MANAGED_CHROME_PIDS.items():
        if known_pid == pid:
            return platform
    return ""


def _chrome_process_tree_pids(root_pid: int) -> set[int]:
    """Return root PID plus Chrome child PIDs for more reliable window matching."""
    pids = {int(root_pid)} if root_pid else set()
    if os.name != "nt" or not root_pid:
        return pids
    try:
        result = silent_subprocess_run(
            [
                "wmic",
                "process",
                "where",
                "name='chrome.exe'",
                "get",
                "ProcessId,ParentProcessId",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        children_by_parent: Dict[int, set[int]] = {}
        for line in result.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) < 2 or not all(part.isdigit() for part in parts[-2:]):
                continue
            parent_pid = int(parts[-2])
            child_pid = int(parts[-1])
            children_by_parent.setdefault(parent_pid, set()).add(child_pid)
        frontier = [int(root_pid)]
        while frontier:
            current = frontier.pop()
            for child in children_by_parent.get(current, set()):
                if child not in pids:
                    pids.add(child)
                    frontier.append(child)
    except Exception as e:
        log.debug(f"Enumerate Chrome process tree failed: {e}")
    return pids


def _minimize_chrome_windows(pid: int) -> None:
    """Best-effort post-launch minimize for Chrome windows owned by pid."""
    if os.name != "nt" or not pid:
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        SW_SHOWMINNOACTIVE = 7
        target_pids = _chrome_process_tree_pids(pid)
        changed = 0
        offscreen_left, offscreen_top = _parse_window_pair(_CHROME_SILENT_WINDOW_POSITION, (-32000, -32000))
        width, height = _parse_window_size(_CHROME_WINDOW_SIZE)
        SWP_NOACTIVATE = 0x0010
        SWP_NOZORDER = 0x0004

        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def _callback(hwnd, _lparam):
            nonlocal changed
            proc_id = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
            if proc_id.value in target_pids and user32.IsWindowVisible(hwnd):
                if _CHROME_SILENT_OFFSCREEN:
                    user32.SetWindowPos(hwnd, 0, offscreen_left, offscreen_top, width, height, SWP_NOACTIVATE | SWP_NOZORDER)
                user32.ShowWindow(hwnd, SW_SHOWMINNOACTIVE)
                changed += 1
            return True

        user32.EnumWindows(EnumWindowsProc(_callback), 0)
        platform = _platform_for_chrome_pid(pid)
        if platform:
            record_browser_event(platform, "window_minimize_attempt", {"root_pid": pid, "matched_windows": changed})
    except Exception as e:
        log.debug(f"Minimize Chrome window failed: {e}")


def _bring_chrome_windows_to_front(pid: int) -> None:
    """Best-effort foreground activation for Chrome windows owned by pid."""
    if os.name != "nt" or not pid:
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        SW_RESTORE = 9
        target_pids = _chrome_process_tree_pids(pid)
        changed = 0
        visible_left, visible_top = _parse_window_pair(_CHROME_VISIBLE_WINDOW_POSITION, (80, 80))
        width, height = _parse_window_size(_CHROME_WINDOW_SIZE)
        SWP_NOZORDER = 0x0004

        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def _callback(hwnd, _lparam):
            nonlocal changed
            proc_id = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
            if proc_id.value in target_pids and user32.IsWindowVisible(hwnd):
                user32.SetWindowPos(hwnd, 0, visible_left, visible_top, width, height, SWP_NOZORDER)
                user32.ShowWindow(hwnd, SW_RESTORE)
                user32.SetForegroundWindow(hwnd)
                changed += 1
                return False
            return True

        user32.EnumWindows(EnumWindowsProc(_callback), 0)
        platform = _platform_for_chrome_pid(pid)
        if platform:
            record_browser_event(platform, "window_foreground_attempt", {"root_pid": pid, "matched_windows": changed})
    except Exception as e:
        log.debug(f"Bring Chrome window to front failed: {e}")


def bring_chrome_to_front(platform: str = "xhs") -> Dict:
    normalized_platform = _browser_resource_key(platform)
    if normalized_platform == "xhs" and len(_active_xhs_profile_resources()) > 1:
        return {
            "status": "blocked",
            "validation_status": "EXPECTED_DEGRADED",
            "detail": "小红书同时有多个账号浏览器，未猜测前台窗口。",
            "platform": platform,
            "active_resources": list(_active_xhs_profile_resources()),
        }
    platform = normalized_platform
    pid = _MANAGED_CHROME_PIDS.get(platform) or _find_chrome_with_debug_port(platform)
    if not pid:
        return {"status": "skipped", "detail": "Chrome CDP 进程未运行", "platform": platform}
    _bring_chrome_windows_to_front(pid)
    return {"status": "ok", "detail": "Chrome 窗口已尝试拉到前台", "platform": platform, "pid": pid}


def pause_chrome_idle_cleanup(platform: str) -> Dict:
    """暂停 Chrome 空闲超时，用于需要用户扫码的场景。
    
    调用此函数后，Chrome 不会被自动关闭，直到调用 resume_chrome_idle_cleanup。
    """
    _CHROME_KEEP_ALIVE[platform] = True
    log.info(f"Chrome 空闲超时已暂停: platform={platform}")
    transition_browser_session(
        platform,
        "NEEDS_USER",
        desired_visibility="attention",
        reason="keep_alive_requested",
        metadata={"keep_alive": True},
        event="idle_cleanup_paused",
    )
    return {"status": "ok", "detail": "Chrome 空闲超时已暂停，等待用户操作", "platform": platform}


def resume_chrome_idle_cleanup(platform: str) -> Dict:
    """恢复 Chrome 空闲超时。
    
    用户扫码完成后调用此函数，恢复正常的空闲超时机制。
    """
    _CHROME_KEEP_ALIVE[platform] = False
    log.info(f"Chrome 空闲超时已恢复: platform={platform}")
    transition_browser_session(
        platform,
        "READY_SILENT",
        desired_visibility="silent",
        reason="keep_alive_released",
        metadata={"keep_alive": False},
        event="idle_cleanup_resumed",
    )
    # 重新调度空闲超时
    _schedule_chrome_idle_cleanup(platform)
    return {"status": "ok", "detail": "Chrome 空闲超时已恢复", "platform": platform}


def _cancel_manual_interaction_expiry(platform: str) -> None:
    timer = _CHROME_MANUAL_INTERACTION_EXPIRY_TIMERS.pop(platform, None)
    if timer:
        timer.cancel()


def _schedule_manual_interaction_expiry(
    platform: str,
    *,
    deadline_at: float,
    profile_dir: str,
    profile_id: str = "",
    account_slot: str = "",
) -> None:
    """Persist a reminder deadline without ever closing a XHS recovery window.

    A QR scan has no predictable duration.  The old TTL cancellation could
    silently remove the very account prompt the user needed to see.  XHS
    sessions therefore retain a persisted *reminder* deadline only; explicit
    cancellation or a verified authentication probe is required to end them.
    """
    _cancel_manual_interaction_expiry(platform)
    set_browser_session_deadline(
        platform,
        deadline_at,
        profile_id=profile_id,
        account_slot=account_slot,
        profile_dir=profile_dir,
        metadata={"manual_interaction_deadline": True, "deadline_is_reminder_only": _split_browser_resource(platform)[0] == "xhs"},
        event="manual_interaction_deadline_scheduled",
    )
    if _split_browser_resource(platform)[0] != "xhs":
        delay_s = max(0.05, float(deadline_at) - time.time())

        def _expire() -> None:
            cancel_browser_interaction(platform, reason="manual_interaction_expired", expected_profile_dir=profile_dir)

        timer = threading.Timer(delay_s, _expire)
        timer.daemon = True
        _CHROME_MANUAL_INTERACTION_EXPIRY_TIMERS[platform] = timer
        timer.start()


def cancel_browser_interaction(
    platform: str,
    *,
    reason: str = "manual_interaction_cancelled",
    expected_profile_dir: str = "",
) -> Dict:
    """Safely close an abandoned explicit interaction without asserting login.

    Closing is restricted to the managed debug port and its expected profile.
    It releases the coordination lease and records cancellation/expiry, but
    never marks a platform authenticated or replays the original search.
    """
    platform = _browser_resource_key(platform)
    if platform == "xhs" and len(_active_xhs_profile_resources()) > 1:
        return {
            "status": "blocked",
            "validation_status": "EXPECTED_DEGRADED",
            "platform": platform,
            "reason": reason,
            "detail": "小红书同时有多个账号浏览器，未猜测要取消的窗口。",
            "active_resources": list(_active_xhs_profile_resources()),
        }
    _cancel_manual_interaction_expiry(platform)
    _CHROME_KEEP_ALIVE.pop(platform, None)
    lease_id = _CHROME_MANUAL_INTERACTION_LEASES.pop(platform, "")
    lease_released = bool(lease_id and get_runtime_lease_coordinator().release(lease_id))
    pid = _MANAGED_CHROME_PIDS.get(platform) or _find_chrome_with_debug_port(platform)
    profile_dir = os.path.abspath(expected_profile_dir or _idle_cleanup_profile_dir(platform))
    actual_profile = _chrome_user_data_dir_for_pid(pid) if pid else ""
    if pid and actual_profile and not _same_windows_path(actual_profile, profile_dir):
        transition_browser_session(
            platform,
            "FAILED",
            desired_visibility="silent",
            pid=pid,
            profile_dir=actual_profile,
            reason="manual_interaction_cancel_profile_mismatch",
            metadata={"expected_profile_hash": _profile_hash(profile_dir), "actual_profile_hash": _profile_hash(actual_profile)},
            event="manual_interaction_cancel_rejected_profile_mismatch",
        )
        return {"status": "blocked", "platform": platform, "reason": reason, "detail": "受管浏览器资料目录不匹配，未关闭任何浏览器。", "lease_released": lease_released}
    set_browser_session_deadline(platform, None, profile_dir=profile_dir, metadata={"manual_interaction_cancelled": True}, event="manual_interaction_deadline_cleared")
    transition_browser_session(
        platform,
        "QUIESCING",
        desired_visibility="silent",
        pid=pid,
        profile_dir=profile_dir,
        reason=reason,
        metadata={"manual_interaction_cancelled": True, "authenticated": "unverified"},
        event="manual_interaction_expired" if reason == "manual_interaction_expired" else "manual_interaction_cancelled",
    )
    if pid:
        _MANAGED_CHROME_PIDS[platform] = pid
        _cleanup_managed_chrome_platform(platform, expected_profile_dir=profile_dir)
    else:
        transition_browser_session(
            platform,
            "CLOSED",
            desired_visibility="silent",
            profile_dir=profile_dir,
            reason=reason,
            metadata={"manual_interaction_cancelled": True, "already_not_running": True},
            event="manual_interaction_cancelled_not_running",
        )
    return {"status": "cancelled", "platform": platform, "reason": reason, "lease_released": lease_released, "browser_sessions": browser_sessions_summary(limit=5)}


def request_user_login(
    platform: str,
    reason: str = "login_required",
    *,
    target_profile_id: str = "",
    trigger_evidence: Optional[list[str]] = None,
    source: str = "",
) -> Dict:
    """请求用户登录：暂停空闲超时 → 弹出 Chrome → 等待用户扫码 → 恢复空闲超时 → 最小化 Chrome。
    
    当检测到登录态失效时，调用此函数让用户扫码登录。
    
    Args:
        platform: 平台名称（zhihu, xhs, boss 等）
        reason: 请求登录的原因（login_required, security_verification 等）
    
    Returns:
        包含状态和提示信息的字典
    """
    platform = _browser_platform(platform)
    base_platform, embedded_profile_id = _split_browser_resource(platform)
    target_profile_id = target_profile_id or embedded_profile_id
    resource_key = _browser_resource_key(platform, target_profile_id)
    evidence = [str(item)[:160] for item in (trigger_evidence or []) if str(item).strip()]
    # An automatic XHS prompt is safe only when it names one concrete profile
    # and carries direct platform evidence.  A stale platform-wide gate must
    # never choose an account or steal foreground focus on its own.
    if base_platform == "xhs" and not target_profile_id and not str(reason or "").startswith("account_claim"):
        return {
            "status": "blocked",
            "validation_status": "EXPECTED_DEGRADED",
            "platform": platform,
            "reason": reason,
            "reason_code": "PROFILE_BINDING_REQUIRED",
            "detail": "小红书自动人工交互缺少已核验的账号资料目录，已保持后台，不会弹出浏览器。",
        }
    if base_platform == "xhs" and target_profile_id and not evidence and not str(reason or "").startswith("account_claim"):
        return {
            "status": "blocked",
            "validation_status": "EXPECTED_DEGRADED",
            "platform": platform,
            "reason": reason,
            "reason_code": "TRIGGER_EVIDENCE_REQUIRED",
            "detail": "小红书自动人工交互缺少登录页或验证页证据，已保持后台，不会弹出浏览器。",
        }
    log.info(f"请求用户登录: platform={resource_key}, reason={reason}, source={source or 'unspecified'}")
    running_pid = _MANAGED_CHROME_PIDS.get(resource_key) or _find_chrome_with_debug_port(resource_key)
    running_profile_dir = _chrome_user_data_dir_for_pid(running_pid) if running_pid else ""
    profile_dir = (
        _managed_chrome_profile_dir(resource_key, target_profile_id=target_profile_id)
        if target_profile_id
        else _managed_chrome_profile_dir(resource_key)
    )
    if base_platform == "xhs" and not target_profile_id and running_profile_dir:
        actual_meta = _profile_metadata_for_platform(resource_key, profile_dir=running_profile_dir)
        if actual_meta:
            profile_dir = running_profile_dir
            profile_meta = actual_meta
        else:
            profile_meta = _profile_metadata_for_platform(resource_key, profile_dir=profile_dir)
    else:
        profile_meta = (
            _profile_metadata_for_platform(resource_key, profile_dir=profile_dir, target_profile_id=target_profile_id)
            if target_profile_id
            else _profile_metadata_for_platform(resource_key)
        )
    lease_key = ":".join(
        part
        for part in [
            str(resource_key or "unknown"),
            str(profile_meta.get("profile_id") or ""),
            str(profile_meta.get("account_slot") or ""),
            _chrome_debug_port(resource_key),
        ]
        if part
    )
    manual_ttl_s = max(60, int(os.environ.get("KR_MANUAL_INTERACTION_LEASE_TTL_S", "900")))
    manual_deadline_at = time.time() + manual_ttl_s
    lease = get_runtime_lease_coordinator().acquire_exclusive(
        "manual_interaction",
        lease_key,
        owner=default_owner("request_browser_interaction"),
        ttl_s=manual_ttl_s,
        metadata={
            "platform": resource_key,
            "reason": reason,
            "profile_id": profile_meta.get("profile_id", ""),
            "account_slot": profile_meta.get("account_slot", ""),
            "source": source,
            "trigger_evidence": evidence,
        },
    )
    if not lease.acquired:
        return {
            "status": "busy",
            "validation_status": "EXPECTED_DEGRADED",
            "detail": "同一平台/账号已有人工登录或验证正在进行，当前请求不会重复弹窗。",
            "platform": platform,
            "reason": reason,
            "retry_after_s": lease.retry_after_s,
            "active_interaction": lease.holder,
            "next_step": "等待现有交互完成后重试，或调用 health_check(mode='browser_sessions') 查看当前状态。",
        }
    _CHROME_MANUAL_INTERACTION_LEASES[resource_key] = lease.lease_id
    session = upsert_browser_session(
        platform=resource_key,
        debug_port=_chrome_debug_port(resource_key),
        profile_dir=profile_dir,
        profile_id=profile_meta.get("profile_id", ""),
        account_slot=profile_meta.get("account_slot", ""),
        state="NEEDS_USER",
        desired_visibility="attention",
        reason=reason,
        target_url=_startup_url_for_platform(resource_key),
        metadata={
            "entrypoint": "request_user_login",
            "channel_id": profile_meta.get("channel_id", ""),
            "account_identity": {
                "display_label": profile_meta.get("display_label", ""),
                "masked_hint": profile_meta.get("masked_hint", ""),
            },
            "manual_source": source,
            "trigger_evidence": evidence,
        },
        event="user_login_requested",
    )

    # Start or switch silently first.  Visibility is granted only after the
    # listening browser is proven to own the requested profile directory.
    ensure_kwargs = {"visible": False, "detach": False}
    if target_profile_id:
        ensure_kwargs["target_profile_id"] = target_profile_id
    if not _ensure_chrome_debugging(resource_key, **ensure_kwargs):
        get_runtime_lease_coordinator().release(lease.lease_id)
        _CHROME_MANUAL_INTERACTION_LEASES.pop(resource_key, None)
        transition_browser_session(
            resource_key,
            "FAILED",
            desired_visibility="attention",
            reason=reason,
            metadata={"failure": "chrome_start_failed"},
            event="user_login_chrome_start_failed",
        )
        return {
            "status": "error",
            "detail": "Chrome 启动失败",
            "platform": platform,
            "reason": reason,
        }

    pid = _MANAGED_CHROME_PIDS.get(resource_key) or _find_chrome_with_debug_port(resource_key)
    if base_platform == "xhs":
        actual_profile = _chrome_user_data_dir_for_pid(pid) if pid else ""
        if not actual_profile or not _same_windows_path(actual_profile, profile_dir):
            get_runtime_lease_coordinator().release(lease.lease_id)
            _CHROME_MANUAL_INTERACTION_LEASES.pop(resource_key, None)
            transition_browser_session(
                resource_key,
                "FAILED",
                desired_visibility="silent",
                reason="profile_binding_mismatch",
                pid=pid,
                profile_id=profile_meta.get("profile_id", ""),
                account_slot=profile_meta.get("account_slot", ""),
                profile_dir=profile_dir,
                metadata={
                    "expected_profile_hash": _profile_hash(profile_dir),
                    "actual_profile_hash": _profile_hash(actual_profile),
                    "manual_source": source,
                    "trigger_evidence": evidence,
                },
                event="manual_interaction_profile_binding_rejected",
            )
            return {
                "status": "blocked",
                "validation_status": "EXPECTED_DEGRADED",
                "platform": platform,
                "reason": reason,
                "reason_code": "PROFILE_BINDING_MISMATCH",
                "detail": "小红书实际浏览器账号与待处理账号不一致，已保持后台，不会弹出错误窗口。",
            }

    # The binding is now safe. Keep the intended window alive and make it
    # visible exactly once for the user.
    _CHROME_KEEP_ALIVE[resource_key] = True
    _schedule_manual_interaction_expiry(
        resource_key,
        deadline_at=manual_deadline_at,
        profile_dir=profile_dir,
        profile_id=str(profile_meta.get("profile_id") or ""),
        account_slot=str(profile_meta.get("account_slot") or ""),
    )
    log.info(f"Chrome 空闲超时已暂停: platform={resource_key}")
    result = bring_chrome_to_front(resource_key)
    log.info(f"Chrome 已弹出: {result}")
    session = transition_browser_session(
        resource_key,
        "USER_INTERACTING",
        desired_visibility="attention",
        reason=reason,
        pid=pid,
        metadata={
            "bring_to_front_status": result.get("status"),
            "manual_source": source,
            "trigger_evidence": evidence,
            "profile_binding_verified": True,
            "manual_deadline_at": manual_deadline_at,
        },
        event="user_login_waiting_for_user",
    )
    watcher = {}
    if base_platform == "xhs" and profile_meta.get("profile_id"):
        # The watcher observes this exact profile's CDP page.  It does not
        # retry search or infer success from a QR/URL change; its auth probe is
        # the only path that can auto-complete this interaction.
        from .xhs_auth_watcher import start_xhs_auth_watcher

        watcher = start_xhs_auth_watcher(str(profile_meta["profile_id"]))
    
    # 4. 返回提示信息，让调用方知道需要用户操作
    return {
        "status": "waiting_for_user",
        "detail": "Chrome 已弹出，等待用户扫码登录",
        "platform": base_platform,
        "reason": reason,
        "session_id": session.get("session_id"),
        "lease": lease.to_dict(),
        "manual_action": manual_action_request_from_session(
            session,
            reason_code=reason,
            trigger_evidence=evidence or ["request_browser_interaction_explicit", f"platform={base_platform}"],
        ),
        "action_required": "请在 Chrome 窗口中扫码登录",
        "next_step": (
            "扫码后系统会自动确认登录并收口；若需人工诊断，可调用 health_check(mode='browser_sessions')。"
            if base_platform == "xhs"
            else f"登录完成后，调用 health_check(mode='complete_browser_interaction:{base_platform}') 恢复普通浏览器生命周期"
        ),
        "auth_watcher": watcher,
    }


def request_browser_interaction(
    platform: str,
    reason: str = "manual_action_required",
    *,
    target_profile_id: str = "",
    trigger_evidence: Optional[list[str]] = None,
    source: str = "",
) -> Dict:
    """Start a durable human-interaction browser session for a platform."""
    return request_user_login(
        platform,
        reason=reason,
        target_profile_id=target_profile_id,
        trigger_evidence=trigger_evidence,
        source=source,
    )


def complete_user_login(platform: str, *, profile_id: str = "", profile_dir: str = "") -> Dict:
    """用户登录完成：恢复空闲超时 → 最小化 Chrome。
    
    用户扫码完成后，调用此函数恢复正常状态。
    
    Args:
        platform: 平台名称（zhihu, xhs, boss 等）
    
    Returns:
        包含状态的字典
    """
    platform = _browser_resource_key(platform, profile_id)
    log.info(f"用户登录完成: platform={platform}")
    _cancel_manual_interaction_expiry(platform)
    transition_browser_session(
        platform,
        "USER_DONE_VERIFYING",
        desired_visibility="attention",
        reason="user_confirmed_login_complete",
        metadata={"entrypoint": "complete_user_login"},
        event="user_login_complete_requested",
        profile_id=profile_id,
        profile_dir=profile_dir,
    )
    
    # 1. 恢复空闲超时
    _CHROME_KEEP_ALIVE[platform] = False
    log.info(f"Chrome 空闲超时已恢复: platform={platform}")
    
    # 2. 先最小化窗口，避免 cleanup 失败时仍停在前台。
    pid = _MANAGED_CHROME_PIDS.get(platform) or _find_chrome_with_debug_port(platform)
    if pid:
        _minimize_chrome_windows(pid)
        log.info(f"Chrome 已最小化: platform={platform}, pid={pid}")
        transition_browser_session(
            platform,
            "READY_SILENT",
            desired_visibility="silent",
            pid=pid,
            reason="user_login_complete",
            metadata={"minimized": True},
            event="user_login_completed_silent",
            profile_id=profile_id,
            profile_dir=profile_dir,
        )
    
    # 3. 回到普通自动任务生命周期。一次性 CLI 进程里的 idle timer 会随
    # 进程退出而消失，所以这里直接使用正常的 operation-end cleanup。
    finish_chrome_automation(platform, reason="user_login_complete")
    
    lease_id = _CHROME_MANUAL_INTERACTION_LEASES.pop(platform, "")
    if lease_id:
        get_runtime_lease_coordinator().release(lease_id)

    return {
        "status": "ok",
        "detail": "Chrome 已恢复普通生命周期并执行自动收尾",
        "platform": platform,
        "browser_sessions": browser_sessions_summary(limit=5),
    }


def complete_browser_interaction(
    platform: str,
    probe_result: Optional[Dict] = None,
    *,
    profile_id: str = "",
    profile_dir: str = "",
) -> Dict:
    """Mark a human-interaction browser session complete and restore silent mode."""
    resource_key = _browser_resource_key(platform, profile_id)
    result = complete_user_login(resource_key, profile_id=profile_id, profile_dir=profile_dir)
    if probe_result is not None:
        probe_ok = probe_result.get("status") == "ok" and not bool(probe_result.get("manual_action_required"))
        event = "manual_state_auto_recovered" if probe_ok else "browser_interaction_probe_recorded"
        transition_browser_session(
            resource_key,
            "READY_SILENT" if probe_ok else "NEEDS_USER",
            desired_visibility="silent" if probe_ok else "attention",
            reason="interaction_probe_complete",
            last_probe_result=probe_result,
            metadata={
                "probe_status": probe_result.get("status"),
                "manual_state_auto_recovered": probe_ok,
                "platform_state": probe_result.get("platform_state") or probe_result.get("auth_state") or "",
            },
            event=event,
            profile_id=profile_id,
            profile_dir=profile_dir,
        )
        if probe_ok:
            result["manual_state_auto_recovered"] = True
            result["recovery_probe_status"] = probe_result.get("status")
            # Browser-session recovery and XHS account-pool recovery are two
            # different states.  Only bind them after a successful probe for
            # the explicitly selected profile; never clear a risk state merely
            # because a user closed a browser window.
            if platform.strip().lower() in {"xhs", "xiaohongshu", "小红书"} and profile_id:
                account_recovery = record_xhs_account_event(
                    profile_id,
                    "OK",
                    last_tool="complete_browser_interaction",
                    notes=["authenticated_probe=true", "profile_bound=true"],
                )
                result["account_pool_recovery"] = {
                    "status": account_recovery.get("status"),
                    "profile_id": account_recovery.get("profile_id"),
                    "state": account_recovery.get("state"),
                }
                # Delayed import avoids the failure-tag -> chrome-manager
                # import cycle during process startup.
                from .tool_trace import record_trace_child

                record_trace_child(
                    "platform_state_transition",
                    metadata={
                        "status": "ok",
                        "platform": "xiaohongshu",
                        "transition": "authenticated_probe_to_healthy",
                        "manual_action": False,
                        "reason": "verified_profile_recovery",
                    },
                )
    return result


def background_chrome(platform: str = "xhs") -> Dict:
    pid = _MANAGED_CHROME_PIDS.get(platform) or _find_chrome_with_debug_port(platform)
    if not pid:
        return {"status": "skipped", "detail": "Chrome CDP 进程未运行", "platform": platform}
    _minimize_chrome_windows(pid)
    transition_browser_session(
        platform,
        "READY_SILENT",
        desired_visibility="silent",
        pid=pid,
        metadata={"entrypoint": "background_chrome"},
        event="browser_backgrounded",
    )
    return {"status": "ok", "detail": "Chrome 窗口已尝试后台/最小化", "platform": platform, "pid": pid}


def _read_browser_cookie_names(platform: str) -> list[str]:
    """Read cookie names from the platform CDP endpoint without exposing values."""
    try:
        resp = httpx.get(f"{_chrome_debug_url(platform)}/json/version", timeout=3)
        if resp.status_code != 200:
            return []
        if not resp.json().get("webSocketDebuggerUrl", ""):
            return []
        js_code = r"""
        (async () => {
          const port = process.argv[1];
          const platformUrlMarker = process.argv[2] || '';
          const domainMarkers = JSON.parse(process.argv[3] || '[]');
          const version = await fetch(`http://127.0.0.1:${port}/json/version`).then(r => r.json());
          const wsUrl = version.webSocketDebuggerUrl;
          const ws = new WebSocket(wsUrl);
          let seq = 0;
          const pending = new Map();
          await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
          ws.onmessage = event => {
            const message = JSON.parse(event.data);
            if (pending.has(message.id)) {
              pending.get(message.id)(message);
              pending.delete(message.id);
            }
          };
          const send = (method, params, sessionId) => new Promise(resolve => {
            const id = ++seq;
            pending.set(id, resolve);
            const message = { id, method, params };
            if (sessionId) message.sessionId = sessionId;
            ws.send(JSON.stringify(message));
          });
          const targets = await fetch(`http://127.0.0.1:${port}/json/list`).then(r => r.json());
          const page = targets.find(t => platformUrlMarker && (t.url || '').includes(platformUrlMarker)) || targets.find(t => t.type === 'page');
          let cookies = [];
          if (page && page.id) {
            const attached = await send('Target.attachToTarget', { targetId: page.id, flatten: true });
            const sessionId = attached.result?.sessionId;
            if (sessionId) {
              await send('Network.enable', {}, sessionId);
              const result = await send('Network.getAllCookies', {}, sessionId);
              cookies = result.result?.cookies || [];
            }
          }
          if (!cookies.length) {
            const result = await send('Network.getAllCookies', {});
            cookies = result.result?.cookies || [];
          }
          if (domainMarkers.length) {
            cookies = cookies.filter(cookie => domainMarkers.some(marker => String(cookie.domain || '').includes(marker)));
          }
          const names = cookies.map(cookie => cookie.name).filter(Boolean);
          console.log(JSON.stringify([...new Set(names)].sort()));
          ws.close();
        })().catch(error => {
          console.error(error && error.stack ? error.stack : String(error));
          process.exit(1);
        });
        """
        proc = silent_subprocess_run(
            [
                "node",
                "-e",
                js_code,
                _chrome_debug_port(platform),
                _startup_url_for_platform(platform).split("//", 1)[-1].split("/", 1)[0],
                json.dumps(list(MANUAL_BROWSER_AUTH_RULES.get(platform, {}).get("cookie_domain_markers") or [])),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
        )
        if proc.returncode != 0:
            log.debug(f"读取浏览器 Cookie 名称失败: {proc.stderr.strip()}")
            return []
        data = json.loads(proc.stdout.strip() or "[]")
        return [str(name) for name in data if name]
    except Exception as exc:
        log.debug(f"读取浏览器 Cookie 名称异常: {exc}")
        return []


def _read_browser_storage_names(platform: str) -> Dict[str, list[str]]:
    """Read local/session storage key names from the platform page target."""
    try:
        resp = httpx.get(f"{_chrome_debug_url(platform)}/json/version", timeout=3)
        if resp.status_code != 200:
            return {"local_storage": [], "session_storage": []}
        js_code = r"""
        (async () => {
          const port = process.argv[1];
          const platformUrlMarker = process.argv[2] || '';
          const version = await fetch(`http://127.0.0.1:${port}/json/version`).then(r => r.json());
          const wsUrl = version.webSocketDebuggerUrl;
          const ws = new WebSocket(wsUrl);
          let seq = 0;
          const pending = new Map();
          await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
          ws.onmessage = event => {
            const message = JSON.parse(event.data);
            if (pending.has(message.id)) {
              pending.get(message.id)(message);
              pending.delete(message.id);
            }
          };
          const send = (method, params, sessionId) => new Promise(resolve => {
            const id = ++seq;
            pending.set(id, resolve);
            const message = { id, method, params };
            if (sessionId) message.sessionId = sessionId;
            ws.send(JSON.stringify(message));
          });
          const targets = await fetch(`http://127.0.0.1:${port}/json/list`).then(r => r.json());
          const page = targets.find(t => platformUrlMarker && (t.url || '').includes(platformUrlMarker)) || targets.find(t => t.type === 'page');
          const out = { local_storage: [], session_storage: [] };
          if (page && page.id) {
            const attached = await send('Target.attachToTarget', { targetId: page.id, flatten: true });
            const sessionId = attached.result?.sessionId;
            if (sessionId) {
              const result = await send('Runtime.evaluate', {
                expression: "({local_storage: Object.keys(window.localStorage || {}), session_storage: Object.keys(window.sessionStorage || {})})",
                returnByValue: true
              }, sessionId);
              const value = result.result?.result?.value || {};
              out.local_storage = value.local_storage || [];
              out.session_storage = value.session_storage || [];
            }
          }
          console.log(JSON.stringify(out));
          ws.close();
        })().catch(error => {
          console.error(error && error.stack ? error.stack : String(error));
          process.exit(1);
        });
        """
        proc = silent_subprocess_run(
            [
                "node",
                "-e",
                js_code,
                _chrome_debug_port(platform),
                _startup_url_for_platform(platform).split("//", 1)[-1].split("/", 1)[0],
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
        )
        if proc.returncode != 0:
            log.debug(f"读取浏览器 Storage 名称失败: {proc.stderr.strip()}")
            return {"local_storage": [], "session_storage": []}
        data = json.loads(proc.stdout.strip() or "{}")
        return {
            "local_storage": [str(name) for name in data.get("local_storage", []) if name],
            "session_storage": [str(name) for name in data.get("session_storage", []) if name],
        }
    except Exception as exc:
        log.debug(f"读取浏览器 Storage 名称异常: {exc}")
        return {"local_storage": [], "session_storage": []}


def _probe_xhs_browser_auth(*, target_profile_id: str = "", include_platform_state: bool = False) -> Dict:
    """Use Xiaohongshu's platform probe behind the common browser-auth contract."""
    try:
        from collectors.platform.xiaohongshu import _xhs_login_state_ok, xiaohongshu_account_state

        resource_key = _browser_resource_key("xhs", target_profile_id)
        if not _ensure_chrome_debugging(resource_key, visible=False, detach=False, target_profile_id=target_profile_id):
            return {
                "status": "degraded",
                "platform": "xhs",
                "profile_id": target_profile_id,
                "auth_state": "cdp_unavailable",
                "manual_action_required": False,
                "detail": "后台登录态探针无法启动或连接指定小红书浏览器。",
                "retryable": True,
            }
        state = xiaohongshu_account_state(_chrome_debug_url(resource_key))
        result = {
            "platform": "xhs",
            "profile_id": target_profile_id,
            "manual_action_required": False,
        }
        if _xhs_login_state_ok(state):
            result.update(
                {
                    "status": "ok",
                    "auth_state": "authenticated_with_platform_confirmation",
                    "detail": "小红书平台登录态已确认；无需拉前台。",
                }
            )
        elif state.get("has_verify_prompt"):
            result.update(
                {
                    "status": "needs_interaction",
                    "auth_state": "security_verification_required",
                    "manual_action_required": True,
                    "reason_code": "SECURITY_VERIFICATION",
                    "detail": "小红书页面明确要求安全验证。",
                }
            )
        elif state.get("has_login_prompt") or state.get("guest") is True or state.get("code") in {-100, -101}:
            result.update(
                {
                    "status": "needs_interaction",
                    "auth_state": "login_required",
                    "manual_action_required": True,
                    "reason_code": "LOGIN_REQUIRED",
                    "detail": "小红书平台明确要求登录。",
                }
            )
        else:
            result.update(
                {
                    "status": "unknown",
                    "auth_state": "platform_state_unconfirmed",
                    "detail": "小红书未给出可确认的登录身份；不会据此误报需要重新登录。",
                    "retryable": True,
                }
            )
        if include_platform_state:
            result["_platform_state"] = state
        return result
    except Exception as exc:
        return {
            "status": "degraded",
            "platform": "xhs",
            "profile_id": target_profile_id,
            "auth_state": "probe_error",
            "manual_action_required": False,
            "detail": f"小红书平台登录态探针异常: {type(exc).__name__}",
            "retryable": True,
        }
    finally:
        finish_chrome_automation(_browser_resource_key("xhs", target_profile_id), reason="browser_auth_probe")


def probe_browser_auth(platform: str, *, target_profile_id: str = "", _include_platform_state: bool = False) -> Dict:
    """Probe a managed browser profile silently and request interaction only by result.

    This is intentionally separate from request_browser_interaction(): probes
    validate existing login state in the background; requests bring a browser to
    the foreground only when a probe or platform error proves user action is
    needed.
    """
    platform = _browser_platform(platform)
    if platform == "xhs":
        return _probe_xhs_browser_auth(
            target_profile_id=target_profile_id,
            include_platform_state=_include_platform_state,
        )
    if platform == "boss":
        try:
            from collectors.platform.boss import probe_boss_auth_state

            return probe_boss_auth_state(keepalive=True)
        except Exception as exc:
            return {
                "status": "degraded",
                "platform": platform,
                "auth_state": "probe_error",
                "manual_action_required": False,
                "detail": str(exc),
                "retryable": True,
            }
    if platform not in managed_browser_platforms():
        return {
            "status": "error",
            "platform": platform,
            "auth_state": "unsupported_platform",
            "manual_action_required": False,
            "detail": "平台未注册受管浏览器 profile",
        }
    rule = MANUAL_BROWSER_AUTH_RULES.get(platform, {})
    required = tuple(rule.get("required_cookies") or ())
    required_local_storage = tuple(rule.get("required_local_storage_keys") or ())
    required_session_storage = tuple(rule.get("required_session_storage_keys") or ())
    if not required and not required_local_storage and not required_session_storage:
        return {
            "status": "unknown",
            "platform": platform,
            "auth_state": rule.get("auth_state", "login_probe_not_configured"),
            "manual_action_required": False,
            "detail": "该平台尚未声明可自动判定的登录态探针；不要自动拉前台",
        }
    try:
        if not _ensure_chrome_debugging(platform, visible=False, detach=False):
            return {
                "status": "degraded",
                "platform": platform,
                "auth_state": "cdp_unavailable",
                "manual_action_required": False,
                "detail": "后台登录态探针无法启动或连接受管 Chrome",
                "retryable": True,
            }
        cookie_names = set(_read_browser_cookie_names(platform)) if required else set()
        storage_names = (
            _read_browser_storage_names(platform)
            if required_local_storage or required_session_storage
            else {"local_storage": [], "session_storage": []}
        )
        local_storage_names = set(storage_names.get("local_storage") or [])
        session_storage_names = set(storage_names.get("session_storage") or [])
        missing_cookies = sorted(set(required) - cookie_names)
        missing_local_storage = sorted(set(required_local_storage) - local_storage_names)
        missing_session_storage = sorted(set(required_session_storage) - session_storage_names)
        missing = missing_cookies + missing_local_storage + missing_session_storage
        if missing:
            missing_parts = []
            if missing_cookies:
                missing_parts.append("Cookie " + ", ".join(missing_cookies))
            if missing_local_storage:
                missing_parts.append("localStorage " + ", ".join(missing_local_storage))
            if missing_session_storage:
                missing_parts.append("sessionStorage " + ", ".join(missing_session_storage))
            return {
                "status": "needs_interaction",
                "platform": platform,
                "auth_state": "missing_required_cookie",
                "manual_action_required": True,
                "required_cookies": list(required),
                "required_local_storage_keys": list(required_local_storage),
                "required_session_storage_keys": list(required_session_storage),
                "missing_cookies": missing_cookies,
                "missing_local_storage_keys": missing_local_storage,
                "missing_session_storage_keys": missing_session_storage,
                "detail": f"后台登录态探针未通过：缺少关键状态 {'; '.join(missing_parts)}",
                "recommended_action": f"health_check(mode='request_browser_interaction:{platform}:manual_login')",
            }
        return {
            "status": "ok",
            "platform": platform,
            "auth_state": rule.get("auth_state", "authenticated"),
            "manual_action_required": False,
            "required_cookies": list(required),
            "required_local_storage_keys": list(required_local_storage),
            "required_session_storage_keys": list(required_session_storage),
            "observed_cookie_names": sorted(cookie_names),
            "observed_local_storage_keys": sorted(local_storage_names),
            "observed_session_storage_keys": sorted(session_storage_names),
            "detail": "后台登录态探针通过；无需拉前台",
        }
    finally:
        finish_chrome_automation(platform, reason="browser_auth_probe")


def chrome_runtime_summary() -> Dict:
    """Return Chrome/CDP lifecycle state without launching a browser."""
    platforms = {}
    for platform in managed_browser_platforms():
        pid = _MANAGED_CHROME_PIDS.get(platform) or _find_chrome_with_debug_port(platform)
        configured_profile_dir = _managed_chrome_profile_dir(platform)
        profile_dir = os.path.abspath(configured_profile_dir) if configured_profile_dir else ""
        external_profile_pid = _find_chrome_with_profile_dir(profile_dir) if profile_dir else None
        actual_profile = _chrome_user_data_dir_for_pid(pid) if pid else ""
        external_actual_profile = _chrome_user_data_dir_for_pid(external_profile_pid) if external_profile_pid else ""
        cdp_status = "not_connected"
        browser = ""
        if pid:
            try:
                resp = httpx.get(f"{_chrome_debug_url(platform)}/json/version", timeout=2)
                if resp.status_code == 200:
                    cdp_status = "ok"
                    browser = resp.json().get("Browser", "")
                else:
                    cdp_status = f"http_{resp.status_code}"
            except Exception:
                cdp_status = "not_connected"
        platforms[platform] = {
            "port": _chrome_debug_port(platform),
            "debug_url": _chrome_debug_url(platform),
            "pid": pid,
            "managed": bool(pid and (pid in _MANAGED_CHROME_OWNED_PIDS or _same_windows_path(actual_profile, profile_dir))),
            "owned": bool(pid and pid in _MANAGED_CHROME_OWNED_PIDS),
            "cdp_status": cdp_status,
            "browser": browser,
            "profile_dir": profile_dir,
            "blocked_reason": "no_main_chain_allowed_profile" if platform == "xhs" and not profile_dir else "",
            "external_profile_pid": external_profile_pid,
            "external_profile_without_cdp": bool(external_profile_pid and not pid and _same_windows_path(external_actual_profile, profile_dir)),
            "manual_probe": {
                "allowed": True,
                "profile_override_env": f"KR_{_platform_env_name(platform)}_CHROME_USER_DATA_DIR",
                "bypasses_main_chain_admission": platform == "xhs",
                "requires_explicit_profile": platform == "xhs",
                "managed_interaction_mode": "request_browser_interaction",
                "startup_url": _startup_url_for_platform(platform),
            },
            "main_chain_gate": {
                "requires_main_chain_allowed_profile": platform in {"xhs", "zhihu", "boss", "liepin", "cnki"},
                "legacy_fallback_allowed": os.environ.get("KR_XHS_ALLOW_LEGACY_PROFILE_FALLBACK", "0").strip().lower() in {"1", "true", "yes", "on"} if platform == "xhs" else True,
                "profile_selected": bool(profile_dir),
            },
            "profile_directory": _managed_chrome_profile_name(platform),
            "actual_profile_dir": actual_profile,
            "idle_cleanup_s": _CHROME_IDLE_SECONDS,
            "idle_timer_active": platform in _CHROME_IDLE_TIMERS,
        }
    connected = sum(1 for item in platforms.values() if item.get("cdp_status") == "ok")
    return {
        "status": "ok",
        "detail": f"Chrome 按需生命周期可观测，connected={connected}/{len(platforms)}",
        "transport": MCP_TRANSPORT,
        "on_demand": True,
        "strategy": {
            "schema": "knowledgeradar-browser-strategy-runtime/v1",
            "base": "chrome_cdp_persistent_profile",
            "manager": "runtime.chrome_manager",
            "automation": ["playwright_for_dynamic_pages", "raw_cdp_for_platform_control"],
            "experimental_fallbacks": ["camoufox_v2_isolated_backup_candidate", "nodriver_experimental"],
            "selection_rule": "Managed CDP profiles use Google Chrome only; Edge is never an implicit fallback.",
            "executable_resolution": managed_chrome_resolution_summary(),
        },
        "platforms": platforms,
    }


def chrome_runtime_quick_summary() -> Dict:
    """Return a cheap browser lifecycle snapshot for agent-facing health summaries."""
    platforms = {}
    for platform in managed_browser_platforms():
        pid = _MANAGED_CHROME_PIDS.get(platform)
        platforms[platform] = {
            "port": _chrome_debug_port(platform),
            "pid": pid,
            "managed": bool(pid and pid in _MANAGED_CHROME_OWNED_PIDS),
            "owned": bool(pid and pid in _MANAGED_CHROME_OWNED_PIDS),
            "cdp_status": "not_checked",
            "blocked_reason": "no_main_chain_allowed_profile" if platform == "xhs" and not _managed_chrome_profile_dir(platform) else "",
            "diagnostic_mode": f"health_check(mode='probe_browser_auth:{platform}')",
        }
    return {
        "status": "ok",
        "detail": "Chrome quick summary skips CDP and process probes",
        "transport": MCP_TRANSPORT,
        "on_demand": True,
        "platforms": platforms,
    }


def _close_chrome_via_cdp(platform: str) -> bool:
    """Close the browser through CDP so the persistent profile is flushed cleanly."""
    try:
        resp = httpx.get(f"{_chrome_debug_url(platform)}/json/version", timeout=3)
        if resp.status_code != 200:
            return False
        browser_ws = resp.json().get("webSocketDebuggerUrl", "")
        if not browser_ws:
            return False
        js_code = r"""
        (async () => {
          const wsUrl = process.argv[1];
          const ws = new WebSocket(wsUrl);
          await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
          ws.send(JSON.stringify({ id: 1, method: 'Browser.close' }));
          setTimeout(() => process.exit(0), 500);
        })().catch(error => {
          console.error(error && error.stack ? error.stack : String(error));
          process.exit(1);
        });
        """
        proc = silent_subprocess_run(
            ["node", "-e", js_code, browser_ws],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        return proc.returncode == 0
    except Exception as e:
        log.debug(f"Close Chrome via CDP failed: {e}")
        return False


def _ensure_chrome_debugging(
    platform: str = "xhs",
    *,
    visible: bool = False,
    detach: bool = False,
    target_profile_id: str = "",
) -> bool:
    """Ensure the platform's persistent-profile Chrome CDP endpoint is ready."""
    platform = _browser_resource_key(platform, target_profile_id)
    if platform == "xhs" and len(_active_xhs_profile_resources()) > 1:
        record_browser_event(
            platform,
            "browser_start_blocked_ambiguous_xhs_resource",
            {"active_resources": list(_active_xhs_profile_resources())},
        )
        return False
    with _chrome_platform_lock(platform):
        return _ensure_chrome_debugging_locked(
            platform,
            visible=visible,
            detach=detach,
            target_profile_id=target_profile_id,
        )


def _cleanup_stale_chrome(platform: str) -> None:
    """Kill any stale Chrome process using the platform's debug port or profile dir."""
    debug_port = _chrome_debug_port(platform)
    profile_dir = os.path.abspath(_managed_chrome_profile_dir(platform)).lower()

    # 1. Kill by debug port
    pid = _find_chrome_with_debug_port(platform, allow_legacy_edge=True)
    if pid:
        log.info(f"清理残留 Chrome (端口 {debug_port} 占用): platform={platform}, pid={pid}")
        try:
            silent_subprocess_run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=10,
            )
        except Exception as e:
            log.debug(f"清理 Chrome PID={pid} 失败: {e}")
        time.sleep(0.5)

    # 2. Kill by profile directory (handles port-collision or orphan processes)
    try:
        result = silent_subprocess_run(
            ["wmic", "process", "where",
             'commandline like "%--remote-debugging-port%" and (name="chrome.exe" or name="msedge.exe")',
             "get", "ProcessId,CommandLine"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
        )
        for line in result.stdout.splitlines():
            if profile_dir in line.lower():
                parts = line.strip().split()
                try:
                    orphan_pid = int(parts[-1])
                    if orphan_pid != pid:
                        log.info(f"清理残留 Chrome (profile 相同): platform={platform}, pid={orphan_pid}")
                        silent_subprocess_run(
                            ["taskkill", "/PID", str(orphan_pid), "/T", "/F"],
                            capture_output=True, timeout=10,
                        )
                except (ValueError, IndexError):
                    pass
    except Exception as e:
        log.debug(f"按 profile 清理 Chrome 失败: {e}")


def _force_kill_chrome_tree(platform: str, pid: int, reason: str) -> bool:
    """Force-close a verified managed Chrome process tree and record the action."""
    if not pid:
        return False
    record_browser_event(platform, "cleanup_force_kill", {"pid": pid, "reason": reason})
    try:
        result = silent_subprocess_run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        return result.returncode == 0
    except Exception as e:
        log.debug(f"强制清理 Chrome PID={pid} 失败: {e}")
        return False


def _xhs_safe_profile_switch_allowed(actual_profile: str, expected_profile: str) -> bool:
    """Allow only same-platform, project-owned XHS profiles to swap on port 9333."""
    if os.environ.get("KR_XHS_ALLOW_SAFE_PROFILE_SWITCH", "1").strip().lower() in {"0", "false", "no", "off"}:
        return False
    if not actual_profile or not expected_profile:
        return False
    try:
        xhs_root = os.path.abspath(os.path.join(REPO_ROOT, "browser_data", "profiles", "xiaohongshu"))
        actual = os.path.abspath(actual_profile)
        expected = os.path.abspath(expected_profile)
        return (
            os.path.commonpath([xhs_root, actual]) == xhs_root
            and os.path.commonpath([xhs_root, expected]) == xhs_root
            and not _same_windows_path(actual, expected)
        )
    except Exception:
        return False


def _handle_same_platform_profile_switch(
    platform: str,
    pid: Optional[int],
    actual_profile: str,
    expected_profile: str,
) -> bool:
    """Close the current managed XHS Chrome so the selected account profile can start."""
    if _split_browser_resource(platform)[0] != "xhs" or not pid:
        return False
    if not _xhs_safe_profile_switch_allowed(actual_profile, expected_profile):
        return False
    log.info(
        f"小红书账号池需要切换 profile，正在安全关闭旧 CDP: "
        f"pid={pid}, actual={actual_profile}, expected={expected_profile}"
    )
    transition_browser_session(
        platform,
        "QUIESCING",
        desired_visibility="silent",
        pid=pid,
        reason="safe_profile_switch",
        metadata={"actual_profile": actual_profile, "expected_profile": expected_profile},
        event="browser_safe_profile_switch",
    )
    record_browser_event(
        platform,
        "safe_profile_switch_started",
        {"pid": pid, "actual_profile": actual_profile, "expected_profile": expected_profile},
    )
    try:
        _close_chrome_via_cdp(platform)
    except Exception:
        pass
    deadline = time.time() + 8
    while time.time() < deadline:
        if not _find_chrome_with_debug_port(platform, allow_legacy_edge=True):
            break
        time.sleep(0.25)
    if _find_chrome_with_debug_port(platform, allow_legacy_edge=True):
        _force_kill_chrome_tree(platform, pid, "safe_profile_switch")
    _MANAGED_CHROME_PROCS.pop(platform, None)
    _MANAGED_CHROME_PIDS.pop(platform, None)
    _MANAGED_CHROME_PROFILE_DIRS.pop(platform, None)
    _MANAGED_CHROME_OWNED_PIDS.discard(pid)
    return True


def _retire_legacy_edge_session(platform: str, pid: int, expected_profile: str) -> bool:
    """Safely retire a formerly managed Edge process before starting Chrome.

    This migration path is deliberately narrow.  It only operates on a process
    already proven to own the platform CDP port and whose profile is either the
    requested profile or an allowed XHS account-pool switch.  It never searches
    for, or closes, ordinary user Edge windows.
    """

    actual_profile = _chrome_user_data_dir_for_pid(pid)
    if not actual_profile:
        return False
    same_profile = _same_windows_path(actual_profile, expected_profile)
    safe_switch = _split_browser_resource(platform)[0] == "xhs" and _xhs_safe_profile_switch_allowed(actual_profile, expected_profile)
    if not (same_profile or safe_switch):
        log.warning(
            "拒绝收尾旧 Edge 会话：受管资料目录不匹配: platform=%s, pid=%s",
            platform,
            pid,
        )
        return False

    log.info("检测到旧受管 Edge，会安全收尾后改用 Google Chrome: platform=%s, pid=%s", platform, pid)
    transition_browser_session(
        platform,
        "QUIESCING",
        desired_visibility="silent",
        pid=pid,
        reason="legacy_edge_migration",
        metadata={"browser_family": "microsoft_edge", "actual_profile": actual_profile, "expected_profile": expected_profile},
        event="legacy_edge_retirement_started",
    )
    try:
        _close_chrome_via_cdp(platform)
    except Exception:
        pass
    deadline = time.time() + 8
    while time.time() < deadline:
        if not _find_chrome_with_debug_port(platform, allow_legacy_edge=True):
            break
        time.sleep(0.25)
    if _find_chrome_with_debug_port(platform, allow_legacy_edge=True):
        _force_kill_chrome_tree(platform, pid, "legacy_edge_migration")
    retired = not _find_chrome_with_debug_port(platform, allow_legacy_edge=True)
    if retired:
        _MANAGED_CHROME_PROCS.pop(platform, None)
        _MANAGED_CHROME_PIDS.pop(platform, None)
        _MANAGED_CHROME_PROFILE_DIRS.pop(platform, None)
        _MANAGED_CHROME_OWNED_PIDS.discard(pid)
        record_browser_event(platform, "legacy_edge_retired", {"pid": pid, "browser_family": "microsoft_edge"})
    return retired


def _inject_stealth_js(debug_port: str) -> bool:
    """Inject stealth.js anti-debug script via CDP for BOSS直聘."""
    try:
        if not os.path.isfile(BOSS_STEALTH_JS_PATH):
            log.warning(f"stealth.js 不存在: {BOSS_STEALTH_JS_PATH}")
            return False
        with open(BOSS_STEALTH_JS_PATH, "r", encoding="utf-8") as f:
            js_source = f.read()
        inject_code = r"""
        (async () => {
          const port = process.argv[1];
          const jsSource = process.argv[2];
          const version = await fetch(`http://127.0.0.1:${port}/json/version`).then(r => r.json());
          const ws = new WebSocket(version.webSocketDebuggerUrl);
          await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
          let seq = 0;
          const pending = new Map();
          ws.onmessage = event => {
            const message = JSON.parse(event.data);
            if (message.id && pending.has(message.id)) {
              pending.get(message.id)(message);
              pending.delete(message.id);
            }
          };
          const send = (method, params) => new Promise(resolve => {
            const id = ++seq;
            pending.set(id, resolve);
            ws.send(JSON.stringify({ id, method, params }));
          });
          await send('Page.enable', {});
          const result = await send('Page.addScriptToEvaluateOnNewDocument', { source: jsSource });
          console.log(JSON.stringify({ ok: true, identifier: result.result?.identifier }));
          ws.close();
        })().catch(error => {
          console.error(error && error.stack ? error.stack : String(error));
          process.exit(1);
        });
        """
        proc = silent_subprocess_run(
            ["node", "-e", inject_code, str(debug_port), js_source],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if proc.returncode == 0:
            log.info("stealth.js 注入成功 (BOSS直聘反调试)")
            return True
        log.warning(f"stealth.js 注入失败: {proc.stderr.strip()}")
        return False
    except Exception as e:
        log.warning(f"stealth.js 注入异常: {e}")
        return False


def _ensure_chrome_debugging_locked(
    platform: str = "xhs",
    *,
    visible: bool = False,
    detach: bool = False,
    target_profile_id: str = "",
) -> bool:
    import urllib.request

    global _MANAGED_CHROME_PROCS, _MANAGED_CHROME_PROFILE_DIRS, _MANAGED_CHROME_PIDS, _MANAGED_CHROME_OWNED_PIDS
    base_platform, embedded_profile_id = _split_browser_resource(platform)
    target_profile_id = target_profile_id or embedded_profile_id
    profile_dir = _managed_chrome_profile_dir(platform, target_profile_id=target_profile_id)
    existing_pid = _find_chrome_with_debug_port(platform)
    if base_platform == "xhs" and not target_profile_id and existing_pid:
        active_dir = _chrome_user_data_dir_for_pid(existing_pid)
        if _profile_metadata_for_platform(platform, profile_dir=active_dir):
            # Reuse the already selected project profile. Failover always passes an
            # explicit target below, so this cannot hide a requested switch.
            profile_dir = active_dir
    if base_platform == "xhs" and not profile_dir:
        log.warning("XHS 没有 main_chain_allowed profile，拒绝自动启动旧默认 profile")
        transition_browser_session(
            platform,
            "FAILED",
            desired_visibility="silent",
            reason="no_main_chain_allowed_profile",
            metadata={"entrypoint": "_ensure_chrome_debugging"},
            event="browser_start_blocked",
        )
        return False
    debug_port = _chrome_debug_port(platform)
    debug_url = _chrome_debug_url(platform)
    profile_meta = _profile_metadata_for_platform(
        platform,
        profile_dir=profile_dir,
        target_profile_id=target_profile_id,
    )
    if not existing_pid:
        legacy_edge_pid = _find_chrome_with_debug_port(platform, allow_legacy_edge=True)
        if legacy_edge_pid and not _retire_legacy_edge_session(platform, legacy_edge_pid, profile_dir):
            transition_browser_session(
                platform,
                "FAILED",
                desired_visibility="silent",
                pid=legacy_edge_pid,
                reason="legacy_edge_migration_rejected",
                metadata={"target_profile_id": target_profile_id, "browser_family": "microsoft_edge"},
                event="legacy_edge_migration_rejected",
                profile_id=profile_meta.get("profile_id", ""),
                profile_dir=profile_dir,
            )
            return False
    upsert_browser_session(
        platform=platform,
        debug_port=debug_port,
        profile_dir=profile_dir,
        profile_id=profile_meta.get("profile_id", ""),
        account_slot=profile_meta.get("account_slot", ""),
        state="STARTING_SILENT",
        desired_visibility="silent",
        reason="ensure_chrome_debugging",
        target_url=_startup_url_for_platform(platform),
        metadata={
            "entrypoint": "_ensure_chrome_debugging",
            "channel_id": profile_meta.get("channel_id", ""),
            "account_identity": {
                "display_label": profile_meta.get("display_label", ""),
                "masked_hint": profile_meta.get("masked_hint", ""),
            },
        },
        event="browser_ensure_started",
    )

    try:
        req = urllib.request.Request(
            f"{debug_url}/json/version",
            headers={"User-Agent": "Mozilla/5.0"},
            method="GET",
        )
        resp = urllib.request.urlopen(req, timeout=3)
        data = json.loads(resp.read().decode())
        expected_profile = os.path.abspath(profile_dir).lower()
        port_pid = _find_chrome_with_debug_port(platform)
        actual_profile = _chrome_user_data_dir_for_pid(port_pid).lower() if port_pid else ""
        # The listening Chrome process is authoritative.  A remembered directory
        # is only local bookkeeping and can be stale after an account switch.
        if _same_windows_path(actual_profile, expected_profile):
            log.info(f"Chrome 调试端口已就绪: {data.get('Browser', '?')}")
            if port_pid:
                _MANAGED_CHROME_PIDS[platform] = port_pid
                if visible or _CHROME_KEEP_ALIVE.get(platform, False):
                    _bring_chrome_windows_to_front(port_pid)
                    desired_visibility = "attention"
                    ready_state = "USER_INTERACTING"
                    ready_event = "browser_reused_for_manual_interaction"
                else:
                    _minimize_chrome_windows(port_pid)
                    desired_visibility = "silent"
                    ready_state = "READY_SILENT"
                    ready_event = "browser_reused_ready"
                transition_browser_session(
                    platform,
                    ready_state,
                    desired_visibility=desired_visibility,
                    pid=port_pid,
                    metadata={"reuse": True, "browser": data.get("Browser", "?")},
                    event=ready_event,
                )
            _MANAGED_CHROME_PROFILE_DIRS[platform] = profile_dir
            _schedule_chrome_idle_cleanup(platform)
            if platform == "zhihu":
                _start_zhihu_session_refresh()
            return True
        if actual_profile:
            if _handle_same_platform_profile_switch(platform, port_pid, actual_profile, expected_profile):
                raise RuntimeError("profile_switch_restart_requested")
            log.warning(
                f"Chrome 调试端口由其他 profile 占用，拒绝自动回落或清理: "
                f"expected={expected_profile}, actual={actual_profile}"
            )
            transition_browser_session(
                platform,
                "FAILED",
                desired_visibility="silent",
                reason="profile_mismatch",
                pid=port_pid,
                metadata={"reuse": False, "has_actual_profile": True},
                event="browser_profile_mismatch",
            )
            return False
        else:
            if base_platform == "xhs" and target_profile_id:
                log.warning(
                    "无法确认指定小红书 profile 的实际浏览器目录，拒绝将其用于账号认领或登录恢复: "
                    f"profile_id={target_profile_id}"
                )
                transition_browser_session(
                    platform,
                    "FAILED",
                    desired_visibility="silent",
                    reason="target_profile_unconfirmed",
                    pid=port_pid,
                    metadata={"reuse": False, "profile_unconfirmed": True},
                    event="browser_target_profile_unconfirmed",
                )
                return False
            log.info("Chrome 调试端口已就绪，但无法从进程命令行确认 profile；继续复用现有 CDP")
            transition_browser_session(
                platform,
                "READY_SILENT",
                desired_visibility="silent",
                pid=port_pid,
                metadata={"reuse": True, "profile_unconfirmed": True},
                event="browser_reused_profile_unconfirmed",
            )
            if platform == "zhihu":
                _start_zhihu_session_refresh()
            return True
    except Exception:
        pass

    chrome_selection = resolve_managed_chrome()
    chrome_exe = chrome_selection.path if chrome_selection else ""
    if not chrome_exe:
        raise RuntimeError("未找到受管 Google Chrome。请安装 Chrome 或设置 KR_CHROME_EXE；系统不会自动改用 Edge。")

    existing = _find_chrome_with_debug_port(platform)
    if existing:
        log.info(f"检测到已有 Chrome 调试进程 (PID={existing}), 快速探测端口...")
        wait_attempts = 20
        for i in range(wait_attempts):
            try:
                req = urllib.request.Request(
                    f"{debug_url}/json/version",
                    headers={"User-Agent": "Mozilla/5.0"},
                    method="GET",
                )
                urllib.request.urlopen(req, timeout=1)
                expected_profile = os.path.abspath(profile_dir).lower()
                actual_profile = _chrome_user_data_dir_for_pid(existing).lower()
                if actual_profile and not _same_windows_path(actual_profile, expected_profile):
                    if _handle_same_platform_profile_switch(platform, existing, actual_profile, expected_profile):
                        break
                    log.warning(
                        f"Chrome 调试进程 profile 不匹配，拒绝自动回落或清理: "
                        f"expected={expected_profile}, actual={actual_profile}"
                    )
                    return False
                log.info(f"Chrome 调试端口就绪 (等待 {i+1}s)")
                _MANAGED_CHROME_PIDS[platform] = existing
                _MANAGED_CHROME_PROFILE_DIRS[platform] = profile_dir
                if visible or _CHROME_KEEP_ALIVE.get(platform, False):
                    _bring_chrome_windows_to_front(existing)
                    desired_visibility = "attention"
                    ready_state = "USER_INTERACTING"
                    ready_event = "browser_existing_for_manual_interaction"
                else:
                    _minimize_chrome_windows(existing)
                    desired_visibility = "silent"
                    ready_state = "READY_SILENT"
                    ready_event = "browser_existing_ready"
                transition_browser_session(
                    platform,
                    ready_state,
                    desired_visibility=desired_visibility,
                    pid=existing,
                    metadata={"reuse": True, "wait_seconds": i + 1},
                    event=ready_event,
                )
                _schedule_chrome_idle_cleanup(platform)
                if platform == "zhihu":
                    _start_zhihu_session_refresh()
                return True
            except Exception:
                time.sleep(0.25)
        log.warning(f"Chrome 调试进程疑似僵死，快速清理后重启: platform={platform}, pid={existing}")
        try:
            silent_subprocess_run(
                ["taskkill", "/PID", str(existing), "/T", "/F"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
        except Exception as e:
            log.debug(f"清理僵死 Chrome 失败: {e}")

    # Clean up any stale Chrome processes using this profile before launching
    _cleanup_stale_chrome(platform)

    os.makedirs(profile_dir, exist_ok=True)
    profile_name = _managed_chrome_profile_name(platform)
    _mark_profile_exit_clean(profile_dir, profile_name)
    chrome_profile_arg = _chrome_arg_path(profile_dir)

    startup_url = _startup_url_for_platform(platform)
    cmd = [
        chrome_exe,
        f"--remote-debugging-port={debug_port}",
        f"--user-data-dir={chrome_profile_arg}",
        f"--profile-directory={profile_name}",
        f"--window-size={_CHROME_WINDOW_SIZE}",
        f"--remote-allow-origins=http://127.0.0.1:{debug_port},http://localhost:{debug_port},devtools://devtools",
        "--lang=zh-CN",
        "--disable-session-crashed-bubble",
        "--no-session-restore",
        "--no-first-run",
        "--no-default-browser-check",
        startup_url,
    ]
    for flag in reversed(_chrome_stealth_flags_for_platform(platform)):
        cmd.insert(7, flag)
    if visible or _CHROME_KEEP_ALIVE.get(platform, False):
        cmd.insert(5, f"--window-position={_CHROME_VISIBLE_WINDOW_POSITION}")
    else:
        cmd.insert(8, "--start-in-background")  # 启动后不抢焦点，避免闪屏
    if _CHROME_SILENT_OFFSCREEN and not (visible or _CHROME_KEEP_ALIVE.get(platform, False)):
        cmd.insert(5, f"--window-position={_CHROME_SILENT_WINDOW_POSITION}")
    log.info(
        "自动启动受管浏览器: family=%s, selection_source=%s, command=%s",
        chrome_selection.family,
        chrome_selection.selection_source,
        " ".join(cmd),
    )

    try:
        creationflags = 0
        if os.name == "nt" and detach:
            creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        proc = silent_subprocess_popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            startupinfo=None if (visible or _CHROME_KEEP_ALIVE.get(platform, False)) else _chrome_startupinfo(),
            creationflags=creationflags,
        )
        if not detach:
            _MANAGED_CHROME_PROCS[platform] = proc
            _MANAGED_CHROME_OWNED_PIDS.add(proc.pid)
        _MANAGED_CHROME_PIDS[platform] = proc.pid
        _MANAGED_CHROME_PROFILE_DIRS[platform] = profile_dir
        upsert_browser_session(
            platform=platform,
            debug_port=debug_port,
            profile_dir=profile_dir,
            state="USER_INTERACTING" if (visible or _CHROME_KEEP_ALIVE.get(platform, False)) else "STARTING_SILENT",
            desired_visibility="attention" if (visible or _CHROME_KEEP_ALIVE.get(platform, False)) else "silent",
            pid=proc.pid,
            reason="process_launched",
            target_url=startup_url,
            metadata={
                "owned": not detach,
                "detached": detach,
                "browser_executable": chrome_selection.path,
                "browser_family": chrome_selection.family,
                "selection_source": chrome_selection.selection_source,
            },
            event="browser_process_launched",
        )
        if visible or _CHROME_KEEP_ALIVE.get(platform, False):
            _bring_chrome_windows_to_front(proc.pid)
        else:
            # --start-in-background 已确保不闪屏，仅保留最小化作为兜底
            _minimize_chrome_windows(proc.pid)
        log.info("受管 Google Chrome 进程已启动 (PID=%s)", proc.pid)
    except Exception as e:
        log.error(f"Chrome 启动失败: {e}")
        return False

    for i in range(20):
        try:
            req = urllib.request.Request(
                f"{debug_url}/json/version",
                headers={"User-Agent": "Mozilla/5.0"},
                method="GET",
            )
            urllib.request.urlopen(req, timeout=3)
            log.info(f"Chrome 调试端口就绪 (启动耗时 ~{i+1}s)")
            if visible or _CHROME_KEEP_ALIVE.get(platform, False):
                _bring_chrome_windows_to_front(proc.pid)
                desired_visibility = "attention"
                ready_state = "USER_INTERACTING"
                ready_event = "browser_launch_ready_for_manual_interaction"
            else:
                _minimize_chrome_windows(proc.pid)
                desired_visibility = "silent"
                ready_state = "READY_SILENT"
                ready_event = "browser_launch_ready"
            transition_browser_session(
                platform,
                ready_state,
                desired_visibility=desired_visibility,
                pid=proc.pid,
                metadata={
                    "owned": not detach,
                    "detached": detach,
                    "startup_seconds": i + 1,
                    "browser_executable": chrome_selection.path,
                    "browser_family": chrome_selection.family,
                    "selection_source": chrome_selection.selection_source,
                },
                event=ready_event,
            )
            _schedule_chrome_idle_cleanup(platform)
            if platform == "zhihu":
                _start_zhihu_session_refresh()
            # BOSS直聘：注入 stealth.js 反调试脚本
            if platform == "boss":
                _inject_stealth_js(debug_port)
            return True
        except Exception:
            time.sleep(1)

    log.warning("Chrome 已启动但调试端口未就绪，将继续尝试搜索...")
    transition_browser_session(
        platform,
        "FAILED",
        desired_visibility="silent",
        pid=_MANAGED_CHROME_PIDS.get(platform),
        reason="cdp_port_not_ready",
        metadata={"debug_port": debug_port},
        event="browser_launch_cdp_not_ready",
    )
    return False


# ── 知乎会话定期刷新 ──────────────────────────────────────────────────
_zhihu_refresh_timer: Optional[threading.Timer] = None
_ZHIHU_REFRESH_INTERVAL = 6 * 3600  # 6 小时


def _start_zhihu_session_refresh() -> None:
    """启动知乎会话定期刷新（每 6 小时访问一次知乎首页）。"""
    global _zhihu_refresh_timer

    def _refresh() -> None:
        try:
            # 检查知乎 Chrome 是否在运行
            zhihu_pid = _MANAGED_CHROME_PIDS.get("zhihu")
            if not zhihu_pid:
                log.debug("知乎 Chrome 未运行，跳过会话刷新")
                return

            # 通过 CDP 获取页面列表
            import urllib.request
            debug_url = _chrome_debug_url("zhihu")
            req = urllib.request.Request(
                f"{debug_url}/json",
                headers={"User-Agent": "Mozilla/5.0"},
                method="GET",
            )
            resp = urllib.request.urlopen(req, timeout=5)
            tabs = json.loads(resp.read().decode())

            if not tabs:
                log.debug("知乎 Chrome 无页面，跳过会话刷新")
                return

            # 使用 node 脚本通过 CDP 导航到知乎首页
            ws_url = tabs[0].get("webSocketDebuggerUrl")
            if not ws_url:
                log.debug("知乎 Chrome 无 WebSocket URL，跳过会话刷新")
                return

            js_code = """
            const ws = new (require('ws'))(process.argv[1]);
            ws.on('open', () => {
                ws.send(JSON.stringify({
                    id: 1,
                    method: 'Page.navigate',
                    params: { url: 'https://www.zhihu.com' }
                }));
                setTimeout(() => { ws.close(); process.exit(0); }, 2000);
            });
            ws.on('error', (err) => { console.error(err.message); process.exit(1); });
            """
            proc = silent_subprocess_run(
                ["node", "-e", js_code, ws_url],
                capture_output=True,
                text=True,
                timeout=15,
                cwd=REPO_ROOT,
            )
            if proc.returncode == 0:
                log.info("知乎会话刷新成功")
            else:
                log.warning(f"知乎会话刷新失败: {proc.stderr.strip()[:200]}")
        except Exception as e:
            log.warning(f"知乎会话刷新异常: {e}")
        finally:
            # 重新调度下一次刷新
            _start_zhihu_session_refresh()

    _zhihu_refresh_timer = threading.Timer(_ZHIHU_REFRESH_INTERVAL, _refresh)
    _zhihu_refresh_timer.daemon = True
    _zhihu_refresh_timer.start()
    log.info(f"知乎会话定期刷新已启动（间隔 {_ZHIHU_REFRESH_INTERVAL // 3600} 小时）")


def _chrome_arg_path(path: str) -> str:
    """Return a Chrome-safe Windows path, avoiding spaces in --user-data-dir."""
    abs_path = os.path.abspath(path)
    if os.name != "nt" or " " not in abs_path:
        return abs_path
    try:
        import ctypes
        from ctypes import wintypes

        get_short_path_name = ctypes.windll.kernel32.GetShortPathNameW
        get_short_path_name.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        get_short_path_name.restype = wintypes.DWORD
        size = get_short_path_name(abs_path, None, 0)
        if size:
            buffer = ctypes.create_unicode_buffer(size)
            if get_short_path_name(abs_path, buffer, size):
                return buffer.value
    except Exception as e:
        log.debug(f"获取 Chrome 参数短路径失败: {e}")
    return abs_path


def _same_windows_path(left: str, right: str) -> bool:
    if not left or not right:
        return False
    try:
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))
    except Exception:
        return left.strip().lower() == right.strip().lower()


def _mark_profile_exit_clean(profile_dir: str, profile_name: str = "Default") -> None:
    """Avoid Chrome restoring stale crash tabs from prior debugging runs."""
    pref_path = os.path.join(profile_dir, profile_name or "Default", "Preferences")
    if not os.path.isfile(pref_path):
        return
    try:
        with open(pref_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        changed = False
        if data.get("exit_type") == "Crashed":
            data["exit_type"] = "Normal"
            changed = True
        profile = data.get("profile")
        if isinstance(profile, dict) and profile.get("exit_type") == "Crashed":
            profile["exit_type"] = "Normal"
            changed = True
        if changed:
            with open(pref_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        log.debug(f"标记 Chrome profile clean exit 失败: {e}")


def _find_chrome_exe() -> Optional[str]:
    """Locate the single policy-approved Google Chrome executable for CDP."""
    selection = resolve_managed_chrome()
    if selection:
        log.info(
            "找到受管 Google Chrome: path=%s, selection_source=%s",
            selection.path,
            selection.selection_source,
        )
        return selection.path
    log.error("未找到受管 Google Chrome: %s", managed_chrome_resolution_summary())
    return None


def _chrome_user_data_dir_for_pid(pid: Optional[int]) -> str:
    if not pid:
        return ""
    try:
        result = silent_subprocess_run(
            ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        command_line = " ".join(line.strip() for line in result.stdout.splitlines() if line.strip())
        for known_profile in (
            XHS_USER_DATA_DIR,
            ZHIHU_USER_DATA_DIR,
            XHS_CHROME_USER_DATA_DIR,
            ZHIHU_CHROME_USER_DATA_DIR,
            BOSS_CHROME_USER_DATA_DIR,
            MANAGED_CHROME_USER_DATA_DIR,
        ):
            if known_profile and known_profile in command_line:
                return os.path.abspath(known_profile)
        match = re.search(r'--user-data-dir=(?:"([^"]+)"|([^\s]+))', command_line)
        if match:
            return os.path.abspath(match.group(1) or match.group(2) or "")
    except Exception as e:
        log.debug(f"读取 Chrome user-data-dir 失败: {e}")
    return ""


def _find_chrome_with_debug_port(platform: str = "xhs", *, allow_legacy_edge: bool = False) -> Optional[int]:
    """Return the PID of a Chrome process exposing the platform CDP port.

    Strategy: netstat to find the PID listening on the port, then wmic to verify
    the process name is chrome.exe (not a wrapper that happened to contain the
    port).  ``allow_legacy_edge`` exists only for safe cleanup of a previously
    managed Edge process; normal reuse never accepts Edge.
    """
    debug_port = _chrome_debug_port(platform)
    try:
        # Step 1: netstat to find the PID actually listening on the port
        netstat = silent_subprocess_run(
            ["netstat", "-ano"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
        )
        candidate_pid = None
        for line in netstat.stdout.splitlines():
            if f":{debug_port}" in line and "LISTENING" in line:
                parts = line.strip().split()
                try:
                    candidate_pid = int(parts[-1])
                except (ValueError, IndexError):
                    continue
                break

        if not candidate_pid:
            return None

        accepted_names = {"chrome.exe", "msedge.exe"} if allow_legacy_edge else {"chrome.exe"}
        # Step 2: verify the PID is the accepted browser family via wmic
        wmic = silent_subprocess_run(
            ["wmic", "process", "where", f"ProcessId={candidate_pid}", "get", "Name"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
        )
        for line in wmic.stdout.strip().splitlines():
            name = line.strip().lower()
            if name in accepted_names:
                return candidate_pid

        # Step 3: fallback — wmic command-line scan (strict name check)
        result = silent_subprocess_run(
            [
                "wmic",
                "process",
                "where",
                f'commandline like "%--remote-debugging-port={debug_port}%"',
                "get",
                "Name,ProcessId",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if not line or line.lower().startswith("name"):
                continue
            match = re.match(r"(chrome\.exe|msedge\.exe)\s+(\d+)", line, re.IGNORECASE)
            if match and match.group(1).lower() in accepted_names:
                return int(match.group(2))
    except Exception:
        pass
    return None


def _find_chrome_with_profile_dir(profile_dir: str) -> Optional[int]:
    """Return a Chrome root PID using a profile dir, including non-CDP manual windows."""
    if not profile_dir or os.name != "nt":
        return None
    expected = os.path.normcase(os.path.abspath(profile_dir)).lower()

    def _root_from_rows(rows: list[Dict]) -> Optional[int]:
        candidates: list[tuple[int, int]] = []
        for row in rows:
            try:
                command_line = str(row.get("CommandLine") or "")
                lowered = os.path.normcase(command_line).lower()
                if "--user-data-dir" not in lowered or expected not in lowered:
                    continue
                pid = int(row.get("ProcessId") or 0)
                parent_pid = int(row.get("ParentProcessId") or 0)
                if pid:
                    candidates.append((parent_pid, pid))
            except Exception:
                continue
        if not candidates:
            return None
        child_pids = {pid for _parent, pid in candidates}
        roots = [pid for parent, pid in candidates if parent not in child_pids]
        return roots[0] if roots else candidates[0][1]

    try:
        result = silent_subprocess_run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "Get-CimInstance Win32_Process | "
                    "Where-Object { $_.Name -in @('chrome.exe','msedge.exe') } | "
                    "Select-Object ProcessId,ParentProcessId,CommandLine | "
                    "ConvertTo-Json -Compress"
                ),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if result.stdout.strip():
            payload = json.loads(result.stdout)
            rows = payload if isinstance(payload, list) else [payload]
            root = _root_from_rows([row for row in rows if isinstance(row, dict)])
            if root:
                return root
    except Exception as exc:
        log.debug(f"PowerShell 按 profile 查找 Chrome 失败: {exc}")

    try:
        result = silent_subprocess_run(
            [
                "wmic",
                "process",
                "where",
                "name='chrome.exe' or name='msedge.exe'",
                "get",
                "ProcessId,ParentProcessId,CommandLine",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        rows = []
        for line in result.stdout.splitlines():
            lowered = os.path.normcase(line).lower()
            if "--user-data-dir" not in lowered or expected not in lowered:
                continue
            parts = line.strip().split()
            numeric = [int(part) for part in parts if part.isdigit()]
            if len(numeric) >= 2:
                parent_pid, pid = numeric[-2], numeric[-1]
                rows.append({"ParentProcessId": parent_pid, "ProcessId": pid, "CommandLine": line})
        return _root_from_rows(rows)
    except Exception as exc:
        log.debug(f"按 profile 查找 Chrome 失败: {exc}")
        return None
