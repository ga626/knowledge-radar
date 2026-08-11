"""Local quota tracking for optional academic providers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import threading
from typing import Any, Dict

from runtime.paths import runtime_state_dir


_LOCK = threading.Lock()


@dataclass(frozen=True)
class DailyQuotaState:
    date: str
    used: int
    limit: int
    exhausted: bool
    path: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "date": self.date,
            "used": self.used,
            "limit": self.limit,
            "remaining": max(0, self.limit - self.used),
            "exhausted": self.exhausted,
            "path": self.path,
        }


def academic_quota_path(provider_id: str) -> Path:
    normalized = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(provider_id or "").strip().lower())
    if not normalized:
        normalized = "unknown"
    generic_env = f"KR_ACADEMIC_{normalized.upper()}_USAGE_PATH"
    if os.environ.get(generic_env):
        return Path(os.environ[generic_env])
    if normalized == "serpapi_scholar" and os.environ.get("KR_ACADEMIC_SERPAPI_USAGE_PATH"):
        return Path(os.environ["KR_ACADEMIC_SERPAPI_USAGE_PATH"])
    return runtime_state_dir() / f"academic_{normalized}_usage.json"


def academic_serpapi_quota_path() -> Path:
    return Path(os.environ.get("KR_ACADEMIC_SERPAPI_USAGE_PATH") or academic_quota_path("serpapi"))


def quota_status(provider_id: str, limit: int, *, path: Path | None = None, today: str | None = None) -> DailyQuotaState:
    return daily_quota_status(limit, path=path or academic_quota_path(provider_id), today=today)


def consume_quota(provider_id: str, limit: int, *, path: Path | None = None, today: str | None = None) -> DailyQuotaState:
    return consume_daily_quota(limit, path=path or academic_quota_path(provider_id), today=today)


def daily_quota_status(limit: int, *, path: Path | None = None, today: str | None = None) -> DailyQuotaState:
    date = today or _today()
    quota_path = path or academic_serpapi_quota_path()
    data = _read_usage(quota_path)
    used = int(data.get(date, 0) or 0)
    return DailyQuotaState(date=date, used=used, limit=max(0, int(limit)), exhausted=used >= max(0, int(limit)), path=str(quota_path))


def consume_daily_quota(limit: int, *, path: Path | None = None, today: str | None = None) -> DailyQuotaState:
    date = today or _today()
    quota_path = path or academic_serpapi_quota_path()
    with _LOCK:
        data = _read_usage(quota_path)
        used = int(data.get(date, 0) or 0)
        quota_limit = max(0, int(limit))
        if used >= quota_limit:
            return DailyQuotaState(date=date, used=used, limit=quota_limit, exhausted=True, path=str(quota_path))
        data = {date: used + 1}
        _write_usage(quota_path, data)
        return DailyQuotaState(date=date, used=used + 1, limit=quota_limit, exhausted=(used + 1) >= quota_limit, path=str(quota_path))


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _read_usage(path: Path) -> Dict[str, int]:
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): int(value) for key, value in data.items() if str(key) and _is_int(value)}


def _write_usage(path: Path, data: Dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _is_int(value: Any) -> bool:
    try:
        int(value)
        return True
    except Exception:
        return False
