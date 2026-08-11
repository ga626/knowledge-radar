"""Local, non-secret Xiaohongshu account labels and collision fingerprints."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from pathlib import Path
import secrets
import tempfile
from typing import Any, Dict

from .paths import runtime_state_dir


IDENTITY_SCHEMA_VERSION = "knowledgeradar-xhs-account-identities/v1"


def default_identity_path() -> Path:
    return runtime_state_dir() / "xhs-account-identities.json"


def default_identity_key_path() -> Path:
    return runtime_state_dir() / "xhs-account-identities.key"


def _load(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {"schema_version": IDENTITY_SCHEMA_VERSION, "profiles": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"schema_version": IDENTITY_SCHEMA_VERSION, "profiles": []}
    return data if isinstance(data, dict) else {"schema_version": IDENTITY_SCHEMA_VERSION, "profiles": []}


def _write(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _fingerprint_key(path: Path) -> bytes:
    if path.is_file():
        return path.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_bytes(32)
    path.write_bytes(value)
    return value


def account_identity_fingerprint(user_id: str, *, key_path: Path | None = None) -> str:
    """Return an irreversible local collision key without storing the raw user id."""
    value = str(user_id or "").strip()
    if not value:
        return ""
    return hmac.new(_fingerprint_key(key_path or default_identity_key_path()), value.encode("utf-8"), hashlib.sha256).hexdigest()[:24]


def identity_for_profile(profile_id: str, *, path: Path | None = None) -> Dict[str, Any]:
    profile_id = str(profile_id or "").strip()
    for item in _load(path or default_identity_path()).get("profiles", []):
        if isinstance(item, dict) and str(item.get("profile_id") or "") == profile_id:
            return dict(item)
    return {}


def xhs_account_identity_summary(*, path: Path | None = None) -> Dict[str, Any]:
    data = _load(path or default_identity_path())
    profiles = []
    for item in data.get("profiles", []):
        if not isinstance(item, dict):
            continue
        profiles.append(
            {
                "profile_id": str(item.get("profile_id") or ""),
                "account_slot": str(item.get("account_slot") or ""),
                "profile_dir_hash": str(item.get("profile_dir_hash") or ""),
                "display_label": str(item.get("display_label") or ""),
                "masked_hint": str(item.get("masked_hint") or ""),
                "identity_verification": str(item.get("identity_verification") or "platform_verified"),
                "identity_fingerprint": str(item.get("identity_fingerprint") or ""),
                "claimed_at": str(item.get("claimed_at") or ""),
            }
        )
    return {"schema_version": IDENTITY_SCHEMA_VERSION, "status": "ok", "profiles": profiles}


def claim_xhs_account_identity(
    *,
    profile_id: str,
    account_slot: str,
    profile_dir_hash: str,
    display_label: str,
    nickname: str,
    user_id: str,
    masked_hint: str = "",
    allow_user_confirmed_without_identity: bool = False,
    path: Path | None = None,
    key_path: Path | None = None,
) -> Dict[str, Any]:
    """Persist user-confirmed labels; raw platform IDs never leave this call."""
    label = str(display_label or "").strip()
    if not label:
        return {"status": "blocked", "reason_code": "DISPLAY_LABEL_REQUIRED"}
    if len(label) > 80 or len(str(masked_hint or "")) > 40:
        return {"status": "blocked", "reason_code": "DISPLAY_LABEL_TOO_LONG"}
    fingerprint = account_identity_fingerprint(user_id, key_path=key_path)
    if not fingerprint and not allow_user_confirmed_without_identity:
        return {"status": "blocked", "reason_code": "AUTHENTICATED_IDENTITY_REQUIRED"}

    target = path or default_identity_path()
    data = _load(target)
    rows = [dict(item) for item in data.get("profiles", []) if isinstance(item, dict)]
    conflict = next(
        (
            item
            for item in rows
            if fingerprint
            and str(item.get("identity_fingerprint") or "") == fingerprint
            and str(item.get("profile_id") or "") != str(profile_id or "")
        ),
        None,
    )
    if conflict:
        return {
            "status": "blocked",
            "reason_code": "IDENTITY_ALREADY_CLAIMED_BY_ANOTHER_PROFILE",
            "conflicting_profile_id": str(conflict.get("profile_id") or ""),
        }

    record = {
        "profile_id": str(profile_id or ""),
        "account_slot": str(account_slot or ""),
        "profile_dir_hash": str(profile_dir_hash or ""),
        "display_label": label,
        "masked_hint": str(masked_hint or "").strip(),
        "identity_verification": "platform_verified" if fingerprint else "user_confirmed_pending_platform_proof",
        "identity_fingerprint": fingerprint,
        "claimed_at": datetime.now(timezone.utc).isoformat(),
    }
    rows = [item for item in rows if str(item.get("profile_id") or "") != record["profile_id"]]
    rows.append(record)
    _write(target, {"schema_version": IDENTITY_SCHEMA_VERSION, "profiles": rows})
    return {
        "status": "ok",
        "profile_id": record["profile_id"],
        "account_slot": record["account_slot"],
        "display_label": record["display_label"],
        "masked_hint": record["masked_hint"],
        "observed_nickname": str(nickname or "").strip(),
        "identity_verification": record["identity_verification"],
        "identity_fingerprint": fingerprint,
    }
