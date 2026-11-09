from runtime.browser_sessions import BrowserSessionStore
import pytest


@pytest.mark.parametrize('observation,expected', [
    ({'status': 'missing', 'identity': ''}, 'process_missing'),
    ({'status': 'alive', 'identity': '4242:new'}, 'process_generation_changed'),
])
def test_dead_or_reused_pid_is_projected_without_mutating_ledger(tmp_path, monkeypatch, observation, expected):
    from runtime import browser_sessions as module
    store = BrowserSessionStore(tmp_path / 'sessions.json', tmp_path / 'events.jsonl')
    store.upsert(platform='vip_oa', pid=4242, state='USER_INTERACTING',
                 metadata={'account_recovery_pending': True, 'process_identity': '4242:old'})
    original = store.state_path.read_bytes()
    events = store.event_path.read_bytes()
    monkeypatch.setattr(module, 'observe_process', lambda pid: observation, raising=False)
    summary = store.summary()
    assert summary['pending_human_action'] == 0
    row = summary['sessions'][0]
    assert row['state'] == 'CLOSED'
    assert row['recorded_state'] == 'USER_INTERACTING'
    assert row['stale_reason'] == expected
    assert store.state_path.read_bytes() == original
    assert store.event_path.read_bytes() == events
    assert store.reconcile_stale_sessions(dry_run=True)['stale_count'] == 1
    assert store.state_path.read_bytes() == original
    assert store.reconcile_stale_sessions()['reconciled_count'] == 1
    assert store.summary()['sessions'][0]['state'] == 'CLOSED'
    assert store.reconcile_stale_sessions()['reconciled_count'] == 0


@pytest.mark.parametrize('pid,observation', [
    (4242, {'status': 'alive', 'identity': '4242:same'}),
    (4242, {'status': 'unknown', 'identity': ''}),
    (None, {'status': 'missing', 'identity': ''}),
])
def test_deadline_alone_and_unknown_identity_never_discard_login(tmp_path, monkeypatch, pid, observation):
    from runtime import browser_sessions as module
    monkeypatch.setattr(module, 'observe_process', lambda pid: observation, raising=False)
    store = BrowserSessionStore(tmp_path / 'sessions.json', tmp_path / 'events.jsonl')
    store.upsert(platform='vip_oa', pid=pid, state='USER_INTERACTING',
                 metadata={'process_identity': '4242:same', 'account_recovery_pending': True})
    store.set_deadline('vip_oa', 1)
    assert store.summary()['pending_human_action'] == 1
    assert store.reconcile_stale_sessions()['reconciled_count'] == 0


def test_reconciliation_rechecks_liveness_before_applying(tmp_path, monkeypatch):
    from runtime import browser_sessions as module
    monkeypatch.setattr(module, 'observe_process', lambda pid: {'status': 'missing', 'identity': ''}, raising=False)
    store = BrowserSessionStore(tmp_path / 'sessions.json', tmp_path / 'events.jsonl')
    store.upsert(platform='vip_oa', pid=4242, state='NEEDS_USER')
    assert store.reconcile_stale_sessions(dry_run=True)['stale_count'] == 1
    monkeypatch.setattr(module, 'observe_process', lambda pid: {'status': 'alive', 'identity': '4242:new'})
    # Legacy rows with no captured generation must not guess PID reuse.
    assert store.reconcile_stale_sessions()['reconciled_count'] == 0


def test_process_observation_tracks_actual_child_exit():
    import os,subprocess,sys
    from runtime.process_identity import observe_process
    current=observe_process(os.getpid())
    assert current['status']=='alive' and current['identity'].startswith(str(os.getpid())+':')
    child=subprocess.Popen([sys.executable,'-c','pass'],creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    child.wait(timeout=10)
    assert observe_process(child.pid)['status']=='missing'
    assert observe_process(None)['status']=='unknown'


def test_browser_session_compaction_dry_run_does_not_delete_terminal_records(tmp_path):
    store = BrowserSessionStore(tmp_path / "sessions.json", tmp_path / "events.jsonl")
    store.upsert(platform="xhs", state="CLOSED", debug_port="101")
    store.upsert(platform="xhs", state="CLOSED", debug_port="102")

    preview = store.compact_terminal_sessions(retain_closed=1, retain_failed=0, dry_run=True)
    assert preview["action"] == "dry_run"
    assert preview["removed_count"] == 1
    assert store.summary()["total"] == 2

    result = store.compact_terminal_sessions(retain_closed=1, retain_failed=0)
    assert result["action"] == "compacted"
    assert store.summary()["total"] == 1
