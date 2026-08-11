"""Open foreground Chrome windows for manual academic-platform login probes.

This script mirrors KnowledgeRadar's persistent-profile style without admitting
these platforms into the automatic main chain. Cookies stay inside
browser_data/profiles/<platform>/account_a and are not copied into config.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from runtime.chrome_manager import complete_browser_interaction, probe_browser_auth, request_browser_interaction  # noqa: E402
from runtime.paths import browser_data_dir  # noqa: E402


LOGIN_TARGETS: dict[str, dict[str, str]] = {
    "vip_oa": {
        "url": "https://www.cqvip.com/search?k=ai",
        "note": "VIP/CQVIP metadata is public; intelligent-reading preview needs an authorized official-site session probe.",
    },
    "coaj": {
        "url": "https://www.coaj.cn/login",
        "note": "COAJ public metadata works; authenticated detail/PDF route is unconfirmed.",
    },
    "ucdrs": {
        "url": "http://www.ucdrs.superlib.net/",
        "note": "UCDRS is document delivery, not confirmed unattended direct full text.",
    },
    "calis_thesis": {
        "url": "https://etd2.calis.edu.cn/",
        "note": "CALIS thesis access usually depends on institution or delivery authorization.",
    },
    "nstrs": {
        "url": "https://www.nstrs.cn/cas",
        "note": "NSTRS requires real-name professional registration and states online viewing only.",
    },
    "pubscholar": {
        "url": "https://pubscholar.cn/",
        "note": "PubScholar is a discovery/full-text aggregator candidate; stable PDF extraction is unconfirmed.",
    },
    "socolar": {
        "url": "https://www.socolar.com/",
        "note": "Socolar openAccess API returned a not-logged-in response; validate with an authorized profile before promotion.",
    },
}


def _profile_dir(platform: str) -> Path:
    return browser_data_dir() / "profiles" / platform / "account_a"


def planned_interaction(platform: str) -> dict[str, str]:
    target = LOGIN_TARGETS[platform]
    profile_dir = _profile_dir(platform)
    return {
        "platform": platform,
        "profile_dir": str(profile_dir),
        "target_url": target["url"],
        "probe_mode": f"health_check(mode='probe_browser_auth:{platform}')",
        "mode": f"health_check(mode='request_browser_interaction:{platform}:manual_login')",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--platform",
        action="append",
        choices=[*LOGIN_TARGETS.keys(), "all"],
        help="Platform to open. Repeatable. Defaults to all.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print commands without launching Chrome.")
    parser.add_argument(
        "--request",
        action="store_true",
        help="Open the foreground browser for user login. Default is a silent auth probe.",
    )
    parser.add_argument(
        "--auto-request-if-needed",
        action="store_true",
        help="Run a silent auth probe first; open the browser only when the probe returns needs_interaction.",
    )
    parser.add_argument(
        "--complete",
        action="store_true",
        help="Mark login complete for the selected platform(s), restore normal lifecycle, and close idle Chrome.",
    )
    args = parser.parse_args()

    selected = args.platform or ["all"]
    platforms = list(LOGIN_TARGETS) if "all" in selected else selected

    for platform in platforms:
        profile_dir = _profile_dir(platform)
        profile_dir.mkdir(parents=True, exist_ok=True)
        plan = planned_interaction(platform)
        print(f"[{platform}] {LOGIN_TARGETS[platform]['url']}")
        print(f"  profile_dir={profile_dir}")
        print(f"  note={LOGIN_TARGETS[platform]['note']}")
        if args.dry_run:
            print(f"  auth_probe={plan['probe_mode']}")
            print(f"  managed_interaction={plan['mode']}")
            if args.complete:
                print(f"  complete_interaction=health_check(mode='complete_browser_interaction:{platform}')")
            continue
        if args.complete:
            result = complete_browser_interaction(platform, probe_result={"status": "ok", "note": "manual login completed"})
        elif args.request:
            result = request_browser_interaction(platform, reason="manual_login")
        else:
            result = probe_browser_auth(platform)
            if args.auto_request_if_needed and result.get("status") == "needs_interaction":
                result = request_browser_interaction(platform, reason="manual_login")
        print(f"  status={result.get('status')}")
        print(f"  auth_state={result.get('auth_state', '')}")
        print(f"  manual_action_required={result.get('manual_action_required', '')}")
        print(f"  session_id={result.get('session_id', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
