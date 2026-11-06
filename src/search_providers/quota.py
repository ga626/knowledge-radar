"""搜索规划的配额只读视图；发送预留唯一落在runtime.paid_execution。"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from runtime.paid_execution import provider_quota_summary


@dataclass(frozen=True)
class QuotaStatus:
    details: dict

    @property
    def status(self):
        return self.details["status"]

    def to_dict(self):
        return dict(self.details)


class SearchQuotaLedger:
    def __init__(self, path: Path | None = None):
        self.path = path

    def status(self, provider: str):
        try:
            return QuotaStatus(provider_quota_summary(provider, path=self.path))
        except (OSError, ValueError, TypeError):
            return QuotaStatus({"provider": provider, "status": "quota_state_invalid"})

    def allow(self, provider: str):
        return self.status(provider).status in {"available", "record_only"}


def quota_summary():
    ledger = SearchQuotaLedger()
    return {"schema": "knowledgeradar-search-quota/v2", "providers": {
        name: ledger.status(name).to_dict() for name in ("tavily", "brave", "exa", "anysearch")}}
