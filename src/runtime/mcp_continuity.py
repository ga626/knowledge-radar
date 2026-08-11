"""Four-layer MCP continuity state and auditable recovery receipts.

This module deliberately does not control Codex Desktop.  It records what the
KnowledgeRadar process can prove and accepts host-observed native-call evidence
from a caller such as ``check_codex_mcp_surface.py``.  A local probe can prove
L0/L1; only a real ``mcp__knowledgeradar.*`` call can prove L2.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from typing import Any, Iterable

from runtime.paths import runtime_state_dir


SCHEMA = "knowledgeradar-mcp-continuity/v1"
STATE_FILE = "knowledgeradar-mcp-continuity.json"
ACCESS_NATIVE = "native_mcp"
ACCESS_RUNTIME = "mcp_server_runtime"
ACCESS_FALLBACK = "continuity_fallback"
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def state_path() -> Path:
    return runtime_state_dir() / STATE_FILE


def _read() -> dict[str, Any]:
    try:
        value = json.loads(state_path().read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write(payload: dict[str, Any]) -> dict[str, Any]:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"schema": SCHEMA, "updated_at": _now(), **payload}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return data


def _default() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "generation": 0,
        "access_path": ACCESS_RUNTIME,
        "status": "unknown",
        "layers": {
            "l0_config_process": "unknown",
            "l1_mcp_session_tools": "unknown",
            "l2_thread_native_surface": "host_unobserved",
            "l3_continuity_fallback": "not_activated",
        },
        "last_error": "",
        "last_degraded_reason": "",
        "source_fingerprint": "",
        "tool_list_fingerprint": "",
        "native_tools": [],
        "events": [],
    }


def evaluate(
    *,
    config_ok: bool,
    service_ok: bool,
    session_status: str,
    tool_list_ok: bool,
    native_tools: Iterable[str] = (),
    fallback_active: bool = False,
    fallback_reason: str = "",
    source_fingerprint: str = "",
    tool_list_fingerprint: str = "",
) -> dict[str, Any]:
    """Purely derive the four-layer status; never writes runtime state."""

    native = sorted({str(item) for item in native_tools if str(item)})
    l0 = "pass" if config_ok and service_ok else "fail"
    l1 = "pass" if session_status in {"observed", "initialized"} and tool_list_ok else "fail"
    l2 = "pass" if native else "host_unobserved"
    l3 = "active" if fallback_active else "not_activated"
    if l2 == "pass":
        status, access = "native_ready", ACCESS_NATIVE
    elif l3 == "active":
        status, access = "degraded_continuity", ACCESS_FALLBACK
    elif l0 == "fail" or l1 == "fail":
        status, access = "runtime_not_ready", ACCESS_RUNTIME
    else:
        # The server cannot observe Codex's thread cards.  This is unknown,
        # not a failed refresh; only a host-provided failure receipt may use a
        # refresh-required status.
        status, access = "host_unobserved", ACCESS_RUNTIME
    return {
        "schema": SCHEMA,
        "status": status,
        "access_path": access,
        "layers": {
            "l0_config_process": l0,
            "l1_mcp_session_tools": l1,
            "l2_thread_native_surface": l2,
            "l3_continuity_fallback": l3,
        },
        "source_fingerprint": str(source_fingerprint or ""),
        "tool_list_fingerprint": str(tool_list_fingerprint or ""),
        "native_tools": native,
        "last_degraded_reason": str(fallback_reason or ""),
    }


def snapshot(**kwargs: Any) -> dict[str, Any]:
    """Return persisted state merged with a fresh pure evaluation."""

    current = _read()
    derived = evaluate(**kwargs)
    return {**current, **derived, "generation": current.get("generation", 0)}


def record_transition(*, event: str, access_path: str, status: str, reason: str = "", **fields: Any) -> dict[str, Any]:
    """Persist a redacted transition receipt; caller owns the truth of the event."""

    with _LOCK:
        current = {**_default(), **_read()}
        events = list(current.get("events") or [])[-99:]
        events.append({"event": str(event), "at": _now(), "access_path": str(access_path), "status": str(status), "reason": str(reason or "")})
        current.update({"generation": int(current.get("generation") or 0) + 1, "access_path": str(access_path), "status": str(status), "last_error": str(reason or ""), "last_degraded_reason": str(reason or ""), "events": events, **fields})
        return _write(current)


def record_native_call(*, tool: str, source_fingerprint: str = "", tool_list_fingerprint: str = "") -> dict[str, Any]:
    """Record host-observed proof of a real native KR tool call."""

    return record_transition(
        event="native_call_observed",
        access_path=ACCESS_NATIVE,
        status="native_ready",
        native_tools=[str(tool)],
        source_fingerprint=source_fingerprint,
        tool_list_fingerprint=tool_list_fingerprint,
        layers={
            "l0_config_process": "pass",
            "l1_mcp_session_tools": "pass",
            "l2_thread_native_surface": "pass",
            "l3_continuity_fallback": "not_activated",
        },
        last_error="",
        last_degraded_reason="",
    )


def record_fallback(*, reason: str, task_id: str = "") -> dict[str, Any]:
    """Record an explicit, non-native continuity fallback activation."""

    return record_transition(
        event="continuity_fallback_activated",
        access_path=ACCESS_FALLBACK,
        status="degraded_continuity",
        reason=reason,
        task_id=str(task_id or ""),
        layers={
            "l0_config_process": "unknown",
            "l1_mcp_session_tools": "unknown",
            "l2_thread_native_surface": "host_unobserved",
            "l3_continuity_fallback": "active",
        },
    )


def record_fallback_call(
    *,
    tool: str,
    outcome: str,
    reason: str,
    task_id: str = "",
    source_fingerprint: str = "",
    tool_list_fingerprint: str = "",
) -> dict[str, Any]:
    """Record an actual L3 invocation without calling it a native recovery."""

    normalized_outcome = str(outcome or "failed")
    status = "degraded_continuity" if normalized_outcome == "ok" else "fallback_unavailable"
    return record_transition(
        event="continuity_fallback_call",
        access_path=ACCESS_FALLBACK,
        status=status,
        reason=reason,
        task_id=str(task_id or ""),
        fallback_tool=str(tool or ""),
        fallback_outcome=normalized_outcome,
        source_fingerprint=str(source_fingerprint or ""),
        tool_list_fingerprint=str(tool_list_fingerprint or ""),
        layers={
            "l0_config_process": "unknown",
            "l1_mcp_session_tools": "independent_fallback_session",
            "l2_thread_native_surface": "host_unobserved",
            "l3_continuity_fallback": "active" if normalized_outcome == "ok" else "failed",
        },
    )
