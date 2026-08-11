from pathlib import Path

from runtime.browser_sessions import BrowserSessionStore


def test_terminal_compaction_keeps_pending_and_recent_terminal_records(tmp_path: Path):
    store = BrowserSessionStore(tmp_path / "sessions.json", tmp_path / "events.jsonl")
    for index in range(3):
        store.upsert(platform="xhs", debug_port=str(9000 + index), state="CLOSED")
    store.upsert(platform="xhs", debug_port="9100", state="NEEDS_USER")

    result = store.compact_terminal_sessions(retain_closed=1, retain_failed=0)

    assert result["removed_count"] == 2
    summary = store.summary()
    assert summary["counts"]["CLOSED"] == 1
    assert summary["counts"]["NEEDS_USER"] == 1
