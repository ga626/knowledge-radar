"""Process-wide and cross-process operation serialization for Xiaohongshu."""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator

from .paths import runtime_state_dir


_PROCESS_LOCK = threading.RLock()


def default_xhs_operation_lock_path() -> Path:
    return Path(os.environ.get("KR_XHS_OPERATION_LOCK_PATH") or (runtime_state_dir() / "xhs_operation.lock"))


def default_xhs_operation_lease_path() -> Path:
    return Path(os.environ.get("KR_XHS_OPERATION_LEASE_PATH") or (runtime_state_dir() / "xhs_operation_lease.json"))


@contextlib.contextmanager
def xhs_operation(operation_type: str, *, note_id: str = "", keyword: str = "", ttl_s: int = 180) -> Iterator[Dict[str, Any]]:
    """Serialize active XHS browser operations.

    P0 intentionally uses max_concurrent_xhs=1. The returned lease is diagnostic
    metadata; callers should not rely on it for routing decisions.
    """
    with _PROCESS_LOCK:
        lock_path = default_xhs_operation_lock_path()
        lease_path = default_xhs_operation_lease_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+", encoding="utf-8") as lock_file:
            _lock_file(lock_file)
            lease = {
                "schema": "knowledgeradar-xhs-operation-lease/v1",
                "operation_id": f"xhs-{int(time.time() * 1000)}-{os.getpid()}",
                "operation_type": str(operation_type or "unknown"),
                "note_id": str(note_id or ""),
                "keyword": str(keyword or "")[:120],
                "pid": os.getpid(),
                "started_at": time.time(),
                "ttl_s": int(ttl_s or 180),
                "max_concurrent_xhs": 1,
                "scheduled_patrol": False,
            }
            try:
                lease_path.write_text(json.dumps(lease, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
            try:
                yield lease
            finally:
                try:
                    if lease_path.exists():
                        lease_path.unlink()
                except Exception:
                    pass
                _unlock_file(lock_file)


def _lock_file(file_obj: Any) -> None:
    if os.name == "nt":
        import msvcrt

        file_obj.seek(0)
        msvcrt.locking(file_obj.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX)


def _unlock_file(file_obj: Any) -> None:
    if os.name == "nt":
        import msvcrt

        file_obj.seek(0)
        try:
            msvcrt.locking(file_obj.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl

        fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)
