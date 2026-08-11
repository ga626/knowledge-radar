"""Hard daily call limits for paid TikHub Xiaohongshu fallback calls."""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from .paths import runtime_state_dir


_LOCK = threading.RLock()


def default_xhs_tikhub_usage_path() -> Path:
    return Path(os.environ.get("KR_XHS_TIKHUB_USAGE_PATH") or (runtime_state_dir() / "xhs_tikhub_usage.json"))


def tikhub_daily_limit(kind: str) -> int:
    env = "KR_XHS_TIKHUB_DAILY_DETAIL_LIMIT" if kind == "detail" else "KR_XHS_TIKHUB_DAILY_SEARCH_LIMIT"
    try:
        # Search has two paid break-glass slots by default; detail remains one.
        default = "1" if kind == "detail" else "2"
        return max(0, int(os.environ.get(env, default) or default))
    except Exception:
        return 1


def tikhub_usage_summary(path: str | Path | None = None) -> Dict[str, Any]:
    today = _today()
    data = _read(path)
    day = data.get(today) if isinstance(data.get(today), dict) else {}
    def _metrics(kind: str) -> Dict[str, Any]:
        reservations = day.get("reservations") if isinstance(day.get("reservations"), dict) else {}
        rows = [row for row in reservations.values() if isinstance(row, dict) and row.get("kind") == kind]
        actual = [row for row in rows if row.get("outcome_status") not in {"", "reserved"}]
        billed = [row for row in actual if row.get("billed") is not False]
        failures = [row for row in actual if str(row.get("outcome_status") or "").lower() not in {"ok", "success"}]
        reserved = int(day.get(kind) or 0)
        return {
            "used": reserved,  # backwards-compatible alias for reserved slots
            "reserved": reserved,
            "actual_api_calls": len(actual),
            "billed_units": len(billed),
            "failed_calls": len(failures),
            "limit": tikhub_daily_limit(kind),
        }

    return {
        "schema": "knowledgeradar-xhs-tikhub-usage/v1",
        "date": today,
        "path": str(path or default_xhs_tikhub_usage_path()),
        "search": _metrics("search"),
        "detail": _metrics("detail"),
        "note": "Only paid TikHub calls are hard-limited. Native XHS search/detail use existing rate guards.",
    }


def check_tikhub_daily_limit(kind: str, path: str | Path | None = None) -> Dict[str, Any]:
    kind = "detail" if kind == "detail" else "search"
    with _LOCK:
        summary = tikhub_usage_summary(path)
        row = summary[kind]
        allowed = int(row["used"]) < int(row["limit"])
        return {
            "allowed": allowed,
            "kind": kind,
            "used": int(row["used"]),
            "limit": int(row["limit"]),
            "reason_code": "OK" if allowed else f"TIKHUB_DAILY_{kind.upper()}_LIMIT_REACHED",
            "summary": summary,
        }


