"""从已有调用被动收集低敏评测候选；有界、去重，不自动生成金标或外部调用。"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .paths import runtime_log_dir

LIMIT = 500
RETENTION_DAYS = 30
FAMILIES = {"login_required", "network_timeout", "empty_detail", "empty_results", "rate_limited",
            "provider_unavailable", "anti_bot_verification", "dead_link", "bridge_parse_failed", "cdp_unavailable"}


def candidate_path():
    return runtime_log_dir() / "evaluation-candidates.sqlite3"


def _connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=0.1)
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS candidates (candidate_id TEXT PRIMARY KEY, tool TEXT, family TEXT, sample_kind TEXT, first_at TEXT, last_at TEXT, occurrences INTEGER, trace_id TEXT, review TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS seen_events (event_id TEXT PRIMARY KEY, at TEXT)")
    return db


def record_candidate(event, path=None):
    metadata = event.get("metadata") or {}
    if event.get("event_type", "tool_execution") != "tool_execution" or event.get("parent_trace_id") or metadata.get("parent_trace_id") or metadata.get("invocation_origin") == "internal":
        return
    tool = str(event.get("tool_name") or "")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,80}", tool):
        return
    trace = str(event.get("trace_id") or "")
    trace = trace if re.fullmatch(r"[a-f0-9]{16,64}", trace) else ""
    success = event.get("status") == "ok"
    # 1%正常调用只作覆盖线索；失败富集样本不可用于估计用户总体失败率。
    if success and (not trace or int(hashlib.sha256(trace.encode()).hexdigest()[:8], 16) % 100):
        return
    family = "success_sample" if success else next((x for x in event.get("failure_tags", []) if x in FAMILIES), "other_failure")
    key = hashlib.sha256(f"{tool}:{family}".encode()).hexdigest()[:24]
    at = datetime.now(timezone.utc)
    observed = datetime.fromisoformat(event["timestamp"]) if event.get("timestamp") else at
    if observed.tzinfo is None or observed < at - timedelta(days=RETENTION_DAYS) or observed > at + timedelta(minutes=5):
        return
    event_id = hashlib.sha256(f"{tool}:{trace}:{event.get('timestamp', '')}:{family}".encode()).hexdigest()
    with closing(_connect(path or candidate_path())) as db, db:
        db.execute("DELETE FROM candidates WHERE last_at < ?", ((at - timedelta(days=RETENTION_DAYS)).isoformat(),))
        db.execute("DELETE FROM seen_events WHERE at < ?", ((at - timedelta(days=RETENTION_DAYS)).isoformat(),))
        if not db.execute("INSERT OR IGNORE INTO seen_events VALUES (?,?)", (event_id, observed.isoformat())).rowcount:
            return
        db.execute("DELETE FROM seen_events WHERE event_id IN (SELECT event_id FROM seen_events ORDER BY at DESC LIMIT -1 OFFSET 10000)")
        # 每类一个候选；保留一个低敏trace引用，不保存输入/URL/异常文本/账号。
        db.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,1,?,NULL) ON CONFLICT(candidate_id) DO UPDATE SET first_at=min(first_at,excluded.first_at), last_at=max(last_at,excluded.last_at), occurrences=occurrences+1, trace_id=CASE WHEN excluded.last_at>=last_at THEN excluded.trace_id ELSE trace_id END",
                   (key, tool, family, "representative_sample" if success else "failure_enriched", observed.isoformat(), observed.isoformat(), trace))
        db.execute("DELETE FROM candidates WHERE candidate_id IN (SELECT candidate_id FROM candidates ORDER BY last_at DESC LIMIT -1 OFFSET ?)", (LIMIT,))


def list_candidates(path=None):
    path = Path(path or candidate_path())
    if not path.is_file():
        return []
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=0.1)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM candidates ORDER BY last_at DESC LIMIT ?", (LIMIT,))]


def review_candidate(path, candidate_id, *, case=None, reviewer, reason, input_sha256="", oracle_version=None):
    if not reviewer.strip() or not reason.strip() or len(reason) > 500:
        raise ValueError("reviewer and bounded reason required")
    if case and (not re.fullmatch(r"[a-f0-9]{64}", input_sha256) or not oracle_version):
        raise ValueError("reviewed input identity and oracle version required")
    review = {"decision": "linked_existing_case" if case else "rejected", "case_id": case,
              "input_sha256": input_sha256, "oracle_version": oracle_version,
              "reviewer": reviewer, "reason": reason, "at": datetime.now(timezone.utc).isoformat(),
              "gold_promoted": False}
    with closing(_connect(path)) as db, db:
        cur = db.execute("UPDATE candidates SET review=? WHERE candidate_id=? AND review IS NULL", (json.dumps(review, ensure_ascii=False), candidate_id))
        if cur.rowcount != 1:
            raise ValueError("candidate absent or already reviewed")
    return review
