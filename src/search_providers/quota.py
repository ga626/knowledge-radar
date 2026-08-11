"""Quota ledger for generic paid web search providers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict

from runtime.paths import runtime_state_dir


TAVILY_DAILY_LIMIT = 33
TAVILY_MONTHLY_LIMIT = 1000


def _today() -> str:
    return date.today().isoformat()


def _state_path() -> Path:
    return Path(runtime_state_dir()) / "search_quota_state.json"


def _default_state() -> Dict[str, Any]:
    return {"schema": "knowledgeradar-search-quota/v1", "providers": {}}


def load_quota_state(path: Path | None = None) -> Dict[str, Any]:
    target = path or _state_path()
    if not target.exists():
        return _default_state()
    try:
        with target.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("schema", "knowledgeradar-search-quota/v1")
            data.setdefault("providers", {})
            return data
    except Exception:
        return _default_state()
    return _default_state()


def save_quota_state(state: Dict[str, Any], path: Path | None = None) -> None:
    target = path or _state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


@dataclass(frozen=True)
class QuotaStatus:
    provider: str
    daily_limit: int
    used_today: int
    remaining_today: int
    status: str
    month_limit: int = TAVILY_MONTHLY_LIMIT
    remote_remaining: int | None = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "daily_limit": self.daily_limit,
            "used_today": self.used_today,
            "remaining_today": self.remaining_today,
            "status": self.status,
            "month_limit": self.month_limit,
            "remote_remaining": self.remote_remaining,
        }


class SearchQuotaLedger:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path

    def status(self, provider: str) -> QuotaStatus:
        if provider != "tavily":
            return QuotaStatus(provider=provider, daily_limit=0, used_today=0, remaining_today=0, status="unlimited")
        state = load_quota_state(self.path)
        providers = state.setdefault("providers", {})
        row = providers.get("tavily") if isinstance(providers.get("tavily"), dict) else {}
        if row.get("date") != _today():
            row = {"date": _today(), "used_today": 0}
        used = int(row.get("used_today") or 0)
        remaining = max(0, TAVILY_DAILY_LIMIT - used)
        status = "available" if remaining > 0 else "daily_exhausted"
        remote_remaining = row.get("remote_remaining")
        try:
            remote_remaining_int = int(remote_remaining) if remote_remaining is not None else None
        except (TypeError, ValueError):
            remote_remaining_int = None
        if remote_remaining_int is not None and remote_remaining_int <= 0:
            status = "monthly_exhausted"
            remaining = 0
        return QuotaStatus(
            provider="tavily",
            daily_limit=TAVILY_DAILY_LIMIT,
            used_today=used,
            remaining_today=remaining,
            status=status,
            remote_remaining=remote_remaining_int,
        )

    def allow(self, provider: str) -> bool:
        status = self.status(provider)
        return status.status in {"available", "unlimited"}

    def record_success(self, provider: str, count: int = 1) -> None:
        if provider != "tavily":
            return
        state = load_quota_state(self.path)
        row = state.setdefault("providers", {}).get("tavily")
        if not isinstance(row, dict) or row.get("date") != _today():
            row = {"date": _today(), "used_today": 0}
        row["date"] = _today()
        row["used_today"] = int(row.get("used_today") or 0) + max(1, int(count or 1))
        row["updated_at"] = datetime.now(timezone.utc).isoformat()
        state["providers"]["tavily"] = row
        save_quota_state(state, self.path)

    def update_remote_remaining(self, provider: str, remaining: int | None) -> None:
        if provider != "tavily" or remaining is None:
            return
        state = load_quota_state(self.path)
        row = state.setdefault("providers", {}).get("tavily")
        if not isinstance(row, dict) or row.get("date") != _today():
            row = {"date": _today(), "used_today": 0}
        row["remote_remaining"] = int(remaining)
        row["updated_at"] = datetime.now(timezone.utc).isoformat()
        state["providers"]["tavily"] = row
        save_quota_state(state, self.path)


def quota_summary() -> Dict[str, Any]:
    ledger = SearchQuotaLedger()
    return {"schema": "knowledgeradar-search-quota/v1", "providers": {"tavily": ledger.status("tavily").to_dict()}}