def reserve_tikhub_daily_limit(
    kind: str,
    *,
    reservation_id: str,
    path: str | Path | None = None,
) -> Dict[str, Any]:
    """Atomically reserve one paid slot before issuing a TikHub request.

    The reservation is also the consumption record: a timeout or non-2xx can
    still be billed, so it must not be returned to the pool.  Reusing the same
    id is idempotent and therefore safe for retried task orchestration.
    """
    kind = "detail" if kind == "detail" else "search"
    reservation_id = str(reservation_id or "").strip()
    if not reservation_id:
        return {"reserved": False, "reason_code": "TIKHUB_RESERVATION_ID_REQUIRED", "kind": kind}
    today = _today()
    usage_path = Path(path or default_xhs_tikhub_usage_path())
    with _usage_file_lock(usage_path):
        data = _read(usage_path)
        day = data.get(today)
        if not isinstance(day, dict):
            day = {}
        reservations = day.get("reservations")
        if not isinstance(reservations, dict):
            reservations = {}
        existing = reservations.get(reservation_id)
        if isinstance(existing, dict):
            return {
                "reserved": True,
                "reused": True,
                "reservation_id": reservation_id,
                "kind": kind,
                "reason_code": "OK_REUSED_RESERVATION",
                "usage_semantics": "reservation_reused; actual_call_not_inferred",
                "summary": tikhub_usage_summary(usage_path),
            }
        used = int(day.get(kind) or 0)
        limit = tikhub_daily_limit(kind)
        if used >= limit:
            return {
                "reserved": False,
                "reused": False,
                "reservation_id": reservation_id,
                "kind": kind,
                "reason_code": f"TIKHUB_DAILY_{kind.upper()}_LIMIT_REACHED",
                "summary": tikhub_usage_summary(usage_path),
            }
        day[kind] = used + 1
        reservations[reservation_id] = {
            "kind": kind,
            "reserved_at": datetime.now().isoformat(timespec="seconds"),
            "outcome_status": "reserved",
            "outcome_reason_code": "",
        }
        day["reservations"] = reservations
        data[today] = day
        _write(usage_path, data)
        return {
            "reserved": True,
            "reused": False,
            "reservation_id": reservation_id,
            "kind": kind,
            "reason_code": "OK_RESERVED",
            "usage_semantics": "reserved_slot; actual_call_not_inferred",
            "summary": tikhub_usage_summary(usage_path),
        }


def record_tikhub_reservation_outcome(
    reservation_id: str,
    *,
    status: str,
    reason_code: str,
    billed: bool | None = None,
    path: str | Path | None = None,
) -> Dict[str, Any]:
    """Attach a redacted request result to an existing, already-consumed slot."""
    usage_path = Path(path or default_xhs_tikhub_usage_path())
    today = _today()
    with _usage_file_lock(usage_path):
        data = _read(usage_path)
        day = data.get(today) if isinstance(data.get(today), dict) else {}
        reservations = day.get("reservations") if isinstance(day.get("reservations"), dict) else {}
        row = reservations.get(str(reservation_id or ""))
        if isinstance(row, dict):
            row["outcome_status"] = str(status or "")
            row["outcome_reason_code"] = str(reason_code or "")
            if billed is not None:
                row["billed"] = bool(billed)
            elif str(status or "").lower() in {"ok", "success", "degraded", "failed", "empty"}:
                row["billed"] = True
            row["outcome_at"] = datetime.now().isoformat(timespec="seconds")
            reservations[str(reservation_id)] = row
            day["reservations"] = reservations
            data[today] = day
            _write(usage_path, data)
    return tikhub_usage_summary(usage_path)


def consume_tikhub_daily_limit(kind: str, *, status: str = "", reason_code: str = "", path: str | Path | None = None) -> Dict[str, Any]:
    """Consume one paid-call slot. Failed HTTP/API calls still count."""
    kind = "detail" if kind == "detail" else "search"
    today = _today()
    with _LOCK:
        usage_path = Path(path or default_xhs_tikhub_usage_path())
        data = _read(usage_path)
        day = data.get(today)
        if not isinstance(day, dict):
            day = {}
        day[kind] = int(day.get(kind) or 0) + 1
        events = day.get("events")
        if not isinstance(events, list):
            events = []
        events.append(
            {
                "kind": kind,
                "status": str(status or ""),
                "reason_code": str(reason_code or ""),
                "consumed_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        day["events"] = events[-50:]
        data[today] = day
        _write(usage_path, data)
        return tikhub_usage_summary(usage_path)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _read(path: str | Path | None = None) -> Dict[str, Any]:
    usage_path = Path(path or default_xhs_tikhub_usage_path())
    if not usage_path.exists():
        return {}
    try:
        data = json.loads(usage_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@contextmanager
def _usage_file_lock(usage_path: Path):
    """Combine process-local and Windows cross-process locking for a JSON ledger."""
    lock_path = Path(f"{usage_path}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        with lock_path.open("a+b") as lock_file:
            try:
                import msvcrt

                lock_file.seek(0)
                if lock_file.tell() == 0:
                    lock_file.write(b"0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            except ImportError:
                yield
