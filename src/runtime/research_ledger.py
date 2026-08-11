"""Task-scoped research receipts; records decisions without prescribing routes."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import threading
from typing import Any

from .paths import runtime_state_dir


SCHEMA = "knowledgeradar-research-task-ledger/v3"
_LOCK = threading.RLock()
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
    safe = "".join(char for char in str(task_id) if char.isalnum() or char in {"-", "_"})[:96]
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


def record_event(
    *, task_id: str, kind: str, source_ecology: str = "", tool: str = "", language: str = "",
    intent_label: str = "", query: str = "", parent_event_id: str = "", metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a privacy-minimized research event; never persist raw retrieval text."""
    with _LOCK:
        payload = _read(task_id)
        if not payload:
            return {"schema": SCHEMA, "status": "unknown_task", "research_task_id": task_id}
        normalized_kind = str(kind or "").strip()
        if normalized_kind not in _EVENT_KINDS:
            return {"schema": SCHEMA, "status": "invalid_event_kind", "research_task_id": task_id}
        event = {
            "event_id": _event_id(payload, normalized_kind), "at": _now(), "kind": normalized_kind,
            "source_ecology": _safe_label(source_ecology), "tool": _safe_label(tool),
            "language": _safe_label(language, limit=24), "intent_label": _safe_label(intent_label),
            "parent_event_id": _safe_label(parent_event_id), "query_fingerprint": query_fingerprint(query) if query else "",
            "metadata": _safe_event_metadata(metadata),
        }
        payload.setdefault("events", []).append(event)
        payload["updated_at"] = _now()
        _write(payload)
        return {"schema": SCHEMA, "status": "recorded", "research_task_id": task_id, "event": event}


def _candidate_identity(item: dict[str, Any], ecology: str) -> str:
    value = str(item.get("content_id") or item.get("note_id") or item.get("id") or item.get("url") or item.get("title") or "")
    return f"candidate-{hashlib.sha256(f'{ecology}|{value}'.encode('utf-8', errors='ignore')).hexdigest()[:16]}"


def _receipt_id(payload: dict[str, Any], trace_id: str) -> str:
    seed = f"{payload.get('research_task_id')}|{trace_id}|{len(payload.get('tool_receipts') or [])}"
    return f"receipt-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:16]}"


def record_tool_receipt(
    *, task_id: str, trace_id: str, tool: str, status: str, failure_code: str = "",
    source_ecology: str = "", association: str = "explicit_task_scope",
) -> dict[str, Any]:
    """Bind a completed, privacy-safe tool trace to an explicit research task.

    The caller must provide a task id.  This deliberately refuses transcript
    order or a process-global "last tool" as an association mechanism.
    """
    with _LOCK:
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
        }
        payload["tool_receipts"].append(receipt)
        payload["updated_at"] = _now()
        _write(payload)
        return {"schema": SCHEMA, "status": "recorded", "research_task_id": task_id, "receipt": receipt}


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
    with _LOCK:
        payload = _read(task_id)
        if not payload:
            return {"schema": SCHEMA, "status": "unknown_task", "research_task_id": task_id, "candidates": []}
        known = {str(item.get("candidate_id") or ""): item for item in payload.setdefault("candidate_pool", []) if isinstance(item, dict)}
        receipt_verified = bool(_receipts(payload, [receipt_id]))
        received: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            candidate_id = _candidate_identity(item, source_ecology)
            candidate = known.get(candidate_id)
            if candidate is None:
                candidate = {
                    "candidate_id": candidate_id, "source_ecology": _safe_label(source_ecology),
                    "discovery_tool": _safe_label(tool), "stage": "discovered_candidate",
                    "detail_capability": _safe_label(item.get("detail_capability") or item.get("detail_tool") or "unknown"),
                    "cluster_id": f"cluster-{candidate_id.rsplit('-', 1)[-1]}", "seen_count": 0,
                    "discovery_receipt_ids": [], "verification": "declared",
                }
                payload["candidate_pool"].append(candidate)
                known[candidate_id] = candidate
            candidate["seen_count"] = int(candidate.get("seen_count") or 0) + 1
            if receipt_verified and receipt_id not in candidate["discovery_receipt_ids"]:
                candidate["discovery_receipt_ids"].append(receipt_id)
                candidate["verification"] = "receipt_verified"
            received.append(dict(candidate))
        payload["updated_at"] = _now()
        _write(payload)
        event = record_event(
            task_id=task_id, kind="candidate_page_received", source_ecology=source_ecology, tool=tool,
            language=language, intent_label=intent_label, query=query, parent_event_id=parent_event_id,
            metadata={"candidate_count": len(received), "receipt_verified": receipt_verified},
        )
        record_event(
            task_id=task_id, kind="candidates_clustered", source_ecology=source_ecology, tool=tool,
            parent_event_id=str((event.get("event") or {}).get("event_id") or ""),
            metadata={"candidate_count": len(received), "cluster_count": len({item["cluster_id"] for item in received})},
        )
        return {"schema": SCHEMA, "status": "recorded", "research_task_id": task_id, "candidates": received}


