"""只在实际受管Profile和真实业务结果匹配时登记可用，不读取缓存赋绿。"""
from pathlib import Path
import os
from .account_observations import observe_account


def observe_business(platform, result, *, profile_dir, source):
    if not isinstance(result, dict) or result.get("error") or result.get("failure_type") or result.get("status") in {"failed", "error", "empty", "needs_interaction"} or result.get("manual_action_required") or not profile_dir:
        return result
    metadata = result.get("metadata") or {}
    if metadata.get("cache_hit") or metadata.get("fallback_from") or metadata.get("fallback_used"):
        return result
    substantive = bool(result.get("items")) or len(str(result.get("content") or result.get("desc") or "").strip()) >= 80
    if not substantive:
        return result
    from .profile_registry import raw_registry_for_platform
    actual = os.path.normcase(str(Path(profile_dir).resolve()))
    profiles = raw_registry_for_platform(platform).get("profiles", [])
    matches = [p for p in profiles if p.get("profile_id") and p.get("profile_dir") and
               os.path.normcase(str(Path(os.path.expandvars(os.path.expanduser(p["profile_dir"]))).resolve())) == actual]
    if len(matches) == 1:
        fact = observe_account(matches[0]["profile_id"], platform, "business_verified", source=source)
        result = {**result, "account_observation": fact}
    return result
