from __future__ import annotations

import threading

from runtime import xhs_tikhub_fallback
from runtime import xhs_api_candidates
from runtime import env_loader
from runtime import xhs_tikhub_usage


def test_tikhub_daily_limit_counts_failed_calls(monkeypatch, tmp_path) -> None:
    path = tmp_path / "xhs_tikhub_usage.json"
    monkeypatch.setenv("KR_XHS_TIKHUB_DAILY_SEARCH_LIMIT", "1")
    monkeypatch.setenv("KR_XHS_TIKHUB_DAILY_DETAIL_LIMIT", "1")

    before = xhs_tikhub_usage.check_tikhub_daily_limit("search", path=path)
    assert before["allowed"] is True

    after_consume = xhs_tikhub_usage.consume_tikhub_daily_limit(
        "search",
        status="failed",
        reason_code="Timeout",
        path=path,
    )
    assert after_consume["search"]["used"] == 1

    blocked = xhs_tikhub_usage.check_tikhub_daily_limit("search", path=path)
    assert blocked["allowed"] is False
    assert blocked["reason_code"] == "TIKHUB_DAILY_SEARCH_LIMIT_REACHED"


def test_tikhub_search_reservations_are_atomic_and_idempotent(monkeypatch, tmp_path) -> None:
    path = tmp_path / "xhs_tikhub_usage.json"
    monkeypatch.setenv("KR_XHS_TIKHUB_DAILY_SEARCH_LIMIT", "2")
    first = xhs_tikhub_usage.reserve_tikhub_daily_limit("search", reservation_id="task-idempotent", path=path)
    assert first["reserved"] is True
    outcomes = []

    def reserve(index: int) -> None:
        outcomes.append(xhs_tikhub_usage.reserve_tikhub_daily_limit("search", reservation_id=f"task-{index}", path=path))

    threads = [threading.Thread(target=reserve, args=(index,)) for index in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(bool(item["reserved"]) for item in outcomes) == 1
    assert xhs_tikhub_usage.tikhub_usage_summary(path)["search"]["used"] == 2
    reused = xhs_tikhub_usage.reserve_tikhub_daily_limit("search", reservation_id="task-idempotent", path=path)
    assert reused["reserved"] is True
    assert reused["reused"] is True
    assert xhs_tikhub_usage.tikhub_usage_summary(path)["search"]["used"] == 2


def test_tikhub_break_glass_defaults_to_live_paid_fallback(monkeypatch) -> None:
    monkeypatch.delenv("KR_XHS_TIKHUB_BREAK_GLASS_AUTO", raising=False)
    monkeypatch.delenv("KR_XHS_TIKHUB_BREAK_GLASS_DRY_RUN", raising=False)

    assert xhs_tikhub_fallback._break_glass_enabled() is True
    assert xhs_tikhub_fallback._break_glass_dry_run() is False


def test_runtime_env_loader_reads_repo_dotenv(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("TIKHUB_API_KEY", raising=False)
    monkeypatch.setattr(env_loader, "REPO_ROOT", str(tmp_path))
    (tmp_path / ".env").write_text("TIKHUB_API_KEY=from-dotenv\n", encoding="utf-8")

    used = env_loader.load_runtime_env()

    assert used == str(tmp_path / ".env")
    assert xhs_tikhub_fallback._api_key_configured() is True


def test_tikhub_api_candidate_reports_break_glass_when_key_configured(monkeypatch) -> None:
    monkeypatch.setenv("TIKHUB_API_KEY", "dummy")

    summary = xhs_api_candidates.xhs_api_candidate_config_summary()
    tikhub = next(row for row in summary["candidates"] if row["id"] == "tikhub")

    assert tikhub["key_configured"] is True
    assert tikhub["status"] == "ready_for_break_glass"
    assert tikhub["probe_permission"] == "auto_break_glass_when_native_routes_fail"
