"""Durable, privacy-preserving observability for the MCP/host boundary.

The MCP server can prove its own process, session and tool-list state.  It
cannot inspect the Codex host's thread tool cards, so that fourth layer is
returned explicitly as host-observed-only instead of being guessed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import os
import ctypes
import tempfile
import threading
import time
from typing import Any, Iterable
import uuid

from runtime.paths import runtime_state_dir
from runtime.mcp_continuity import evaluate as evaluate_continuity


SCHEMA = "knowledgeradar-mcp-observability/v1"
STATE_FILE = "knowledgeradar-mcp-observability.json"
_LOCK = threading.RLock()
LOCK_WAIT_SECONDS = 5.0
LOCK_STALE_SECONDS = 30.0
FALLBACK_HISTORY_LIMIT = 12


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def state_path() -> Path:
    return runtime_state_dir() / STATE_FILE


def _opaque_id(value: Any) -> str:
    text = str(value or "")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] if text else ""


def tool_list_fingerprint(tool_names: Iterable[str]) -> str:
    names = sorted({str(name) for name in tool_names if str(name)})
    return "sha256:" + hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()[:20]


def _read() -> dict[str, Any]:
    try:
        value = json.loads(state_path().read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


@contextmanager
def _state_lock() -> Iterable[None]:
    """Serialize read-modify-write cycles across short-lived L3 processes."""

    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"pid": os.getpid(), "created_at": _now()}, ensure_ascii=False))
            break
        except FileExistsError:
            try:
                stale = time.time() - lock_path.stat().st_mtime > LOCK_STALE_SECONDS
            except OSError:
                stale = False
            if stale:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise OSError(f"mcp_observability_lock_timeout:{lock_path}")
            time.sleep(0.02)
    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


@contextmanager
def _transaction() -> Iterable[None]:
    with _LOCK:
        with _state_lock():
            yield


def _write(payload: dict[str, Any]) -> dict[str, Any]:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"schema": SCHEMA, "updated_at": _now(), **payload}
    temporary_path = Path(
        tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.",
            suffix=".tmp",
            delete=False,
        ).name
    )
    try:
        temporary_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
    return data


def _pid_is_running(pid: Any) -> bool:
    """Return whether a recorded local PID is still alive without probing a host session."""

    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    if os.name == "nt":
        # ``os.kill(pid, 0)`` is not a harmless existence probe on Windows.
        # Ask the kernel for a query-only handle instead, so observability can
        # never signal the short-lived process it is merely inspecting.
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, value)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _bounded_fallback_records(records: dict[str, Any]) -> dict[str, Any]:
    """Keep only recent L3 lifecycle records; they are diagnostics, not an audit log."""

    ordered = sorted(
        ((str(key), value) for key, value in records.items() if isinstance(value, dict)),
        key=lambda item: str(item[1].get("stopped_at") or item[1].get("last_observed_at") or item[1].get("started_at") or ""),
        reverse=True,
    )
    return dict(ordered[:FALLBACK_HISTORY_LIMIT])


def _reconcile_fallback_processes(current: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Mark abandoned one-shot L3 records terminal before they pollute health output."""

    records = dict(current.get("fallback_processes") or {})
    changed = False
    for invocation_id, record in records.items():
        if not isinstance(record, dict) or record.get("status") != "running":
            continue
        if _pid_is_running(record.get("pid")):
            continue
        records[invocation_id] = {
            **record,
            "status": "stopped",
            "stopped_at": _now(),
            "stop_reason": "process_not_running",
        }
        changed = True
    bounded = _bounded_fallback_records(records)
    if bounded != records:
        changed = True
    return bounded, changed


def record_server_started(
    *,
    transport: str,
    tool_names: Iterable[str],
    source_fingerprint: str = "",
    invocation_kind: str = "native_server",
    invocation_id: str = "",
) -> dict[str, Any]:
    names = sorted({str(name) for name in tool_names if str(name)})
    with _transaction():
        current = _read()
        server = {
            "status": "running",
            "pid": os.getpid(),
            "transport": str(transport or "unknown"),
            "started_at": _now(),
            "source_fingerprint": str(source_fingerprint or ""),
        }
        if invocation_kind == "continuity_fallback":
            fallback_processes = dict(current.get("fallback_processes") or {})
            fallback_key = str(invocation_id or f"pid-{os.getpid()}")
            fallback_processes[fallback_key] = {**server, "invocation_id": fallback_key}
            return _write({
                **current,
                "fallback_processes": _bounded_fallback_records(fallback_processes),
                "events": current.get("events", [])[-99:] + [{"kind": "fallback_server_started", "at": _now(), "transport": str(transport or "unknown"), "invocation_id": fallback_key}],
            })
        tool_list = {
            "status": "declared",
            "count": len(names),
            "fingerprint": tool_list_fingerprint(names),
            "names": names,
            "last_observed_at": current.get("tool_list", {}).get("last_observed_at", ""),
        }
        processes = dict(current.get("server_processes") or {})
        processes[str(transport or "unknown")] = server
        return _write({
            **current,
            "server_process": server,
            "server_processes": processes,
            "tool_list": tool_list,
            "tool_lists": {**dict(current.get("tool_lists") or {}), str(transport or "unknown"): tool_list},
            "events": current.get("events", [])[-99:] + [{"kind": "server_started", "at": _now(), "transport": str(transport or "unknown")}],
        })


