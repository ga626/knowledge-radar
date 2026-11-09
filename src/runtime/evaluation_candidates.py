"""从已有调用被动收集低敏评测候选；有界、去重，不自动生成金标或外部调用。"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
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
    db = sqlite3.connect(path, timeout=3)
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS candidates (candidate_id TEXT PRIMARY KEY, tool TEXT, family TEXT, sample_kind TEXT, first_at TEXT, last_at TEXT, occurrences INTEGER, trace_id TEXT, review TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS seen_events (event_id TEXT PRIMARY KEY, at TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS observation_totals (name TEXT PRIMARY KEY, value INTEGER NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS task_observations (task_key TEXT PRIMARY KEY, revision INTEGER, state TEXT, project_key TEXT, updated_at TEXT)")
    columns = {r[1] for r in db.execute('PRAGMA table_info(task_observations)')}
    for name in ('delivery_state', 'audit_reason'):
        if name not in columns:
            db.execute(f"ALTER TABLE task_observations ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
    db.execute("CREATE TABLE IF NOT EXISTS candidate_members (event_id TEXT PRIMARY KEY, candidate_id TEXT, trace_id TEXT, at TEXT, task_key TEXT, semantic_classification TEXT)")
    db.execute("CREATE INDEX IF NOT EXISTS member_cluster ON candidate_members(candidate_id)")
    return db


def _count(db, name, n=1):
    db.execute('INSERT INTO observation_totals VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=value+excluded.value', (name, n))


def _key(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:24] if value else ''


def _prune(db, at):
    cutoff = (at - timedelta(days=RETENTION_DAYS)).isoformat()
    for name, query, params in [
        ('expired_candidates', 'DELETE FROM candidates WHERE last_at < ?', (cutoff,)),
        ('evicted_candidates', 'DELETE FROM candidates WHERE candidate_id IN (SELECT candidate_id FROM candidates ORDER BY last_at DESC,candidate_id LIMIT -1 OFFSET ?)', (LIMIT,)),
        ('expired_events', 'DELETE FROM seen_events WHERE at < ?', (cutoff,)),
        ('evicted_events', 'DELETE FROM seen_events WHERE event_id IN (SELECT event_id FROM seen_events ORDER BY at DESC,event_id LIMIT -1 OFFSET 10000)', ()),
        ('expired_tasks', 'DELETE FROM task_observations WHERE updated_at < ?', (cutoff,)),
        ('evicted_tasks', 'DELETE FROM task_observations WHERE task_key IN (SELECT task_key FROM task_observations ORDER BY updated_at DESC,task_key LIMIT -1 OFFSET 10000)', ()),
    ]:
        _count(db, name, db.execute(query, params).rowcount)
    db.execute('DELETE FROM candidate_members WHERE event_id NOT IN (SELECT event_id FROM seen_events) OR candidate_id NOT IN (SELECT candidate_id FROM candidates)')


def observe_task(payload, path=None):
    """仅索引 KR 可见任务；不存目标、显示名、正文和路径。"""
    key = _key(payload.get('research_task_id'))
    if not key:
        return
    revision = int(payload.get('revision') or 0)
    state = payload.get('status') if payload.get('status') in {'open', 'closed', 'closed_with_gaps', 'recorded', 'recorded_with_gaps', 'cancelled', 'completed', 'stopped', 'finalized'} else 'other'
    project = _key((payload.get('project_context') or {}).get('project_id') or payload.get('project_id'))
    at = datetime.now(timezone.utc)
    with closing(_connect(path or candidate_path())) as db, db:
        excluded = {s.strip() for s in os.environ.get('KR_EVALUATION_EXCLUDED_PROJECTS','').split(',') if s.strip()}
        project_id=(payload.get('project_context') or {}).get('project_id') or payload.get('project_id')
        if payload.get('evaluation_excluded') is True or os.environ.get('KR_EVALUATION_OBSERVATION')=='off' or project_id in excluded:
            db.execute('DELETE FROM task_observations WHERE task_key=?',(key,))
            _count(db,'excluded_task_updates')
            return
        closeout = payload.get('closeout') or {}
        facts=closeout.get('completion_facts') or closeout
        delivery = facts.get('delivery') if facts.get('delivery') in {'delivered','pending','conflict','unavailable','not_configured'} else 'unknown'
        gap = state != 'open' and delivery in {'pending','conflict','unavailable'}
        sampled = state != 'open' and int(key[:8],16)%100==0
        reason = 'delivery_missing' if gap else 'success_semantic_sample' if sampled else ''
        db.execute('INSERT INTO task_observations VALUES (?,?,?,?,?,?,?) ON CONFLICT(task_key) DO UPDATE SET revision=excluded.revision,state=excluded.state,project_key=CASE WHEN excluded.project_key!=\'\' THEN excluded.project_key ELSE project_key END,updated_at=excluded.updated_at,delivery_state=excluded.delivery_state,audit_reason=excluded.audit_reason WHERE excluded.revision>revision',
                   (key, revision, state, project, at.isoformat(),delivery,reason))
        _prune(db, at)


def _readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    return db


def observation_summary(path=None):
    path = Path(path or candidate_path())
    result = {'schema': 'kr-evaluation-observation/v1', 'tool_calls': 0, 'failed_tool_calls': 0,
              'observed_tasks': 0, 'task_states': {}, 'candidate_clusters': 0, 'evicted_candidates': 0,
              'semantic_success': 'not_judged', 'retention_days': RETENTION_DAYS,
              'scope': 'KR_visible_only', 'task_window': 'retained_tasks', 'tool_window': 'since_observer_enabled',
              'executor': 'waiting_for_agent'}
    if not path.is_file():
        return result
    with closing(_readonly(path)) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'observation_totals' in tables:
            result.update(dict(db.execute('SELECT name,value FROM observation_totals')))
        if 'task_observations' in tables:
            result['task_states'] = dict(db.execute('SELECT state,count(*) FROM task_observations GROUP BY state'))
            result['observed_tasks'] = sum(result['task_states'].values())
            columns = {r[1] for r in db.execute('PRAGMA table_info(task_observations)')}
            if 'audit_reason' in columns:
                result['delivery_gaps'] = db.execute("SELECT count(*) FROM task_observations WHERE audit_reason='delivery_missing'").fetchone()[0]
                result['task_audits_pending'] = db.execute("SELECT count(*) FROM task_observations WHERE audit_reason!=''").fetchone()[0]
        result['candidate_clusters'] = db.execute('SELECT count(*) FROM candidates').fetchone()[0]
        result['pending_clusters'] = db.execute("SELECT count(*) FROM candidates WHERE review IS NULL OR json_extract(review,'$.decision') IN ('needs_evidence','disputed')").fetchone()[0]
        oldest = db.execute('SELECT min(first_at) FROM candidates WHERE review IS NULL').fetchone()[0]
        result['oldest_pending_at'] = oldest
        result['expired_unreviewed_clusters'] = db.execute('SELECT count(*) FROM candidates WHERE review IS NULL AND last_at < ?', ((datetime.now(timezone.utc)-timedelta(days=RETENTION_DAYS)).isoformat(),)).fetchone()[0]
    return result


def candidate_members(candidate_id, path=None):
    path = Path(path or candidate_path())
    if not path.is_file():
        return []
    with closing(_readonly(path)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='candidate_members'").fetchone():
            return []
        return [dict(r) for r in db.execute('SELECT * FROM candidate_members WHERE candidate_id=? ORDER BY at,event_id LIMIT 100', (candidate_id,))]


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
    sampled = not success or bool(trace and int(hashlib.sha256(trace.encode()).hexdigest()[:8], 16) % 100 == 0)
    family = "success_sample" if success else next((x for x in event.get("failure_tags", []) if x in FAMILIES), "other_failure")
    key = hashlib.sha256(f"{tool}:{family}".encode()).hexdigest()[:24]
    at = datetime.now(timezone.utc)
    observed = datetime.fromisoformat(event["timestamp"]) if event.get("timestamp") else at
    if observed.tzinfo is None or observed < at - timedelta(days=RETENTION_DAYS) or observed > at + timedelta(minutes=5):
        return
    event_id = hashlib.sha256(f"{tool}:{trace}:{event.get('timestamp', '')}:{family}".encode()).hexdigest()
    with closing(_connect(path or candidate_path())) as db, db:
        if not db.execute("INSERT OR IGNORE INTO seen_events VALUES (?,?)", (event_id, observed.isoformat())).rowcount:
            return
        _count(db, 'tool_calls')
        if not success:
            _count(db, 'failed_tool_calls')
        if not sampled:
            _prune(db, at)
            return
        # 每类一个候选；保留一个低敏trace引用，不保存输入/URL/异常文本/账号。
        db.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,1,?,NULL) ON CONFLICT(candidate_id) DO UPDATE SET first_at=min(first_at,excluded.first_at), last_at=max(last_at,excluded.last_at), occurrences=occurrences+1, trace_id=CASE WHEN excluded.last_at>=last_at THEN excluded.trace_id ELSE trace_id END",
                   (key, tool, family, "representative_sample" if success else "failure_enriched", observed.isoformat(), observed.isoformat(), trace))
        task_key = str(metadata.get('research_task_key') or '')
        task_key = task_key if re.fullmatch('[a-f0-9]{24}',task_key) else _key(metadata.get('research_task_id'))
        db.execute('INSERT INTO candidate_members VALUES (?,?,?,?,?,?)', (event_id, key, trace, observed.isoformat(), task_key, 'unreviewed'))
        _prune(db, at)


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
