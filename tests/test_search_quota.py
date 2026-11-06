"""搜索规划复用同一credits账本，旧每日33次门槛不再运行。"""
import json
from decimal import Decimal
import pytest
from runtime import paid_execution as paid
from search_providers.quota import SearchQuotaLedger


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "paid_usage.json"
    monkeypatch.setenv("KR_PAID_USAGE_PATH", str(path))
    monkeypatch.delenv("KR_PAID_POLICY_PATH", raising=False)
    return SearchQuotaLedger(path)


def test_tavily_monthly_view_reads_transport_reservations(ledger):
    for index in range(34):
        with paid.paid_send("tavily", {"query": str(index), "search_depth": "basic"}, bound=20, task_id="research"):
            pass
    status = ledger.status("tavily").to_dict()
    assert status["status"] == "available"
    assert status["used"] == "34" and status["remaining"] == "966" and status["unit"] == "credits"
    assert "daily_limit" not in status
    assert ledger.allow("tavily")


def test_month_exhaustion_and_invalid_ledger_block_planning(ledger):
    period = paid._period(paid.DEFAULT_POLICY["providers"]["tavily"])
    ledger.path.write_text(json.dumps({period + "-01": {"reservations": {
        "history": {"provider": "tavily", "quota_period": period, "quota_units": "1000"}}}}), encoding="utf-8")
    assert not ledger.allow("tavily")
    assert ledger.status("tavily").status == "quota_exhausted"
    ledger.path.write_text("broken", encoding="utf-8")
    assert ledger.status("tavily").status == "quota_state_invalid"


def test_old_remote_exhaustion_and_success_count_are_not_discarded(ledger):
    old = ledger.path.parent / "search_quota_state.json"
    date = paid._period({"period": "utc_day"})
    old.write_text(json.dumps({"providers": {"tavily": {"date": date, "used_today": 4, "remote_remaining": 0}}}), encoding="utf-8")
    assert not ledger.allow("tavily")
    with pytest.raises(paid.PaidAdmissionError, match="free_quota_limit"):
        with paid.paid_send("tavily", {"query": "blocked"}, bound=20):
            pytest.fail("spent after old remote exhaustion")
    assert old.is_file()  # 原始历史仍保留，不继续更新旧账本。
    assert sum(Decimal(r["quota_units"]) for d in paid.read_usage().values() for r in d["reservations"].values()) == 1000


def test_legacy_success_count_migrates_once_without_double_counting(ledger):
    date = paid._period({"period": "utc_day"})
    old = ledger.path.parent / "search_quota_state.json"
    old.write_text(json.dumps({"providers": {"tavily": {"date": date, "used_today": 4}}}), encoding="utf-8")
    for index in range(2):
        with paid.paid_send("tavily", {"query": str(index)}, bound=20):
            pass
    assert ledger.status("tavily").to_dict()["used"] == "10"
    assert len([r for d in paid.read_usage().values() for r in d["reservations"].values() if r.get("kind") == "legacy_quota_balance"]) == 1


def test_last_month_remote_zero_does_not_block_new_free_period(ledger):
    old = ledger.path.parent / "search_quota_state.json"
    old.write_text(json.dumps({"providers": {"tavily": {"date": "2025-01-01", "remote_remaining": 0}}}), encoding="utf-8")
    assert ledger.allow("tavily")
