"""Profile-scoped, event-driven Xiaohongshu authentication recovery.

The watcher never performs a search, extracts content, or reads cookie values.
It only observes the already opened, user-authorized CDP page and asks the
existing platform auth probe whether the user has completed login/verification.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Dict

import httpx
import websocket

from .browser_sessions import browser_sessions_summary, transition_browser_session


log = logging.getLogger("mcp-server")
_WATCHERS: Dict[str, "XhsAuthWatcher"] = {}
_LOCK = threading.RLock()
_EVENT_METHODS = {"Page.frameNavigated", "Page.lifecycleEvent", "DOM.documentUpdated"}


class XhsAuthWatcher:
    """One durable CDP observer for exactly one XHS profile."""

    def __init__(self, profile_id: str) -> None:
        self.profile_id = str(profile_id or "")
        self.resource_key = f"xhs:{self.profile_id}"
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name=f"kr-xhs-auth-{self.profile_id[-8:]}", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def _record(self, event: str, *, state: str = "USER_INTERACTING", **metadata: object) -> None:
        transition_browser_session(
            self.resource_key,
            state,
            profile_id=self.profile_id,
            desired_visibility="attention",
            metadata={"watcher": "xhs_auth", **metadata},
            event=event,
        )

    def _probe_and_complete(self, reason: str) -> bool:
        # Delayed import prevents a chrome-manager -> watcher -> chrome-manager
        # cycle during service boot.
        from .chrome_manager import complete_browser_interaction, probe_browser_auth

        probe = probe_browser_auth("xhs", target_profile_id=self.profile_id)
        if probe.get("status") != "ok" or probe.get("auth_state") != "authenticated_with_platform_confirmation":
            self._record("xhs_auth_watcher_probe_not_authenticated", trigger=reason, probe_status=probe.get("status"), auth_state=probe.get("auth_state"))
            return False
        completed = complete_browser_interaction("xhs", probe_result=probe, profile_id=self.profile_id)
        self._record("xhs_auth_watcher_auto_completed", state="READY_SILENT", trigger=reason, completed=completed.get("status") == "ok")
        return bool(completed.get("status") == "ok")

    def _page_websocket_url(self) -> str:
        from .chrome_manager import _chrome_debug_url

        resource_url = _chrome_debug_url(self.resource_key)
        response = httpx.get(f"{resource_url}/json/list", timeout=3)
        response.raise_for_status()
        targets = response.json()
        if not isinstance(targets, list):
            return ""
        page = next((item for item in targets if isinstance(item, dict) and item.get("type") == "page" and "xiaohongshu.com" in str(item.get("url") or "")), None)
        page = page or next((item for item in targets if isinstance(item, dict) and item.get("type") == "page"), None)
        return str((page or {}).get("webSocketDebuggerUrl") or "")

    def _observe_until_event_or_probe_due(self) -> bool:
        ws_url = self._page_websocket_url()
        if not ws_url:
            return False
        ws = websocket.create_connection(ws_url, timeout=3)
        try:
            for message_id, method, params in (
                (1, "Page.enable", {}),
                (2, "DOM.enable", {}),
                (3, "Page.setLifecycleEventsEnabled", {"enabled": True}),
            ):
                ws.send(json.dumps({"id": message_id, "method": method, "params": params}))
            deadline = time.monotonic() + 30.0
            while not self.stop_event.is_set() and time.monotonic() < deadline:
                try:
                    ws.settimeout(min(1.0, max(0.1, deadline - time.monotonic())))
                    message = json.loads(ws.recv())
                except websocket.WebSocketTimeoutException:
                    continue
                if str(message.get("method") or "") in _EVENT_METHODS:
                    # Events are wakeups, not proof.  A short debounce avoids
                    # probing mid-navigation and the probe itself is decisive.
                    self.stop_event.wait(1.5)
                    return True
            return False
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def _run(self) -> None:
        self._record("xhs_auth_watcher_started", observation="cdp_page_dom_lifecycle_events")
        try:
            while not self.stop_event.is_set():
                event_seen = False
                try:
                    event_seen = self._observe_until_event_or_probe_due()
                except Exception as exc:
                    self._record("xhs_auth_watcher_event_channel_degraded", error_type=type(exc).__name__)
                if self.stop_event.is_set():
                    return
                if self._probe_and_complete("cdp_event" if event_seen else "30s_readonly_fallback_probe"):
                    return
        finally:
            with _LOCK:
                if _WATCHERS.get(self.profile_id) is self:
                    _WATCHERS.pop(self.profile_id, None)


def start_xhs_auth_watcher(profile_id: str) -> Dict[str, object]:
    """Start or reuse the sole watcher for a profile-scoped manual session."""
    profile_id = str(profile_id or "")
    if not profile_id:
        return {"status": "blocked", "reason_code": "PROFILE_BINDING_REQUIRED"}
    with _LOCK:
        existing = _WATCHERS.get(profile_id)
        if existing and existing.thread.is_alive():
            return {"status": "reused", "profile_id": profile_id}
        watcher = XhsAuthWatcher(profile_id)
        _WATCHERS[profile_id] = watcher
        watcher.start()
        return {"status": "started", "profile_id": profile_id}


def restore_pending_xhs_auth_watchers() -> Dict[str, object]:
    """Reattach watchers after a service restart without closing user windows."""
    restored = []
    for session in browser_sessions_summary(limit=200).get("sessions", []):
        platform = str(session.get("platform") or "")
        if not platform.startswith("xhs") or session.get("state") not in {"NEEDS_USER", "USER_INTERACTING", "USER_DONE_VERIFYING"}:
            continue
        profile_id = str(session.get("profile_id") or "")
        if not profile_id:
            continue
        # The prior process may have gone away while the user was scanning.
        # Recreate the *same* profile-scoped visible request before starting the
        # watcher, rather than silently attaching an offscreen browser.
        from .chrome_manager import request_browser_interaction

        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        evidence = metadata.get("trigger_evidence") if isinstance(metadata.get("trigger_evidence"), list) else []
        interaction = request_browser_interaction(
            "xhs",
            str(session.get("reason") or "manual_action_required"),
            target_profile_id=profile_id,
            trigger_evidence=[str(item) for item in evidence if str(item)] or ["persistent_manual_recovery_session"],
            source="xhs_auth_watcher.service_restart_restore",
        )
        if interaction.get("status") in {"waiting_for_user", "busy"} and start_xhs_auth_watcher(profile_id).get("status") in {"started", "reused"}:
            restored.append(profile_id)
    return {"status": "ok", "restored_profile_ids": restored}
