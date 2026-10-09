from runtime import recruitment_governance as gate


def test_city_configuration_failures_do_not_cool_or_exhaust_account_budget(monkeypatch, tmp_path):
    monkeypatch.setenv("KR_TASK_DB_PATH", str(tmp_path / "tasks.sqlite3"))
    clock = [10000.0]
    monkeypatch.setattr(gate.time, "time", lambda: clock[0])
    for _ in range(3):
        gate.record_search_outcome("boss", "failed", "city_mapping_missing", keyword="Python", city="杭州")
        clock[0] += 20
    result = gate.check_search_gate("boss", keyword="Python", city="杭州")
    assert result["allowed"] is True
    assert result["failures_this_hour"] == 0
    conn = gate._get_conn()
    rows = conn.execute("SELECT outcome, reason, cooldown_until FROM recruitment_search_gate").fetchall()
    conn.close()
    assert len(rows) == 3
    assert all(row == ("failed", "city_mapping_missing", 0.0) for row in rows)


def test_existing_configuration_cooldown_is_ignored_without_rewriting_history(monkeypatch, tmp_path):
    monkeypatch.setenv("KR_TASK_DB_PATH", str(tmp_path / "tasks.sqlite3"))
    monkeypatch.setattr(gate.time, "time", lambda: 10000.0)
    conn = gate._get_conn()
    conn.execute("INSERT INTO recruitment_search_gate (platform, ts, outcome, reason, cooldown_until) "
                 "VALUES ('boss', 9900, 'failed', 'city_mapping_missing', 12000)")
    conn.commit()
    conn.close()
    assert gate.check_search_gate("boss")["allowed"] is True
    conn = gate._get_conn()
    assert conn.execute("SELECT cooldown_until FROM recruitment_search_gate").fetchone()[0] == 12000
    conn.close()


def test_platform_risk_still_blocks_search(monkeypatch, tmp_path):
    monkeypatch.setenv("KR_TASK_DB_PATH", str(tmp_path / "tasks.sqlite3"))
    monkeypatch.setattr(gate.time, "time", lambda: 10000.0)
    gate.record_search_outcome("boss", "blocked", "risk_control")
    monkeypatch.setattr(gate.time, "time", lambda: 10020.0)
    assert gate.check_search_gate("boss")["allowed"] is False