def update_candidate_stage(
    *, task_id: str, candidate_id: str, stage: str, tool: str = "", outcome: str = "", parent_event_id: str = "",
    evidence_receipt_ids: list[str] | None = None, independence_rationale: str = "",
) -> dict[str, Any]:
    """Record candidate selection/detail outcome without raw URL or page content."""
    stage_map = {"selected": "candidate_selected", "deferred": "candidate_deferred", "detail_extracted": "detail_upgraded", "identity_checked": "detail_upgraded", "cross_checked": "claim_supported", "counterevidence": "counterevidence", "degraded_or_blocked": "detail_degraded"}
    with _LOCK:
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
        if stage == "degraded_or_blocked" and receipts and not any(str(item.get("status")) in {"failed", "degraded", "partial"} for item in receipts):
            valid = False
        verified = not requires_receipt or valid
        stored_stage = _safe_label(stage) if verified else f"declared_{_safe_label(stage)}"
        candidate["stage"] = stored_stage
        candidate["last_outcome"] = _safe_label(outcome)
        candidate["receipt_ids"] = receipt_ids
        candidate["verification"] = "receipt_verified" if verified else "declared"
        payload["updated_at"] = _now()
        _write(payload)
        kind = stage_map.get(stage, "candidate_deferred")
        event = record_event(task_id=task_id, kind=kind, source_ecology=str(candidate.get("source_ecology") or ""), tool=tool, parent_event_id=parent_event_id, metadata={"candidate_id": candidate_id, "stage": stored_stage, "outcome": outcome, "receipt_count": len(receipts), "verification": candidate["verification"]})
        return {**event, "status": "recorded" if verified else "declared_only", "candidate": dict(candidate)}


def review_task(*, task_id: str, phase: str) -> dict[str, Any]:
    """Describe unresolved evidence gaps without routing the Agent to a tool."""
    with _LOCK:
        payload = _read(task_id)
        if not payload:
            return {"schema": SCHEMA, "status": "unknown_task", "research_task_id": task_id}
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
        receipt = record_event(
            task_id=task_id, kind="stop_reviewed", intent_label="evidence_gap_review",
            metadata={"phase": phase, "gap_count": len(gaps), "candidate_count": len(candidates), "upgraded_count": len(upgraded)},
        )
        return {
            "schema": "knowledgeradar-research-gap-review/v1", "status": "reviewed", "research_task_id": task_id,
            "phase": _safe_label(phase), "gaps": gaps,
            "stop_assessment": "not_ready" if gaps else "no_unrecorded_gap_detected",
            "autonomy_boundary": "This is a decision aid. The Agent still chooses sources, queries, languages, tools, and whether marginal evidence justifies more work.",
            "event": receipt.get("event", {}),
        }


def _read(task_id: str) -> dict[str, Any]:
    path = _path(task_id)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write(payload: dict[str, Any]) -> dict[str, Any]:
    path = _path(str(payload["research_task_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return payload


def open_task(*, objective: str, budget: str, considered: list[dict[str, Any]], task_id: str = "") -> dict[str, Any]:
    """Create or retrieve the auditable research state for one user delivery."""
    with _LOCK:
        research_task_id = str(task_id or "").strip() or _new_id(objective)
        existing = _read(research_task_id)
        if existing:
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
    with _LOCK:
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
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    return (
        str(receipt.get("report_path") or "") == str(report.resolve())
        and str(receipt.get("evidence_path") or "") == str(evidence.resolve())
        and receipt.get("report_sha256") == digest(report)
        and receipt.get("evidence_sha256") == digest(evidence)
    )


def close_task(*, task_id: str, ecology_outcomes: list[dict[str, Any]], stop_rationale: str, key_claims: list[dict[str, Any]], quality_status: str = "", transcript_status: str = "unavailable", report_path: str = "", evidence_path: str = "", quality_receipt_path: str = "") -> dict[str, Any]:
    """Close a task without dictating tools; missing consideration outcomes fail closed."""
    with _LOCK:
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
                valid_outcome = valid_outcome and bool(claim_gap_ids) and bool(linked_receipts) and bool(str(item.get("reopen_condition") or "").strip())
            if not valid_outcome:
                missing.append(ecology)
            candidate["outcome"] = outcome or "not_recorded"
            candidate["reason"] = reason
            normalized_item = {"source_ecology": ecology, "outcome": candidate["outcome"], "reason": reason, "receipt_ids": receipt_ids, "claim_gap_ids": claim_gap_ids, "reopen_condition": _safe_label(item.get("reopen_condition") or "")}
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
        deep_receipt_required = str(payload.get("budget") or "").lower() == "deep"
        status = "accepted_for_decision" if not missing and not critical_without_support and not critical_without_receipts and quality_status == "pass" and (not deep_receipt_required or quality_receipt_valid) else "needs_repair"
        payload["status"] = status
        payload["updated_at"] = _now()
        payload["closeout"] = {
            "at": _now(),
            "ecology_outcomes": normalized,
            "missing_outcomes": missing,
            "critical_claims_without_support": critical_without_support,
            "critical_claims_without_receipts": critical_without_receipts,
            "quality_status": quality_status or "not_supplied",
            "quality_receipt_valid": quality_receipt_valid,
            "quality_receipt_path": str(quality_receipt_path or ""),
            "transcript_status": transcript_status or "unavailable",
            "stop_rationale": str(stop_rationale or ""),
            "host_boundary": "Standard MCP does not receive Codex host call IDs; transcript association remains post-hoc and may be ambiguous under concurrency.",
        }
        payload.setdefault("events", []).append({"at": _now(), "kind": "closeout", "detail": status})
        _write(payload)
        return payload
