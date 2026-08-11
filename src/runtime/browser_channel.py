"""Browser channel descriptors for browser-backed platform probes.

The first version is a descriptor layer only: it does not launch browsers,
switch accounts, or route production traffic. It gives health/admission code a
shared vocabulary instead of repeating browser/profile/channel strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List


@dataclass(frozen=True)
class BrowserChannel:
    channel_id: str
    platform: str
    profile_id: str
    browser_base: str
    automation: str
    role: str
    launch_policy: str
    account_pool_member: bool
    main_chain_allowed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "platform": self.platform,
            "profile_id": self.profile_id,
            "browser_base": self.browser_base,
            "automation": self.automation,
            "role": self.role,
            "launch_policy": self.launch_policy,
            "account_pool_member": self.account_pool_member,
            "main_chain_allowed": self.main_chain_allowed,
        }


def channels_from_profiles(profiles: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    channels: List[Dict[str, Any]] = []
    for profile in profiles or []:
        if not isinstance(profile, dict):
            continue
        explicit_main_chain = profile.get("main_chain_allowed")
        channel = BrowserChannel(
            channel_id=str(profile.get("channel_id") or ""),
            platform=str(profile.get("platform") or ""),
            profile_id=str(profile.get("profile_id") or ""),
            browser_base=str(profile.get("browser_base") or ""),
            automation=_automation_for_channel(str(profile.get("channel_id") or ""), str(profile.get("browser_base") or "")),
            role=str(profile.get("role") or ""),
            launch_policy=str(profile.get("launch_policy") or ""),
            account_pool_member=bool(profile.get("account_pool_member", False)),
            main_chain_allowed=bool(explicit_main_chain) if explicit_main_chain is not None else str(profile.get("role") or "") == "primary",
        )
        channels.append(channel.to_dict())
    return channels


def browser_channel_summary(profiles: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    channels = channels_from_profiles(profiles)
    ownership = browser_ownership_summary(channels)
    return {
        "schema": "knowledgeradar-browser-channel-summary/v1",
        "status": ownership["status"],
        "production_routing": "unchanged",
        "channels": channels,
        "ownership": ownership,
        "counts": {
            "total": len(channels),
            "main_chain_allowed": sum(1 for item in channels if item.get("main_chain_allowed")),
            "account_pool_members": sum(1 for item in channels if item.get("account_pool_member")),
            "orphan_browser_records": ownership["orphan_count"],
        },
    }


def browser_ownership_summary(channels: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    orphan_records: list[Dict[str, Any]] = []
    registered_platforms: set[str] = set()
    for index, channel in enumerate(channels or []):
        platform = str(channel.get("platform") or "")
        if platform:
            registered_platforms.add(platform)
        missing = [
            field
            for field in ("channel_id", "platform", "profile_id", "browser_base", "launch_policy")
            if not str(channel.get(field) or "")
        ]
        if missing:
            orphan_records.append(
                {
                    "index": index,
                    "platform": platform,
                    "profile_id": str(channel.get("profile_id") or ""),
                    "missing": missing,
                }
            )
    return {
        "schema": "knowledgeradar-browser-ownership-summary/v1",
        "status": "ok" if not orphan_records else "degraded",
        "registered_platforms": sorted(registered_platforms),
        "orphan_count": len(orphan_records),
        "orphan_records": orphan_records,
        "policy": "Every browser-backed profile must declare channel_id, platform, profile_id, browser_base, and launch_policy before it is treated as managed.",
    }


def _automation_for_channel(channel_id: str, browser_base: str) -> str:
    text = f"{channel_id} {browser_base}".lower()
    if "playwright" in text:
        if "cdp" in text:
            return "playwright_cdp"
        return "playwright_dom"
    if "camoufox" in text:
        return "playwright_dom"
    if "scrapling" in text or "cdp" in text:
        return "scrapling_cdp"
    return "unknown"
