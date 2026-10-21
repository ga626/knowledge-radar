"""控制台只索引日志增量；数据库不保存查询、URL或正文。"""
from datetime import datetime
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import uuid

from .paths import runtime_state_dir


def trace_activity(path, since, *, database=None):
    path=Path(path)
    if not path.is_file():
        return {"available":False,"successful":0,"top_tools":[]}
    cache=Path(os.environ.get("KR_CONSOLE_CACHE_DIR") or runtime_state_dir()/"console_cache")
    db=Path(database) if database else cache/"activity-index.sqlite3"
    db.parent.mkdir(parents=True,exist_ok=True)
    stat=path.stat();identity=f"{stat.st_dev}:{stat.st_ino}"
    key=str(path.resolve())
    with sqlite3.connect(db,timeout=5) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS cursor(path TEXT PRIMARY KEY,identity TEXT,offset INTEGER,generation TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS events(generation TEXT,offset INTEGER,at REAL,tool TEXT,PRIMARY KEY(generation,offset))")
        conn.execute("CREATE INDEX IF NOT EXISTS event_time ON events(at)")
        if 'checkpoint' not in {r[1] for r in conn.execute('PRAGMA table_info(cursor)')}:
            conn.execute("ALTER TABLE cursor ADD COLUMN checkpoint TEXT DEFAULT ''")
        if 'event_key' not in {r[1] for r in conn.execute('PRAGMA table_info(events)')}:
            conn.execute("ALTER TABLE events ADD COLUMN event_key TEXT")
        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS event_identity ON events(event_key) WHERE event_key IS NOT NULL')
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        cursor=conn.execute("SELECT identity,offset,generation,checkpoint FROM cursor WHERE path=?",(key,)).fetchone()
        offset=cursor[1] if cursor and cursor[0]==identity and cursor[1]<=stat.st_size else 0
        with path.open('rb') as check:
            check.seek(max(0,offset-128))
            checkpoint=hashlib.sha256(check.read(min(offset,128))).hexdigest()
        if offset and (not cursor[3] or checkpoint!=cursor[3]):
            offset=0
        generation=cursor[2] if cursor and offset else uuid.uuid4().hex
        consumed=0
        with path.open("rb") as stream:
            stream.seek(offset)
            while raw:=stream.readline():
                if not raw.endswith(b"\n"):
                    break
                current=stream.tell();consumed+=len(raw)
                try:
                    row=json.loads(raw)
                    at=datetime.fromisoformat(str(row.get("timestamp") or "").replace("Z","+00:00")).timestamp()
                    if row.get("status")=="ok" and not row.get("parent_trace_id"):
                        event_key=str(row.get('trace_id') or hashlib.sha256(key.encode()+str(offset).encode()+raw).hexdigest())
                        conn.execute("INSERT OR IGNORE INTO events(generation,offset,at,tool,event_key) VALUES(?,?,?,?,?)",(generation,offset,at,str(row.get("tool_name") or "unknown")[:80],event_key))
                except (ValueError,TypeError,UnicodeDecodeError):
                    pass
                offset=current
            stream.seek(max(0,offset-128))
            checkpoint=hashlib.sha256(stream.read(min(offset,128))).hexdigest()
        conn.execute("INSERT OR REPLACE INTO cursor(path,identity,offset,generation,checkpoint) VALUES(?,?,?,?,?)",(key,identity,offset,generation,checkpoint))
        # 留31天计数，固定保留窗口，避免统计库无限增长。
        conn.execute("DELETE FROM events WHERE at < ?",(__import__("time").time()-31*86400,))
        total=conn.execute("SELECT COUNT(*) FROM events WHERE at>=?",(since,)).fetchone()[0]
        top=conn.execute("SELECT tool,COUNT(*) AS n FROM events WHERE at>=? GROUP BY tool ORDER BY n DESC,tool LIMIT 3",(since,)).fetchall()
    return {"available":True,"successful":total,"top_tools":[{"label":name,"count":n} for name,n in top],"index_read_bytes":consumed}
