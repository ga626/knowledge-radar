"""复用媒体manifest的可恢复清理；只处理已登记文件并复核活动租约。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .paths import runtime_state_dir


def digest(path):
    h=hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b""):
            h.update(chunk)
    return h.hexdigest()


class StorageLeases:
    def __init__(self, path=None):
        self.path=Path(path or runtime_state_dir()/"storage-leases.sqlite3")

    def connect(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        conn=sqlite3.connect(self.path,timeout=5)
        conn.execute("CREATE TABLE IF NOT EXISTS leases(path TEXT,pid INTEGER,expires REAL,PRIMARY KEY(path,pid))")
        return conn

    def acquire(self,path,ttl_s=3600):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not Path(path).exists():
                raise FileNotFoundError("cannot lease a missing cache path")
            conn.execute("INSERT OR REPLACE INTO leases VALUES(?,?,?)",(str(Path(path).resolve()),os.getpid(),time.time()+ttl_s))

    def release(self,path):
        with self.connect() as conn:
            conn.execute("DELETE FROM leases WHERE path=? AND pid=?",(str(Path(path).resolve()),os.getpid()))

    def release_process(self):
        with self.connect() as conn:
            conn.execute("DELETE FROM leases WHERE pid=?", (os.getpid(),))

    def protected(self,path,conn=None):
        from .mcp_observability import _pid_is_running
        resolved=Path(path).resolve()
        if conn is None:
            with self.connect() as reader:
                rows=reader.execute("SELECT path,pid,expires FROM leases").fetchall()
        else:
            rows=conn.execute("SELECT path,pid,expires FROM leases").fetchall()
        return any((resolved==Path(p) or Path(p) in resolved.parents) and until>time.time() and _pid_is_running(pid) for p,pid,until in rows)


def quarantine_files(paths, *, cache_root, data_root):
    cache_root,data_root=Path(cache_root).resolve(),Path(data_root).resolve()
    if cache_root==data_root or data_root not in cache_root.parents:
        raise ValueError("cache root must be a proper child of product data root")
    batch=uuid.uuid4().hex
    folder=data_root/"quarantine"/"media-cache"/batch
    receipt=data_root/"receipts"/f"media-cache-quarantine-{batch}.jsonl"
    receipt.parent.mkdir(parents=True,exist_ok=True)
    leases=StorageLeases()
    rows=[]
    for raw in paths:
        path=Path(raw)
        resolved=path.resolve()
        if any(p.is_symlink() for p in [path,*path.parents]) or not resolved.is_relative_to(cache_root) or not resolved.is_file() or leases.protected(resolved):
            rows.append({"status":"protected_or_missing","relative":str(path.name)})
            continue
        relative=resolved.relative_to(cache_root)
        target=folder/relative
        row={"schema":"knowledgeradar-quarantine-file/v2","batch":batch,"relative":relative.as_posix(),
             "cache_root":str(cache_root),"quarantine_root":str(folder),"size":resolved.stat().st_size,
             "sha256":digest(resolved),"status":"planned"}
        # 先落逐文件计划，移动中断后可从相同路径核对，不丢恢复映射。
        with receipt.open("a",encoding="utf-8") as out:
            out.write(json.dumps(row)+"\n");out.flush();os.fsync(out.fileno())
        try:
            target.parent.mkdir(parents=True,exist_ok=True)
            with leases.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if leases.protected(resolved,conn):
                    row["status"]="protected"
                else:
                    resolved.replace(target)
                    row["status"]="quarantined"
        except OSError as exc:
            row.update(status="error",error=type(exc).__name__)
        with receipt.open("a",encoding="utf-8") as out:
            out.write(json.dumps(row)+"\n");out.flush();os.fsync(out.fileno())
        rows.append(row)
    return {"receipt":str(receipt),"files":rows,"quarantined_count":sum(r["status"]=="quarantined" for r in rows)}


def restore_quarantine(receipt, *, data_root, cache_root=None):
    root=Path(data_root).resolve();receipt=Path(receipt).resolve()
    from .paths import runtime_media_cache_dir
    trusted_cache=Path(cache_root or runtime_media_cache_dir()).resolve()
    if not receipt.is_relative_to(root/"receipts"):
        raise ValueError("receipt outside product root")
    latest={}
    for line in receipt.read_text(encoding="utf-8").splitlines():
        row=json.loads(line);latest[row["relative"]]=row
    outcomes=[]
    for row in latest.values():
        if row.get("status") not in {"planned", "quarantined"}:
            continue
        raw_source=Path(row["quarantine_root"])/row["relative"]
        if any(p.is_symlink() for p in [raw_source, *raw_source.parents]):
            raise ValueError("symlink in restoration source")
        source=raw_source.resolve()
        target=(Path(row["cache_root"])/row["relative"]).resolve()
        if Path(row["cache_root"]).resolve()!=trusted_cache or not source.is_relative_to(root/"quarantine/media-cache") or not target.is_relative_to(trusted_cache) or any(p.is_symlink() for p in [Path(row["cache_root"])/row["relative"],*(Path(row["cache_root"])/row["relative"]).parents]):
            raise ValueError("invalid restoration mapping")
        if target.exists(): status="target_exists"
        elif not source.is_file(): status="missing"
        elif digest(source)!=row["sha256"]: status="integrity_mismatch"
        else:
            target.parent.mkdir(parents=True,exist_ok=True);source.replace(target);status="restored"
        outcomes.append({"relative":row["relative"],"status":status})
    result={"schema":"knowledgeradar-quarantine-restore/v1","files":outcomes}
    receipt.with_suffix(".restore.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
    return result