def record_tool_list(
    *,
    session_id: Any = "",
    tool_names: Iterable[str],
    transport: str = "unknown",
    invocation_kind: str = "native_server",
    invocation_id: str = "",
) -> dict[str, Any]:
    names = sorted({str(name) for name in tool_names if str(name)})
    with _transaction():
        current = _read()
        session_hash = _opaque_id(session_id)
        observed_at = _now()
        tool_list = {
            **(current.get("tool_lists", {}).get(str(transport or "unknown"), {}) or {}),
            "status": "observed",
            "count": len(names),
            "fingerprint": tool_list_fingerprint(names),
            "names": names,
            "last_observed_at": observed_at,
            "last_session_hash": session_hash,
        }
        if invocation_kind == "continuity_fallback":
            fallback_lists = dict(current.get("fallback_tool_lists") or {})
            fallback_key = str(invocation_id or f"pid-{os.getpid()}")
            fallback_lists[fallback_key] = {**tool_list, "invocation_id": fallback_key}
            return _write({
                **current,
                "fallback_tool_lists": _bounded_fallback_records(fallback_lists),
                "events": current.get("events", [])[-99:] + [{"kind": "fallback_tools_list_observed", "at": observed_at, "invocation_id": fallback_key, "fingerprint": tool_list["fingerprint"], "count": len(names)}],
            })
        event = {"kind": "tools_list_observed", "at": observed_at, "session_hash": session_hash, "fingerprint": tool_list["fingerprint"], "count": len(names)}
        tool_lists = {**dict(current.get("tool_lists") or {}), str(transport or "unknown"): tool_list}
        active_transport = str((current.get("server_process") or {}).get("transport") or "")
        active_tool_list = tool_list if active_transport == str(transport or "unknown") else (tool_lists.get(active_transport) or current.get("tool_list") or tool_list)
        return _write({**current, "tool_list": active_tool_list, "tool_lists": tool_lists, "events": current.get("events", [])[-99:] + [event]})


def record_fallback_server_stopped(*, invocation_id: str, reason: str = "server_exit") -> dict[str, Any]:
    """Close the one-shot L3 record when its dedicated stdio server exits."""

    fallback_key = str(invocation_id or f"pid-{os.getpid()}")
    with _transaction():
        current = _read()
        fallback_processes = dict(current.get("fallback_processes") or {})
        record = dict(fallback_processes.get(fallback_key) or {})
        if not record:
            return current
        fallback_processes[fallback_key] = {
            **record,
            "status": "stopped",
            "stopped_at": _now(),
            "stop_reason": str(reason or "server_exit"),
        }
        return _write({
            **current,
            "fallback_processes": _bounded_fallback_records(fallback_processes),
            "events": current.get("events", [])[-99:] + [{"kind": "fallback_server_stopped", "at": _now(), "invocation_id": fallback_key, "reason": str(reason or "server_exit")}],
        })


def snapshot(*, transport: str, tool_names: Iterable[str]) -> dict[str, Any]:
    names = sorted({str(name) for name in tool_names if str(name)})
    with _transaction():
        current = _read()
        fallback_processes, fallback_changed = _reconcile_fallback_processes(current)
        if fallback_changed:
            current = _write({**current, "fallback_processes": fallback_processes})
    selected_transport = str(transport or "unknown")
    server = dict((current.get("server_processes") or {}).get(selected_transport) or current.get("server_process") or {})
    server.setdefault("status", "running" if os.getpid() else "unknown")
    server.setdefault("pid", os.getpid())
    server.setdefault("transport", str(transport or "unknown"))
    declared_fingerprint = tool_list_fingerprint(names)
    observed = dict((current.get("tool_lists") or {}).get(selected_transport) or current.get("tool_list") or {})
    session_status = "observed" if any(item.get("kind") == "tools_list_observed" for item in current.get("events", []) if isinstance(item, dict)) else "unobserved"
    continuity = evaluate_continuity(
        config_ok=True,
        service_ok=bool(server.get("status") == "running"),
        session_status=session_status,
        tool_list_ok=bool(observed.get("status") == "observed"),
        source_fingerprint=str(server.get("source_fingerprint") or ""),
        tool_list_fingerprint=str(observed.get("fingerprint") or declared_fingerprint),
    )
    return {
        "schema": SCHEMA,
        "server_process": server,
        "server_processes": current.get("server_processes") or {},
        "fallback_processes": current.get("fallback_processes") or {},
        "fallback_tool_lists": current.get("fallback_tool_lists") or {},
        "mcp_session": {"status": session_status, "last_session_hash": observed.get("last_session_hash", ""), "last_observed_at": observed.get("last_observed_at", "")},
        "tool_list": {"status": observed.get("status", "declared"), "declared_count": len(names), "declared_fingerprint": declared_fingerprint, "observed_count": observed.get("count"), "observed_fingerprint": observed.get("fingerprint"), "last_observed_at": observed.get("last_observed_at", "")},
        "thread_tool_surface": {"status": "host_observed_only", "native_observed": None, "reason": "MCP server cannot inspect Codex thread tool cards; only the host can prove mcp__knowledgeradar invocation."},
        "access_path": "mcp_server_runtime",
        "continuity": continuity,
    }
