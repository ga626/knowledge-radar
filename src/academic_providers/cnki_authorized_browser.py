"""CNKI authorized-browser probe helpers.

The module uses KnowledgeRadar's managed Chrome/CDP lifecycle to inspect a
user-authorized CNKI session. It does not bypass captcha, login, IP checks, or
download full text.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Any, Dict, List

from runtime.process import silent_subprocess_run
from runtime.chrome_manager import (
    CNKI_CHROME_DEBUG_PORT,
    CNKI_STARTUP_URL,
    _ensure_chrome_debugging,
    finish_chrome_automation,
)


class CnkiBrowserError(RuntimeError):
    """Raised when the CNKI browser probe cannot execute."""


@dataclass(frozen=True)
class CnkiProbeOptions:
    query: str = ""
    limit: int = 10
    navigate: bool = True
    cleanup: bool = True


def cnki_browser_status(query: str = "", limit: int = 10, *, cleanup: bool = True) -> Dict[str, Any]:
    """Return the current authorized-browser status and visible CNKI metadata.

    This function is intentionally not wired into provider="auto" yet. It is a
    controlled building block for the future explicit CNKI authorized-browser
    provider.
    """
    options = CnkiProbeOptions(query=query, limit=max(1, min(int(limit or 10), 20)), cleanup=cleanup)
    if not _ensure_chrome_debugging("cnki"):
        return {
            "status": "CHROME_UNAVAILABLE",
            "ok": False,
            "platform": "cnki",
            "reason": "managed_chrome_start_failed",
        }
    try:
        return _run_cnki_probe(int(CNKI_CHROME_DEBUG_PORT), options)
    finally:
        if cleanup:
            finish_chrome_automation("cnki", reason="cnki_authorized_browser_probe")


def _run_cnki_probe(port: int, options: CnkiProbeOptions) -> Dict[str, Any]:
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cnki_probe.js")
    payload = json.dumps(
        {
            "startupUrl": CNKI_STARTUP_URL,
            "query": options.query,
            "limit": options.limit,
            "navigate": options.navigate,
        },
        ensure_ascii=False,
    )
    proc = silent_subprocess_run(
        ["node", script_path, str(port), payload],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if proc.returncode != 0:
        raise CnkiBrowserError(proc.stderr.strip() or proc.stdout.strip() or "CNKI probe failed")
    try:
        data = json.loads(proc.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise CnkiBrowserError(f"CNKI probe returned invalid JSON: {proc.stdout[:500]}") from exc
    return _normalize_probe_result(data)


def _normalize_probe_result(data: Dict[str, Any]) -> Dict[str, Any]:
    status = str(data.get("status") or "UNKNOWN").upper()
    items: List[Dict[str, Any]] = data.get("items") if isinstance(data.get("items"), list) else []
    ok = status == "OK"
    return {
        "status": status,
        "ok": ok,
        "platform": "cnki",
        "url": str(data.get("url") or ""),
        "title": str(data.get("title") or ""),
        "items": items,
        "total": len(items),
        "selectors": data.get("selectors") if isinstance(data.get("selectors"), dict) else {},
        "reason": str(data.get("reason") or ""),
        "next_action": _next_action(status),
        "legal_boundary": "authorized browser only; no captcha bypass, account sharing, or bulk full-text download",
    }


def _next_action(status: str) -> str:
    if status in {"CAPTCHA_REQUIRED", "LOGIN_REQUIRED", "NEEDS_USER"}:
        return "ask user to complete CNKI verification/login in the managed Chrome session, then rerun the probe"
    if status == "AUTH_REQUIRED":
        return "confirm institutional or personal CNKI authorization"
    if status == "SCHEMA_CHANGED":
        return "update CNKI selectors before retrying"
    if status == "OK":
        return "metadata visible; citation export/import can proceed"
    return "inspect browser session state and CNKI page"
