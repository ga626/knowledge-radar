"""跨入口、跨进程的公开API间隔；等待可取消，不在SQLite事务里睡眠。"""
import sqlite3
import time
from contextlib import closing
from pathlib import Path


def cooperative_wait(seconds):
    from runtime.execution_context import check_execution
    deadline = time.monotonic() + seconds
    while True:
        check_execution()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, .1))


def wait_interval(provider, seconds):
    from runtime.paths import runtime_state_dir
    from runtime.execution_context import check_execution
    path = Path(runtime_state_dir()) / 'academic-api-interval.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        check_execution()
        with closing(sqlite3.connect(path, timeout=3)) as conn, conn:
            conn.execute('CREATE TABLE IF NOT EXISTS intervals(provider TEXT PRIMARY KEY,next_at REAL NOT NULL)')
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT next_at FROM intervals WHERE provider=?', (provider,)).fetchone()
            now = time.time()
            wait = max(0, row[0] - now) if row else 0
            if not wait:
                conn.execute('INSERT OR REPLACE INTO intervals VALUES (?,?)', (provider, now+seconds))
                return
        time.sleep(min(wait, .1))
