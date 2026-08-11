from runtime.browser_sessions import BrowserSessionStore


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
