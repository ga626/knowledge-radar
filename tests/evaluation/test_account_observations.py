from itertools import permutations
import pytest

from runtime.account_observations import AccountObservationStore
from runtime.xhs_task_failover import run_browser_accounts


def test_ttl_probe_and_business_are_distinct(tmp_path):
    store = AccountObservationStore(tmp_path / "facts.db")
    failure = store.observe("B", "xhs", "needs_login", source="call", now=100)
    repeated = store.observe("B", "xhs", "needs_login", source="call", now=101)
    assert repeated["event_id"] == failure["event_id"]
    assert store.claim_notification(failure["event_id"])
    assert not store.claim_notification(failure["event_id"])
    store.delivery(failure["event_id"], "acknowledged")
    probe = store.observe("B", "xhs", "auth_verified", source="login_probe", now=102)
    assert not probe["verified"]
    success = store.observe("B", "xhs", "business_verified", source="search", now=103)
    assert success["verified"]
    assert store.get("B", now=2000)["state"] == "unknown"
    assert not store.get("B", now=2000)["verified"]


def test_late_delivery_receipt_cannot_undo_ack(tmp_path):
    import sqlite3
    store = AccountObservationStore(tmp_path/'facts.db')
    store.delivery('event', 'acknowledged')
    store.delivery('event', 'failed', {'reason': 'late helper'})
    with sqlite3.connect(store.path) as conn:
        assert conn.execute('SELECT status FROM deliveries').fetchone()[0] == 'acknowledged'


def test_unshown_crashed_notification_can_replay_but_shown_never_replays(tmp_path, monkeypatch):
    from runtime import account_observations
    clock = [100.0]
    monkeypatch.setattr(account_observations.time, "time", lambda: clock[0])
    store = AccountObservationStore(tmp_path / "facts.db")
    event = store.observe("A", "xhs", "needs_login", source="call")["event_id"]
    assert store.claim_notification(event)
    store.delivery(event, "dispatched")
    assert store.pending_notifications() == []
    clock[0] += 91
    assert store.pending_notifications()[0]["event_id"] == event
    assert store.claim_notification(event)
    store.delivery(event, "shown")
    clock[0] += 100
    assert store.pending_notifications() == []
    assert not store.claim_notification(event)
    store.delivery(event, "activated")
    store.delivery(event, "acknowledged")
    assert store.notification_is_resolved(event)


def test_shown_and_activated_receipts_are_monotonic_and_recovery_closes_outbox(tmp_path):
    import sqlite3
    store = AccountObservationStore(tmp_path / "facts.db")
    failure = store.observe("B", "xhs", "needs_login", source="call")
    event = failure["event_id"]
    assert store.pending_notifications()[0]["profile_id"] == "B"
    store.delivery(event, "shown", {"user_observed": False})
    store.delivery(event, "failed", {"reason": "late"})
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT status FROM deliveries").fetchone()[0] == "shown"
    store.delivery(event, "activated")
    store.delivery(event, "dispatched")
    store.observe("B", "xhs", "auth_verified", source="probe")
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT status FROM deliveries").fetchone()[0] == "activated"
        assert conn.execute("SELECT status FROM notification_outbox").fetchone()[0] == "resolved"
    assert not store.claim_notification(event)
    assert store.pending_notifications() == []


def test_xhs_structured_account_failure_is_not_success(tmp_path, monkeypatch):
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path))
    pool = {"accounts": [{"profile_id": p, "risk": {"risk_score": 0}} for p in "ABC"]}
    _, receipt = run_browser_accounts({"policy": {"default_mode": "safe_auto"}}, pool, "A",
                                     lambda _: {"failure_type": "login_required", "status": "needs_interaction"}, lambda *_: None)
    assert receipt["failed_profile_ids"] == ["A", "B", "C"]
    assert receipt["exhausted"]


def test_late_failure_and_stale_revision_cannot_replace_fresh_success(tmp_path):
    store = AccountObservationStore(tmp_path / "facts.db")
    initial = store.observe("A", "xhs", "needs_login", source="old_call", now=100)
    success = store.observe("A", "xhs", "business_verified", source="new_call", now=102)
    late = store.observe("A", "xhs", "needs_login", source="late", now=103, expected_revision=initial["state_revision"])
    assert not late["accepted"]
    assert store.get("A", now=104)["event_id"] == success["event_id"]
    assert not store.observe("A", "xhs", "needs_login", source="old_timestamp", now=101)["accepted"]


def test_recovery_lease_is_cross_process_and_old_owner_is_rejected(tmp_path):
    store = AccountObservationStore(tmp_path / "facts.db")
    store.observe("B", "xhs", "needs_login", source="call", now=100)
    first = store.begin_recovery("B", owner_generation="one", lease_s=2, now=101)
    assert AccountObservationStore(store.path).begin_recovery("B", owner_generation="two", now=102)["status"] == "busy"
    second = store.begin_recovery("B", owner_generation="two", now=104)
    assert not store.complete_recovery(first, "xhs", now=105)["accepted"]
    restored = store.complete_recovery(second, "xhs", now=106)
    assert restored["accepted"] and restored["state"] == "auth_verified"
    assert not restored["verified"]


def test_failure_and_pending_outbox_commit_together_and_success_resolves(tmp_path):
    import sqlite3
    store = AccountObservationStore(tmp_path / "facts.db")
    failure = store.observe("C", "xhs", "needs_login", source="call", now=100)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT event_id,status FROM notification_outbox").fetchone() == (failure["event_id"], "pending")
    store.observe("C", "xhs", "business_verified", source="search", now=101)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT status FROM notification_outbox").fetchone()[0] == "resolved"


def test_legacy_profiles_retained_as_unknown_without_new_writes(tmp_path, monkeypatch):
    import json
    from runtime import profile_registry
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path))
    legacy = tmp_path/'legacy.json'
    legacy.write_text(json.dumps({'profiles':[{'profile_id':'old','state':'healthy'}]}), encoding='utf-8')
    before=legacy.read_bytes()
    monkeypatch.setattr(profile_registry, 'profile_state_path', lambda: str(legacy))
    profile_registry.record_profile_state('new', state='healthy')
    rows={r['profile_id']:r for r in profile_registry.profile_state_summary()['profiles']}
    assert rows['old']['state']=='unknown' and rows['new']['state']=='available'
    assert legacy.read_bytes()==before


@pytest.mark.parametrize("order", list(permutations(["A", "B", "C"])))
def test_all_six_orders_exhaust_distinct_profiles(order):
    pool = {"accounts": [{"profile_id": p, "risk": {"risk_score": 0}} for p in order]}
    registry = {"raw": {"policy": {"default_mode": "safe_auto", "max_switches_per_task": 2}}}
    visited = []
    _, receipt = run_browser_accounts(registry, pool, order[0], lambda p: visited.append(p) or {"error": {"type": "login_required"}}, lambda *_: None)
    assert visited == list(order)
    assert receipt["exhausted"]
