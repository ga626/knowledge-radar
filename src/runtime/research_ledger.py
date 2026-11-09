"""Task-scoped research receipts; records decisions without prescribing routes."""

from __future__ import annotations
from runtime.research_storage import guarded_transaction

from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import uuid
from typing import Any

from .paths import runtime_state_dir
from .file_write_lock import read_observed


SCHEMA = "knowledgeradar-research-task-ledger/v3"
_LOCK = threading.RLock()
_PROCESS_LOCK_STATE = threading.local()
_PROCESS_FINGERPRINT_KEY = secrets.token_bytes(32)
_SENSITIVE_KEYS = {"query", "url", "urls", "title", "snippet", "content", "cookie", "cookies", "token", "account", "account_id", "profile_id", "authorization"}
_EVENT_KINDS = {
    "claim_gap_opened", "query_family_created", "candidate_page_received", "candidates_clustered",
    "candidate_selected", "candidate_deferred", "detail_upgraded", "detail_degraded", "claim_supported",
    "counterevidence", "gap_closed", "gap_blocked", "stop_reviewed", "route_opened", "closeout",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(task_id: str) -> Path:
    from runtime.research_contracts import validate_task_id
    safe = validate_task_id(task_id)
    return runtime_state_dir() / "research_tasks" / f"{safe}.json"


def _new_id(task: str) -> str:
    seed = f"{task}|{_now()}".encode("utf-8", errors="ignore")
    return f"kr-research-{hashlib.sha256(seed).hexdigest()[:16]}"


def query_fingerprint(query: str) -> str:
    """Return a non-reversible query label without persisting the query itself.

    An operator may set ``KR_RESEARCH_FINGERPRINT_KEY`` to retain comparison
    across service restarts. Otherwise the HMAC key is process-local: this is
    intentionally safer than silently writing a new secret to runtime state.
    """
    configured = os.environ.get("KR_RESEARCH_FINGERPRINT_KEY", "").encode("utf-8")
    key = configured or _PROCESS_FINGERPRINT_KEY
    scope = "configured" if configured else "process"
    digest = hmac.new(key, str(query or "").encode("utf-8", errors="ignore"), hashlib.sha256).hexdigest()[:24]
    return f"hmac-sha256:{scope}:{digest}"


def _safe_label(value: Any, *, limit: int = 80) -> str:
    return re.sub(r"[^a-zA-Z0-9_.:-]+", "_", str(value or "").strip())[:limit]


def _safe_event_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in dict(metadata or {}).items():
        if str(key).lower() in _SENSITIVE_KEYS:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            payload[_safe_label(key)] = value if not isinstance(value, str) else value[:160]
        elif isinstance(value, list):
            payload[_safe_label(key)] = [_safe_label(item) for item in value[:12]]
    return payload


def _event_id(payload: dict[str, Any], kind: str) -> str:
    seed = f"{payload.get('research_task_id')}|{kind}|{_now()}|{len(payload.get('events') or [])}"
    return f"event-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:16]}"


@guarded_transaction
@contextmanager
def _ledger_guard(task_id: str):
    """同一研究的读改写跨进程互斥；嵌套事件沿用外层锁。"""
    lock_path = _path(str(task_id or "__global__")).with_suffix(".lock")
    key = str(lock_path.resolve())
    with _LOCK:
        held = getattr(_PROCESS_LOCK_STATE, "held", set())
        if key in held:
            yield
            return
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as stream:
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            _PROCESS_LOCK_STATE.held = held | {key}
            try:
                yield
            finally:
                _PROCESS_LOCK_STATE.held = held
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def record_event(
    *, task_id: str, kind: str, source_ecology: str = "", tool: str = "", language: str = "",
    intent_label: str = "", query: str = "", parent_event_id: str = "", metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a privacy-minimized research event; never persist raw retrieval text."""
    with _ledger_guard(task_id):
        payload = _read(task_id)
        if not payload:
            return {"schema": SCHEMA, "status": "unknown_task", "research_task_id": task_id}
        normalized_kind = str(kind or "").strip()
        if normalized_kind not in _EVENT_KINDS:
            return {"schema": SCHEMA, "status": "invalid_event_kind", "research_task_id": task_id}
        event = _append_event(payload, normalized_kind, source_ecology=source_ecology, tool=tool,
            language=language, intent_label=intent_label, query=query, parent_event_id=parent_event_id, metadata=metadata)
        _write(payload)
        return {"schema": SCHEMA, "status": "recorded", "research_task_id": task_id, "event": event}


def _append_event(payload, kind, *, source_ecology='', tool='', language='', intent_label='',
                  query='', parent_event_id='', metadata=None):
    """仅构造同一账本提交的一部分；由持有锁的调用方一次落盘。"""
    event = {
        'event_id': _event_id(payload, kind), 'at': _now(), 'kind': kind,
        'source_ecology': _safe_label(source_ecology), 'tool': _safe_label(tool),
        'language': _safe_label(language, limit=24), 'intent_label': _safe_label(intent_label),
        'parent_event_id': _safe_label(parent_event_id), 'query_fingerprint': query_fingerprint(query) if query else '',
        'metadata': _safe_event_metadata(metadata),
    }
    payload.setdefault('events', []).append(event)
    payload['updated_at'] = _now()
    return event


def _candidate_identity(item: dict[str, Any], ecology: str) -> str:
    from .research_index import identity
    return identity(item, ecology)


def _receipt_id(payload: dict[str, Any], trace_id: str) -> str:
    seed = f"{payload.get('research_task_id')}|{trace_id}|{len(payload.get('tool_receipts') or [])}"
    return f"receipt-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:16]}"


def record_tool_receipt(
    *, task_id: str, trace_id: str, tool: str, status: str, failure_code: str = "",
    source_ecology: str = "", association: str = "explicit_task_scope", result: Any = None,
    elapsed_s: float | None = None,
    receipt_kind: str = "tool_call",
    submission_sha256: str = "",
    research_action: dict | None = None,
) -> dict[str, Any]:
    """Bind a completed, privacy-safe tool trace to an explicit research task.

    The caller must provide a task id.  This deliberately refuses transcript
    order or a process-global "last tool" as an association mechanism.
    """
    with _ledger_guard(task_id):
        payload = _read(task_id)
        if not payload:
            return {"schema": SCHEMA, "status": "unknown_task", "research_task_id": task_id}
        existing = next(
            (item for item in payload.setdefault("tool_receipts", []) if str(item.get("trace_id") or "") == str(trace_id)),
            None,
        )
        if isinstance(existing, dict):
            return {"schema": SCHEMA, "status": "recorded", "research_task_id": task_id, "receipt": existing}
        receipt = {
            "receipt_id": _receipt_id(payload, str(trace_id)),
            "trace_id": _safe_label(trace_id),
            "tool": _safe_label(tool),
            "source_ecology": _safe_label(source_ecology),
            "status": _safe_label(status) or "partial",
            "failure_code": _safe_label(failure_code),
            "association": _safe_label(association) or "explicit_task_scope",
            "at": _now(),
            "receipt_kind": receipt_kind,
        }
        if submission_sha256:
            receipt['submission_sha256'] = submission_sha256
        receipt['research_action'] = ({key:_safe_label(research_action.get(key,''), limit=200) for key in ('family_id','action_id','attempt_id','question_id','parent_event_id','requirement_id','exploration_id')}
                                      if research_action else {'association':'unplanned'})
        if isinstance(elapsed_s, (int, float)) and elapsed_s >= 0:
            receipt["elapsed_s"] = round(float(elapsed_s), 3)
        if isinstance(result, dict):
            from generic_web.url_policy import canonical_url
            receipt['source_keys'] = sorted({hashlib.sha256(canonical_url(str(result[key])).encode()).hexdigest()
                for key in ('url', 'source_url') if result.get(key)})
            # Only preserve server-produced fixed asset identities and task IDs;
            # raw queries, source text and account data remain outside the ledger.
            receipt["evidence_versions"] = [
                str(item["version_id"]) for item in result.get("evidence_assets", [])
                if isinstance(item, dict) and re.fullmatch(r"[a-f0-9]{64}", str(item.get("version_id") or ""))
            ]
            rows = []
            for container in (result, result.get("detail_metadata") or {}, result.get("metadata") or {}):
                if not isinstance(container, dict):
                    continue
                if isinstance(container.get("task"), dict):
                    rows.append(container["task"])
                for key in ("tasks", "pending_tasks", "blocking_tasks"):
                    value = container.get(key)
                    if isinstance(value, list):
                        rows.extend(value)
                    elif isinstance(value, dict):
                        rows.extend(value.get("tasks") or [])
            receipt["background_task_ids"] = sorted({
                str(row["task_id"]) for row in rows if isinstance(row, dict) and row.get("task_id")
            })
        if payload.get('status') in {'accepted_for_decision','recorded','recorded_with_gaps'} and str(tool) != 'finalize_research_task':
            # 新的真实调用会使上次完成检查失效，但不会自动派发研究。
            payload['status'] = 'open'
            payload['previous_closeout'] = payload.get('closeout') or {}
            payload['closeout'] = {}
        payload["tool_receipts"].append(receipt)
        payload["updated_at"] = _now()
        _write(payload)
        return {"schema": SCHEMA, "status": "recorded", "research_task_id": task_id, "receipt": receipt}


def _pending_result_snapshot(result):
    """保留补偿关联所需的公共身份，不把原始正文写进待补记录。"""
    if not isinstance(result, dict):
        return {}
    snapshot = {}
    for key in ("url", "source_url", "evidence_assets", "content_coverage", "failure_type", "error"):
        if key in result:
            snapshot[key] = result[key]
    for key in ("acquired_items", "items", "results"):
        rows = result.get(key)
        if isinstance(rows, list):
            compact = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                identity = {name: row[name] for name in ("url", "id", "content_id", "note_id", "title", "snippet", "source_provider", "published_at") if row.get(name)}
                if identity:
                    compact.append(identity)
            snapshot[key] = compact
    task = result.get("task")
    if isinstance(task, dict) and task.get("task_id"):
        snapshot["task"] = {name: task[name] for name in ("task_id", "status") if name in task}
    return snapshot


def _submission_fingerprint(result):
    from generic_web.url_policy import public_evidence_payload
    material = {key: value for key, value in result.items() if key != 'research_receipt'} if isinstance(result, dict) else result
    material = public_evidence_payload(material)
    if isinstance(material, dict) and isinstance(material.get('task'), dict):
        material['task'] = {key: material['task'].get(key) for key in ('task_id', 'status')}
    return hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False, default=str).encode('utf-8')).hexdigest()


def _record_pending_submission(*, task_id, trace_id, tool, status, result, receipt_options, reason):
    """把同步发布失败登记在同一研究账本，供运行时重放关联，不重跑工具。"""
    with _ledger_guard(task_id):
        payload = _read(task_id)
        if not payload:
            return False
        rows = payload.setdefault("pending_submissions", [])
        current = next((row for row in rows if row.get("trace_id") == str(trace_id)), None)
        entry = {
            "trace_id": _safe_label(trace_id), "tool": _safe_label(tool), "status": _safe_label(status),
            "failure_code": _safe_label(receipt_options.get("failure_code", "")),
            "source_ecology": _safe_label(receipt_options.get("source_ecology", "")),
            "association": _safe_label(receipt_options.get("association", "explicit_task_scope")),
            "receipt_kind": _safe_label(receipt_options.get("receipt_kind", "tool_call")),
            "elapsed_s": receipt_options.get("elapsed_s"), "result": _pending_result_snapshot(result),
            "submission_sha256": _submission_fingerprint(result),
            "reason": _safe_label(reason), "attempts": int((current or {}).get("attempts", 0)), "updated_at": _now(),
        }
        if current:
            entry["attempts"] = current.get("attempts", 0)
            rows[rows.index(current)] = entry
        else:
            rows.append(entry)
        payload["updated_at"] = _now()
        if payload.get("status") in {"accepted_for_decision","recorded","recorded_with_gaps"}:
            payload["status"] = "open"
            payload["previous_closeout"] = payload.get("closeout") or {}
            payload["closeout"] = {}
        _write(payload)
        return True


def _clear_pending_submission(task_id, trace_id):
    with _ledger_guard(task_id):
        payload = _read(task_id)
        if not payload:
            return
        rows = payload.get("pending_submissions") or []
        remaining = [row for row in rows if row.get("trace_id") != str(trace_id)]
        if len(remaining) != len(rows):
            payload["pending_submissions"] = remaining
            payload["updated_at"] = _now()
            _write(payload)


def _bump_pending_submission(task_id, trace_id, status):
    with _ledger_guard(task_id):
        payload = _read(task_id)
        if not payload:
            return
        for row in payload.get("pending_submissions") or []:
            if row.get("trace_id") == str(trace_id):
                row["attempts"] = int(row.get("attempts", 0)) + 1
                row["last_status"] = _safe_label(status)
                row["updated_at"] = _now()
                payload["updated_at"] = _now()
                _write(payload)
                return


def _pending_submissions():
    """扫描身份记录；批次限制针对待补操作，不截断研究文件集合。"""
    pending = []
    for path in sorted((runtime_state_dir() / "research_tasks").glob("*.json")):
        try:
            payload = _read(path.stem)
        except (OSError, ValueError):
            continue
        for row in (payload or {}).get("pending_submissions") or []:
            pending.append((path.stem, row))
    # 已失败的记录排在尚未尝试的记录后面；不让一个坏项霸占每轮维护。
    return sorted(pending, key=lambda item: (int(item[1].get("attempts", 0)),
        item[1].get("updated_at", ""), item[0], item[1].get("trace_id", "")))


def has_pending_submissions():
    return bool(_pending_submissions())


_REPAIR_OFFSETS = {}


def repair_pending_submissions(*, limit=50):
    """只重放已保存的交接；逐项失败隔离，永不重跑原采集。"""
    pending = _pending_submissions()
    repaired, remaining, errors = [], [], []
    key = str(runtime_state_dir().resolve())
    # 即使失败项无法更新 attempts，存活进程也继续轮转，不永久卡在前缀。
    with _LOCK:
        offset = _REPAIR_OFFSETS.get(key, 0) % max(1, len(pending))
        selected = (pending[offset:] + pending[:offset])[:max(0, int(limit))]
        _REPAIR_OFFSETS[key] = (offset + len(selected)) % max(1, len(pending))
    for task_id, row in selected:
        trace_id = row.get("trace_id", "")
        try:
            result = submit_tool_result(
                task_id=task_id, trace_id=trace_id, tool=row.get("tool", ""),
                status=row.get("status", "partial"), result=row.get("result") or {}, _from_pending=True,
                failure_code=row.get("failure_code", ""), source_ecology=row.get("source_ecology", ""),
                association=row.get("association", "explicit_task_scope"),
                receipt_kind=row.get("receipt_kind", "tool_call"), elapsed_s=row.get("elapsed_s"),
                submission_sha256=row.get("submission_sha256", ""),
            )
            if result.get("status") == "recorded":
                _clear_pending_submission(task_id, trace_id)
                repaired.append(trace_id)
                continue
            failure = result.get("status", "failed")
        except Exception as exc:
            failure = type(exc).__name__
        remaining.append(trace_id)
        try:
            _bump_pending_submission(task_id, trace_id, failure)
        except Exception as exc:
            errors.append({"research_task_id": task_id, "trace_id": trace_id,
                           "error_type": type(exc).__name__})
    return {"repaired": repaired, "remaining": remaining, "errors": errors,
            "has_pending": has_pending_submissions()}


def _submit_tool_result_once(*, task_id, trace_id, tool, status, result=None, **receipt_options):
    """唯一研究交接：保护固定证据→写回执→关联候选→标记完成。

    不与任务库假装跨库原子；中断后用原 trace/产物补交，不重新执行工具。
    旧记录无提交标记可补关联；已有标记的提交不会倒退后续人工/Agent 核验。
    """
    if isinstance(result, dict) and status == 'ok' and (
            result.get('evidence_storage_gaps')
            or (result.get('content_coverage') or {}).get('request_satisfied') is False):
        status = 'partial'
    from generic_web.url_policy import public_evidence_payload
    material = {key: value for key, value in result.items() if key != 'research_receipt'} if isinstance(result, dict) else result
    material = public_evidence_payload(material)
    fingerprint = receipt_options.get('submission_sha256') or _submission_fingerprint(result)
    with _ledger_guard(task_id):
        if not _read(task_id):
            return {'status': 'recording_failed', 'reason': 'unknown_task', 'research_task_id': task_id}
        versions = []
        for ref in material.get('evidence_assets', []) if isinstance(material, dict) else []:
            version = ref.get('version_id') if isinstance(ref, dict) else None
            if not re.fullmatch(r'[a-f0-9]{64}', str(version or '')):
                return {'status': 'recording_failed', 'reason': 'invalid_evidence_identity', 'research_task_id': task_id}
            versions.append(version)
        # 先校验证据，成功的 ledger 不能只靠响应里自报的 SHA。保护多出来的引用
        # 是可接受的保守结果；反过来先写成功回执再保护证据会留下悬空引用。
        if versions:
            from kr_core.evidence_assets import EvidenceAssets
            EvidenceAssets().reference_many(versions, task_id, project_id=str(_read(task_id).get("project_id") or ""), task_id=task_id)
        receipt_options = {key: value for key, value in receipt_options.items() if key != 'submission_sha256'}
        recorded = record_tool_receipt(task_id=task_id, trace_id=trace_id, tool=tool, status=status,
            result=result, submission_sha256=fingerprint, **receipt_options)
        receipt = recorded.get('receipt') or {}
        if recorded.get('status') != 'recorded':
            return recorded
        if (receipt.get('tool') != _safe_label(tool) or receipt.get('status') != _safe_label(status)
                or sorted(receipt.get('evidence_versions', [])) != sorted(versions)
                or receipt.get('submission_sha256', fingerprint) != fingerprint):
            return {**recorded, 'status': 'recording_failed', 'reason': 'receipt_identity_conflict'}
        if isinstance(receipt.get('candidate_association'), dict):
            return {**recorded, 'candidate_association': receipt['candidate_association']}
        association = link_tool_result(task_id=task_id, tool=tool, result=result, receipt=receipt)
        allowed = {'unavailable', 'no_detail_evidence', 'ambiguous_no_source_identity', 'ambiguous',
                   'unmatched', 'discovery_candidates', 'unique_source_identity', 'superseded_background_result'}
        if (not isinstance(association, dict) or association.get('association') not in allowed
                or association.get('status', 'recorded') != 'recorded'):
            return {**recorded, 'status': 'recording_failed', 'reason': 'candidate_association_failed',
                    'candidate_association': association}
        payload = _read(task_id)
        stored = next(row for row in payload['tool_receipts'] if row['receipt_id'] == receipt['receipt_id'])
        stored['candidate_association'] = association
        stored['submission_sha256'] = fingerprint
        payload['updated_at'] = _now()
        _write(payload)
        return {**recorded, 'receipt': stored, 'candidate_association': association}


def submit_tool_result(*, task_id, trace_id, tool, status, result=None, _from_pending=False, _record_pending=False, **receipt_options):
    """登记失败不重采；明确区分待补已落盘和无法持久化。"""
    try:
        recorded = _submit_tool_result_once(task_id=task_id, trace_id=trace_id, tool=tool, status=status,
            result=result, **receipt_options)
    except Exception as exc:
        if not _record_pending or _from_pending:
            raise
        recorded = {"status": "recording_failed", "reason": type(exc).__name__,
                    "research_task_id": task_id}
    if recorded.get("status") != "recorded" and _record_pending and not _from_pending:
        persisted = False
        try:
            persisted = _record_pending_submission(task_id=task_id, trace_id=trace_id, tool=tool, status=status,
                result=result, receipt_options=receipt_options,
                reason=recorded.get("reason") or recorded.get("status") or "recording_failed")
        except Exception:
            pass
        recorded = {**recorded, "recovery": {"status": "pending_persisted" if persisted else "not_persisted",
            "automatic_replay": bool(persisted), "retry_collection": False}}
    return recorded


def link_tool_result(*, task_id, tool, result, receipt):
    """真实搜索自动入候选池；详情只在唯一同源匹配时升级，不按对话顺序猜测。"""
    if not isinstance(result, dict):
        return {'association': 'unavailable'}
    from .research_workspace import _ecology_for_tool
    from generic_web.url_policy import canonical_url
    ecology = receipt.get('source_ecology') or _ecology_for_tool(tool)
    if str(tool).startswith('search_') or tool == 'kr_web_search':
        items = result.get('acquired_items') or result.get('items') or result.get('results') or []
        if isinstance(items, list):
            rows = record_candidates(task_id=task_id, source_ecology=ecology or 'unclassified', tool=tool, items=items, receipt_id=receipt['receipt_id'])
            return {'association':'discovery_candidates', 'count':len(rows.get('candidates', []))}
    if not result.get('evidence_assets') and not result.get('error'):
        return {'association': 'no_detail_evidence'}
    url = str(result.get('url') or result.get('source_url') or '')
    repository_url = ''
    github = (result.get('provenance') or {}).get('github') or {}
    if (result.get('platform') == 'GitHub' and github.get('public_only') is True
            and github.get('object_kind') in {'readme', 'file', 'tree', 'overview'}):
        # README/源码有固定提交URL；仓库候选按采集器明确记录的仓库身份关联。
        repository = str(github.get('repository') or '')
        if re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            from urllib.parse import urlsplit
            parsed = urlsplit(url)
            if parsed.hostname == 'github.com' and parsed.path.startswith('/' + repository + '/'):
                repository_url = 'https://github.com/' + repository
    if not url:
        return {'association': 'ambiguous_no_source_identity'}
    source_key = hashlib.sha256(canonical_url(url).encode()).hexdigest()
    with _ledger_guard(task_id):
        payload = _read(task_id)
        if receipt.get('receipt_kind') == 'background_completion' and receipt.get('status') == 'partial':
            tasks = set(receipt.get('background_task_ids', []))
            if any(row.get('receipt_kind') == 'background_completion' and row.get('status') != 'partial'
                   and tasks.intersection(row.get('background_task_ids', [])) for row in payload.get('tool_receipts', [])):
                return {'association': 'superseded_background_result'}
        matches = [row for row in payload.get('candidate_pool', []) if row.get('source_key') == source_key]
        if not matches and repository_url:
            repository_key = hashlib.sha256(canonical_url(repository_url).encode()).hexdigest()
            matches = [row for row in payload.get('candidate_pool', []) if row.get('source_key') == repository_key]
        if len(matches) != 1:
            return {'association':'ambiguous' if matches else 'unmatched', 'matches':len(matches)}
        if (matches[0].get('stage') in {'identity_checked', 'cross_checked', 'counterevidence'}
                and receipt['receipt_id'] in matches[0].get('receipt_ids', [])):
            return {'association': 'unique_source_identity', 'candidate_id': matches[0]['candidate_id'],
                    'status': 'recorded', 'preserved_stage': matches[0]['stage']}
        stage = 'degraded_or_blocked' if receipt.get('status') in {'failed','partial','degraded'} else 'detail_extracted'
        updated = update_candidate_stage(task_id=task_id, candidate_id=matches[0]['candidate_id'], stage=stage, tool=tool, evidence_receipt_ids=[receipt['receipt_id']])
        return {'association':'unique_source_identity', 'candidate_id':matches[0]['candidate_id'], 'status':updated['status']}


def _receipts(payload: dict[str, Any], receipt_ids: list[str]) -> list[dict[str, Any]]:
    expected = {str(item) for item in receipt_ids if str(item)}
    return [
        item for item in payload.get("tool_receipts", [])
        if isinstance(item, dict) and str(item.get("receipt_id") or "") in expected
    ]


def record_candidates(
    *, task_id: str, source_ecology: str, tool: str, items: list[dict[str, Any]], query: str = "",
    language: str = "", intent_label: str = "", parent_event_id: str = "", receipt_id: str = "",
) -> dict[str, Any]:
    """Deduplicate discovery candidates and retain only upgrade-safe identifiers."""
    with _ledger_guard(task_id):
        payload = _read(task_id)
        if not payload:
            return {"schema": SCHEMA, "status": "unknown_task", "research_task_id": task_id, "candidates": []}
        from .research_index import ResearchIndex
        known = {str(item.get("candidate_id") or ""): item for item in payload.setdefault("candidate_pool", []) if isinstance(item, dict)}
        receipt_verified = bool(_receipts(payload, [receipt_id]))
        received: list[dict[str, Any]] = []
        changed = False
        for item in items:
            if not isinstance(item, dict):
                continue
            candidate_id = _candidate_identity(item, source_ecology)
            candidate = known.get(candidate_id)
            if candidate is None and item.get('url'):
                from generic_web.url_policy import canonical_url
                source_key = hashlib.sha256(canonical_url(str(item['url'])).encode()).hexdigest()
                candidate = next((x for x in known.values() if x.get('source_key') == source_key), None)
            if candidate is None:
                candidate = {
                    "candidate_id": candidate_id, "source_ecology": _safe_label(source_ecology),
                    "discovery_tool": _safe_label(tool), "stage": "discovered_candidate",
                    "detail_capability": _safe_label(item.get("detail_capability") or item.get("detail_tool") or "unknown"),
                    "cluster_id": f"cluster-{candidate_id.rsplit('-', 1)[-1]}", "seen_count": 0,
                    "discovery_receipt_ids": [], "verification": "declared",
                }
                if item.get('url'):
                    from generic_web.url_policy import canonical_url
                    candidate['source_key'] = hashlib.sha256(canonical_url(str(item['url'])).encode()).hexdigest()
                payload["candidate_pool"].append(candidate)
                known[candidate_id] = candidate
                changed = True
            # 同一回执的中断补交不是又发现一次；真实新搜索使用新的回执。
            if not receipt_verified or receipt_id not in candidate["discovery_receipt_ids"]:
                candidate["seen_count"] = int(candidate.get("seen_count") or 0) + 1
                changed = True
            if receipt_verified and receipt_id not in candidate["discovery_receipt_ids"]:
                candidate["discovery_receipt_ids"].append(receipt_id)
                candidate["verification"] = "receipt_verified"
            received.append(dict(candidate))
        aliases = {_candidate_identity(item, source_ecology): row["candidate_id"] for item,row in zip((x for x in items if isinstance(x,dict)),received)}
        ResearchIndex().candidates(task_id, items, source_ecology, receipt_id, query=query, aliases=aliases)
        empty_page_recorded = receipt_verified and any(
            row.get('kind') == 'candidate_page_received' and (row.get('metadata') or {}).get('receipt_id') == receipt_id
            for row in payload.get('events', []))
        if not changed and (received or empty_page_recorded):
            return {"schema": SCHEMA, "status": "recorded", "research_task_id": task_id, "candidates": received}
        event = _append_event(
            payload, "candidate_page_received", source_ecology=source_ecology, tool=tool,
            language=language, intent_label=intent_label, query=query, parent_event_id=parent_event_id,
            metadata={"candidate_count": len(received), "receipt_verified": receipt_verified, "receipt_id": receipt_id},
        )
        _append_event(
            payload, "candidates_clustered", source_ecology=source_ecology, tool=tool,
            parent_event_id=str(event.get("event_id") or ""),
            metadata={"candidate_count": len(received), "cluster_count": len({item["cluster_id"] for item in received})},
        )
        _write(payload)
        return {"schema": SCHEMA, "status": "recorded", "research_task_id": task_id, "candidates": received}


def _candidate_receipt_source_matches(candidate, receipt, fixed):
    if candidate.get('source_key') in receipt.get('source_keys', []):
        return True
    association = receipt.get('candidate_association') or {}
    if association.get('candidate_id') == candidate.get('candidate_id') and association.get('status') == 'recorded':
        return True
    # 固定README的URL是blob/commit/path，仓库候选的URL是仓库根。
    # 只按固定资产中的采集器身份关联，不改写旧回执或凭标题猜测。
    from urllib.parse import urlsplit
    from generic_web.url_policy import canonical_url
    for version in receipt.get('evidence_versions', []):
        asset = fixed.get(version, {})
        github = (asset.get('provenance') or {}).get('github') or {}
        repo, commit = str(github.get('repository') or ''), str(github.get('commit') or '')
        if (github.get('public_only') is not True or github.get('object_kind') not in {'readme','file','tree','overview'}
                or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or not re.fullmatch(r'[a-f0-9]{40}', commit)):
            continue
        parsed = urlsplit(asset.get('source_url') or '')
        if parsed.hostname != 'github.com' or not any(parsed.path.startswith('/'+repo+'/'+kind+'/'+commit+'/') for kind in ('blob','tree')):
            continue
        key = hashlib.sha256(canonical_url('https://github.com/'+repo).encode()).hexdigest()
        if key == candidate.get('source_key'):
            return True
    return False


def update_candidate_stage(
    *, task_id: str, candidate_id: str, stage: str, tool: str = "", outcome: str = "", parent_event_id: str = "",
    evidence_receipt_ids: list[str] | None = None, independence_rationale: str = "",
) -> dict[str, Any]:
    """Record candidate selection/detail outcome without raw URL or page content."""
    stage_map = {"selected": "candidate_selected", "deferred": "candidate_deferred", "detail_extracted": "detail_upgraded", "identity_checked": "detail_upgraded", "cross_checked": "claim_supported", "counterevidence": "counterevidence", "degraded_or_blocked": "detail_degraded"}
    with _ledger_guard(task_id):
        payload = _read(task_id)
        if not payload:
            return {"schema": SCHEMA, "status": "unknown_task", "research_task_id": task_id}
        candidate = next((item for item in payload.get("candidate_pool", []) if str(item.get("candidate_id") or "") == candidate_id), None)
        if not isinstance(candidate, dict):
            return {"schema": SCHEMA, "status": "unknown_candidate", "research_task_id": task_id}
        receipt_ids = [str(item) for item in (evidence_receipt_ids or []) if str(item)]
        receipts = _receipts(payload, receipt_ids)
        requires_receipt = stage in {"detail_extracted", "identity_checked", "cross_checked", "counterevidence", "degraded_or_blocked"}
        valid = len(receipts) == len(set(receipt_ids)) and bool(receipts)
        if stage == "cross_checked" and len(receipts) < 2 and not _safe_label(independence_rationale):
            valid = False
        if stage in {'detail_extracted', 'counterevidence', 'cross_checked', 'identity_checked'}:
            # 发现回执只能证明执行过搜索；不能冒充已取得正文或核实主张。
            valid = valid and all(row.get('evidence_versions') and row.get('status') in {'ok', 'partial'}
                                  and not str(row.get('tool', '')).startswith(('search_', 'kr_web_search'))
                                  for row in receipts)
            if valid:
                from kr_core.evidence_assets import EvidenceAssets
                try:
                    fixed = {}
                    for version in {v for row in receipts for v in row['evidence_versions']}:
                        fixed[version] = EvidenceAssets(read_only=True).fetch(version, max_chars=1)['asset']
                    valid = any(_candidate_receipt_source_matches(candidate, row, fixed) for row in receipts)
                except (OSError, ValueError):
                    valid = False
            if stage == 'cross_checked' and not _safe_label(independence_rationale):
                valid = False
        if stage == "degraded_or_blocked" and receipts and not any(str(item.get("status")) in {"failed", "degraded", "partial"} for item in receipts):
            valid = False
        verified = not requires_receipt or valid
        stored_stage = _safe_label(stage) if verified else f"declared_{_safe_label(stage)}"
        if candidate.get('stage') == stored_stage and candidate.get('receipt_ids') == receipt_ids and candidate.get('last_outcome') == _safe_label(outcome):
            return {'status': 'recorded' if verified else 'declared_only', 'candidate': dict(candidate)}
        candidate["stage"] = stored_stage
        candidate["last_outcome"] = _safe_label(outcome)
        candidate["receipt_ids"] = receipt_ids
        candidate["verification"] = "receipt_verified" if verified else "declared"
        candidate['claim_verification'] = 'agent_assessed' if stage == 'cross_checked' and verified else 'unverified'
        candidate['source_independence'] = 'agent_declared' if independence_rationale else 'unknown'
        kind = stage_map.get(stage, "candidate_deferred")
        event = _append_event(payload, kind, source_ecology=str(candidate.get("source_ecology") or ""), tool=tool, parent_event_id=parent_event_id, metadata={"candidate_id": candidate_id, "stage": stored_stage, "outcome": outcome, "receipt_count": len(receipts), "verification": candidate["verification"]})
        _write(payload)
        return {"schema": SCHEMA, "research_task_id": task_id, "event": event,
                "status": "recorded" if verified else "declared_only", "candidate": dict(candidate)}


def assess_task_gaps(payload: dict[str, Any], *, phase: str) -> dict[str, Any]:
    """仅计算已登记检查，不写账本、不补偿，也不判定证据饱和。"""
    candidates = [item for item in payload.get("candidate_pool", []) if isinstance(item, dict)]
    events = [item for item in payload.get("events", []) if isinstance(item, dict)]
    kinds = {str(item.get("kind") or "") for item in events}
    gaps: list[dict[str, str]] = []
    if phase in {"after_archaeology", "after_first_candidates", "before_delivery"} and "claim_gap_opened" not in kinds:
        gaps.append({"code": "claim_gap_not_recorded", "meaning": "没有可见的待证问题，不能判断候选是否补足了关键缺口。"})
    if phase in {"after_first_candidates", "before_delivery"} and not candidates:
        gaps.append({"code": "no_candidates_recorded", "meaning": "没有候选池记录；这不等于平台没有信息。"})
    upgraded = [item for item in candidates if str(item.get("stage") or "") in {"detail_extracted", "identity_checked", "cross_checked", "counterevidence"} and item.get("verification") == "receipt_verified"]
    if phase == "before_delivery" and candidates and not upgraded:
        gaps.append({"code": "candidate_only", "meaning": "候选尚未有详情/身份/交叉核验记录，不能作为高重要性结论的充分证据。"})
    if phase == "before_delivery" and "counterevidence" not in kinds:
        gaps.append({"code": "counterevidence_not_recorded", "meaning": "没有记录会改变结论的反例检查或其不适用边界。"})
    pending_count = sum(isinstance(item, dict) for item in (payload.get("pending_submissions") or []))
    if pending_count:
        gaps.append({"code": "pending_receipt_submissions", "meaning": "仍有工具回执待补偿，当前账本可能尚未包含已取得的材料；查询不会触发重放。"})
    return {
        "schema": "knowledgeradar-research-gap-review/v1", "status": "assessed",
        "research_task_id": str(payload.get("research_task_id") or ""),
        "phase": _safe_label(phase), "gaps": gaps,
        "counts": {"candidate_count": len(candidates), "upgraded_count": len(upgraded),
                   "pending_submission_count": pending_count},
        "stop_assessment": "not_ready" if gaps else "registered_checks_satisfied",
        "coverage_scope": "registered_items_only",
        "unregistered_coverage": "unknown",
        "autonomy_boundary": "This is a decision aid. The Agent still chooses sources, queries, languages, tools, and whether marginal evidence justifies more work.",
        "read_only": True,
    }


def review_task(*, task_id: str, phase: str) -> dict[str, Any]:
    """显式审阅沿用同一诊断，并登记一次审阅事件。"""
    with _ledger_guard(task_id):
        payload = _read(task_id)
        if not payload:
            return {"schema": SCHEMA, "status": "unknown_task", "research_task_id": task_id}
        assessment = assess_task_gaps(payload, phase=phase)
        receipt = record_event(
            task_id=task_id, kind="stop_reviewed", intent_label="evidence_gap_review",
            metadata={"phase": phase, "gap_count": len(assessment["gaps"]), **assessment["counts"]},
        )
        return {
            **assessment, "status": "reviewed", "read_only": False,
            "event": receipt.get("event", {}),
        }


def _read(task_id: str) -> dict[str, Any]:
    path = _path(task_id)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(read_observed(path).decode('utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("research_ledger_unreadable") from exc
    if not isinstance(payload, dict):
        raise ValueError("research_ledger_invalid")
    return payload


def _write(payload: dict[str, Any]) -> dict[str, Any]:
    path = _path(str(payload["research_task_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    current = _read(str(payload["research_task_id"]))
    revision = int(current.get("revision") or 0)
    if int(payload.get("revision") or 0) != revision:
        raise ValueError("research_ledger_revision_conflict")
    next_payload = {**payload, "revision": revision + 1}
    temporary = path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(next_payload, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Console/AV readers can briefly hold the destination open on Windows.
        # Retry only the local atomic replace, never the network operation.
        for attempt in range(5):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.025 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)
    payload.update(revision=revision + 1)
    from .research_index import ResearchIndex
    try:
        ResearchIndex().ledger_projection(payload)
    except (OSError, ValueError, sqlite3.Error) as exc:
        # 原账本已经提交；保留明确修复标记，执行端补偿不重采集。
        import logging
        logging.getLogger(__name__).warning("research_projection_pending: %s", type(exc).__name__)
        path.with_suffix('.projection-pending').touch()
    else:
        path.with_suffix('.projection-pending').unlink(missing_ok=True)
    return payload


def open_task(*, objective: str, budget: str, considered: list[dict[str, Any]], task_id: str = "", display_title: str = "", project_name: str = "", project_id: str = "") -> dict[str, Any]:
    """Create or retrieve the auditable research state for one user delivery."""
    if task_id:
        from runtime.research_contracts import validate_task_id
        validate_task_id(task_id)
    title = ' '.join(str(display_title or '').split())
    if len(title) > 80:
        raise ValueError('research_title_too_long')
    project = ' '.join(str(project_name or '').split())
    if len(project) > 80:
        raise ValueError('research_project_too_long')
    with _ledger_guard(task_id):
        research_task_id = str(task_id or "").strip() or _new_id(objective)
        existing = _read(research_task_id)
        if existing:
            if project_id and existing.get("project_id") != project_id:
                raise ValueError("research_project_mismatch_use_new_task")
            if project_name and existing.get("project_name") != project_name:
                raise ValueError("research_project_name_changed_bind_context_explicitly")
            return existing
        candidates = []
        for item in considered:
            if not isinstance(item, dict) or not item.get("source_ecology"):
                continue
            candidates.append(
                {
                    "source_ecology": str(item["source_ecology"]),
                    "initial_status": "considered",
                    "outcome": "not_recorded",
                    "candidate_tools": list(item.get("candidate_tools") or []),
                    "detail_affordance": list(item.get("detail_affordance") or []),
                    "reason": "",
                }
            )
        return _write(
            {
                "schema": SCHEMA,
                "research_task_id": research_task_id,
                "objective": str(objective),
                "display_title": title,
                "project_name": project,
                "project_id": project_id,
                "report_obligation": "required",
                "budget": str(budget),
                "created_at": _now(),
                "updated_at": _now(),
                "status": "open",
                "candidate_ecologies": candidates,
                "candidate_pool": [],
                "claim_gaps": [{"claim_gap_id": "gap-initial", "importance": "unknown", "status": "open", "reason": "initial_research_question"}],
                "tool_receipts": [],
                "ecology_decisions": [],
                "events": [
                    {"event_id": "event-route-opened", "at": _now(), "kind": "route_opened", "metadata": {"detail": "route receipt persisted"}},
                    {"event_id": "event-claim-gap-opened", "at": _now(), "kind": "claim_gap_opened", "metadata": {"gap_count": 1}},
                ],
                "closeout": {},
            }
        )


def read_task(*, task_id: str) -> dict[str, Any]:
    """Read one persisted research task for continuity handoff tooling."""
    with _ledger_guard(task_id):
        payload = _read(str(task_id or "").strip())
    return payload or {"schema": SCHEMA, "status": "unknown_task", "research_task_id": str(task_id or "")}


def _quality_receipt_ok(*, report_path: str, evidence_path: str, quality_receipt_path: str) -> bool:
    if not report_path or not evidence_path or not quality_receipt_path:
        return False
    try:
        report = Path(report_path)
        evidence = Path(evidence_path)
        receipt = json.loads(Path(quality_receipt_path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False
    if not report.is_file() or not evidence.is_file() or not isinstance(receipt, dict) or receipt.get("status") != "pass":
        return False
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    return (
        str(receipt.get("report_path") or "") == str(report.resolve())
        and str(receipt.get("evidence_path") or "") == str(evidence.resolve())
        and receipt.get("report_sha256") == digest(report)
        and receipt.get("evidence_sha256") == digest(evidence)
    )


def close_task(*, task_id: str, ecology_outcomes: list[dict[str, Any]], stop_rationale: str, key_claims: list[dict[str, Any]], quality_status: str = "", transcript_status: str = "unavailable", report_path: str = "", evidence_path: str = "", quality_receipt_path: str = "") -> dict[str, Any]:
    """记录执行、证据与报告检查事实；缺口不阻断Agent自主停止。"""
    # 固定时点观察，不在ledger锁内取得归档锁，也不触发交付/恢复。
    from runtime.research_archive import ResearchArchive
    from runtime.knowledge_delivery import DeliveryStore
    try:
        tracker=ResearchArchive(DeliveryStore())
        archive=tracker.status(task_id)
        if archive.get('status')=='context_pending':archive=None
        delivery_error = archive.get("error_code", "") if archive else ""
        archive_source_delivery = archive.get('source_delivery', {}) if archive else {}
        archive_delivery_ready = bool(archive and archive.get('local', {}).get('status') == 'local_committed'
                                      and archive.get('vault', {}).get('status') in {'local_committed', 'not_configured'}
                                      and archive_source_delivery.get('status') not in {'pending', 'incomplete'})
        observed_summary = archive.get("summary", "unknown") if archive else "summary_pending"
        # 完成事实只能沿用已固定且已交付的归档状态。旧账本可能在
        # report 已注册时仍留下 summary_pending，不能把它投影成已完成。
        if delivery_error:
            observed_summary = "registered" if archive and archive.get("report") else "summary_pending"
        elif observed_summary == "delivered" and not archive_delivery_ready:
            observed_summary = "registered" if archive and archive.get("report") else "summary_pending"
        delivery_observation = {
            "summary": observed_summary,
            "summary_version_id": (archive.get("report") or {}).get("version_id", "") if archive else "",
            "delivery": ("conflict" if archive.get("failure_kind") == "conflict" else "pending") if delivery_error else
                        ("delivered" if archive_delivery_ready
                         else "pending") if archive else "not_configured",
            "delivery_error_code": delivery_error,
            "delivery_observed_at": _now(),
            "source_delivery": archive_source_delivery,
        }
        if archive and archive.get('source_delivery',{}).get('status') in {'pending','incomplete'} and delivery_observation['delivery']=='delivered':
            delivery_observation['delivery']='pending'
    except (OSError, ValueError, sqlite3.Error):
        delivery_observation = {"summary": "unknown", "summary_version_id": "", "delivery": "unavailable",
                                "delivery_observed_at": _now()}
    with _ledger_guard(task_id):
        payload = _read(task_id)
        if not payload:
            return {"schema": SCHEMA, "status": "unknown_task", "research_task_id": task_id}
        accepted = {"used", "strategic_skip", "blocked", "not_relevant", "not_reached"}
        by_ecology = {
            str(item.get("source_ecology") or ""): item
            for item in ecology_outcomes
            if isinstance(item, dict) and str(item.get("source_ecology") or "")
        }
        missing: list[str] = []
        normalized: list[dict[str, Any]] = []
        for candidate in payload.get("candidate_ecologies") or []:
            ecology = str(candidate.get("source_ecology") or "")
            item = by_ecology.get(ecology, {})
            outcome = str(item.get("outcome") or "")
            reason = str(item.get("reason") or "").strip()
            receipt_ids = [str(value) for value in item.get("receipt_ids", []) if str(value)] if isinstance(item.get("receipt_ids"), list) else []
            linked_receipts = _receipts(payload, receipt_ids)
            claim_gap_ids = [str(value) for value in item.get("claim_gap_ids", []) if str(value)] if isinstance(item.get("claim_gap_ids"), list) else []
            valid_outcome = outcome in accepted and (outcome == "used" or bool(reason))
            if outcome == "used":
                valid_outcome = valid_outcome and bool(linked_receipts)
            elif outcome == "blocked":
                valid_outcome = valid_outcome and any(str(value.get("status")) in {"failed", "degraded", "partial"} for value in linked_receipts)
            elif outcome in {"not_relevant", "strategic_skip"}:
                valid_outcome = valid_outcome and bool(claim_gap_ids) and bool(str(item.get("reopen_condition") or "").strip())
            if not valid_outcome:
                missing.append(ecology)
            candidate["outcome"] = outcome or "not_recorded"
            candidate["reason"] = reason
            # Reopening is an Agent's human-readable condition, not a machine
            # identifier. Preserve Chinese and other languages like reason.
            normalized_item = {"source_ecology": ecology, "outcome": candidate["outcome"], "reason": reason, "receipt_ids": receipt_ids, "claim_gap_ids": claim_gap_ids, "reopen_condition": str(item.get("reopen_condition") or "").strip()[:500]}
            normalized.append(normalized_item)
            payload.setdefault("ecology_decisions", []).append(normalized_item)
        claims = [item for item in key_claims if isinstance(item, dict)]
        high_claims = [item for item in claims if str(item.get("importance") or "").lower() in {"critical", "high"}]
        critical_without_support = [str(item.get("id") or "claim") for item in high_claims if not list(item.get("supporting_evidence_ids") or [])]
        critical_without_receipts = [
            str(item.get("id") or "claim") for item in high_claims
            if not _receipts(payload, [str(value) for value in item.get("supporting_receipt_ids", []) if str(value)] if isinstance(item.get("supporting_receipt_ids"), list) else [])
        ]
        quality_receipt_valid = _quality_receipt_ok(report_path=report_path, evidence_path=evidence_path, quality_receipt_path=quality_receipt_path)
        pending_tasks, unavailable_versions, failed_tasks = [], [], []
        optional_tasks, required_failures = [], []
        recorded_tasks = {task for receipt in payload.get("tool_receipts", []) for task in receipt.get("background_task_ids", [])}
        if recorded_tasks:
            from .tasks import get_task_store, TERMINAL_STATUSES
            store = get_task_store()
            for task in sorted(recorded_tasks):
                row = store.get_task(task)
                seen = {task}
                # 人工恢复后原任务的 skipped 不是失败；跟随已登记的续接身份。
                while row and (next_id := (row.get("metadata") or {}).get("continuation_task_id")):
                    if next_id in seen:
                        row = None
                        break
                    seen.add(next_id)
                    row = store.get_task(next_id)
                required = not row or (row.get("metadata") or {}).get("blocks_final_report") is not False
                state = row.get("status", "missing") if row else "missing"
                if not required:
                    optional_tasks.append({"task_id": task, "status": state})
                if state not in TERMINAL_STATUSES:
                    if required:
                        pending_tasks.append(task)
                elif state != "completed":
                    failure = {"task_id": task, "status": state, "error_code": row.get("error_code", "")}
                    failed_tasks.append(failure)
                    if required:
                        required_failures.append(failure)
        pending_submissions = [row.get("trace_id", "") for row in payload.get("pending_submissions") or []]
        # Revalidate the assets actually cited through receipts. Withdrawing a
        # source or corrupting its original cannot leave a new closeout green.
        cited_receipts = {str(value) for claim in claims for value in claim.get("supporting_receipt_ids", [])}
        versions = {version for receipt in payload.get("tool_receipts", []) if receipt.get("receipt_id") in cited_receipts for version in receipt.get("evidence_versions", [])}
        if versions:
            from kr_core.evidence_assets import EvidenceAssets
            assets = EvidenceAssets(read_only=True)
            for version in sorted(versions):
                try:
                    assets.fetch(version, max_chars=1)
                except (ValueError, OSError):
                    unavailable_versions.append(version)
        from runtime.research_index import ResearchIndex
        research_feedback = ResearchIndex().read(task_id, view='feedback', max_chars=24000)
        requirements = ResearchIndex().read(task_id, view='requirements', limit=100, max_chars=24000)
        index = ResearchIndex()
        with index.connection() as conn:
            from runtime.research_contracts import rows_at, requirement_projection
            contract_rows = rows_at(conn, task_id) if conn is not None else []
            complete_requirements = requirement_projection(index, conn, task_id) if conn is not None else []
        # 只消除已显式关联同一需求且确实取得完整产物的旧尝试阻断；历史失败保留。
        # 对外需求摘要可以分页；内部完成判断不能被响应预算截断。
        fulfilled = {r['record_id'] for r in complete_requirements if r['acquisition']=='fulfilled'}
        attempts = [r for r in contract_rows if r['_kind']=='attempt']
        satisfied_receipts = {r['receipt_id'] for r in attempts if all(
            a['requirement_id'] in fulfilled and any(q['record_id']==a['requirement_id'] and q['revision']==a['requirement_revision']
                for q in complete_requirements) for a in attempts if a['receipt_id']==r['receipt_id'])}
        satisfied_tasks = {bg for r in payload.get('tool_receipts',[]) if r['receipt_id'] in satisfied_receipts for bg in r.get('background_task_ids',[])}
        required_failures = [r for r in required_failures if r['task_id'] not in satisfied_tasks]
        has_gaps = bool(missing or critical_without_support or critical_without_receipts or pending_tasks or required_failures or pending_submissions or unavailable_versions
                        or research_feedback.get('requirement_gaps_count') or research_feedback.get('exploration_pending_receipts_count')
                        or any(research_feedback.get(key+'_count',len(research_feedback.get(key,[])))
                               for key in ('scope_gaps','questions_without_reviewed_support','unresolved_counterevidence','assessments_requiring_review')))
        status = "recorded_with_gaps" if has_gaps else "recorded"

        payload["status"] = status
        payload["updated_at"] = _now()
        payload["closeout"] = {
            "at": _now(),
            "ecology_outcomes": normalized,
            "missing_outcomes": missing,
            "critical_claims_without_support": critical_without_support,
            "critical_claims_without_receipts": critical_without_receipts,
            "pending_background_tasks": pending_tasks,
            "unsuccessful_background_tasks": failed_tasks,
            "required_failed_tasks": required_failures,
            "requirements": requirements,
            "claim_review": ResearchIndex().read(task_id,view='review',limit=100,max_chars=24000),
            "optional_background_tasks": optional_tasks,
            "pending_submission_ids": pending_submissions,
            "completion_facts": {
                "required_execution": "failed" if required_failures else "pending" if pending_tasks else "incomplete" if research_feedback.get('requirement_gaps_count') else "completed",
                "research_recording": "pending" if pending_submissions else "recorded",
                "research_decision": "agent_owned",
                "delivery_read_tool": "manage_research_artifacts.archive_status",
                **delivery_observation,
                "evidence_validation": "gaps" if critical_without_support or critical_without_receipts or unavailable_versions else "registered_receipt_integrity_only",
                "report_check": "verified_pass" if quality_status == "pass" and quality_receipt_valid else "unverified",
                "stop_authority": "agent",
            },
            "unavailable_evidence_versions": unavailable_versions,
            "quality_status": quality_status or "not_supplied",
            "quality_receipt_valid": quality_receipt_valid,
            "report_path": str(report_path or ""),
            "evidence_path": str(evidence_path or ""),
            "quality_receipt_path": str(quality_receipt_path or ""),
            "transcript_status": transcript_status or "unavailable",
            "stop_rationale": str(stop_rationale or ""),
            "research_feedback": research_feedback,
            "host_boundary": "Standard MCP does not receive Codex host call IDs; transcript association remains post-hoc and may be ambiguous under concurrency.",
        }
        payload.setdefault("events", []).append({"at": _now(), "kind": "closeout", "detail": status})
        _write(payload)
        return payload


def repair_pending_projections(limit=20):
    from .research_index import ResearchIndex
    repaired = 0
    for marker in sorted((runtime_state_dir() / "research_tasks").glob("*.projection-pending"))[:limit]:
        with _ledger_guard(marker.stem):
            payload = _read(marker.stem)
            if payload:
                ResearchIndex().ledger_projection(payload)
                marker.unlink(missing_ok=True)
                repaired += 1
    return repaired
