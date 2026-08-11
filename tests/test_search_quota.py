from pathlib import Path

import search_providers.quota as quota
from search_providers.quota import SearchQuotaLedger, load_quota_state


def test_tavily_quota_has_daily_limit(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(quota, "_today", lambda: "2026-06-13")
    ledger = SearchQuotaLedger(tmp_path / "quota.json")

    for _ in range(quota.TAVILY_DAILY_LIMIT):
        assert ledger.allow("tavily") is True
        ledger.record_success("tavily")

    status = ledger.status("tavily")
    assert status.status == "daily_exhausted"
    assert status.remaining_today == 0


def test_tavily_quota_resets_on_new_day(tmp_path: Path, monkeypatch):
    path = tmp_path / "quota.json"
    monkeypatch.setattr(quota, "_today", lambda: "2026-06-13")
    ledger = SearchQuotaLedger(path)
    ledger.record_success("tavily")

    monkeypatch.setattr(quota, "_today", lambda: "2026-06-14")
    next_day = SearchQuotaLedger(path).status("tavily")

    assert next_day.used_today == 0
    assert next_day.remaining_today == quota.TAVILY_DAILY_LIMIT
    assert load_quota_state(path)["providers"]["tavily"]["used_today"] == 1


def test_remote_monthly_exhaustion_blocks_tavily(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(quota, "_today", lambda: "2026-06-13")
    ledger = SearchQuotaLedger(tmp_path / "quota.json")
    ledger.update_remote_remaining("tavily", 0)

    status = ledger.status("tavily")
    assert status.status == "monthly_exhausted"
    assert ledger.allow("tavily") is False
