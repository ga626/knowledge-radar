"""Internal native execution contracts for read-only KnowledgeRadar patrols.

This module intentionally does not register MCP tools. It gives the runtime a
small command/envelope shape that can be consumed by existing tools such as
health_check without changing the public tool surface.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import time
from typing import Any, Callable, Dict, Iterable, List


READONLY_ALLOWED_SIDE_EFFECTS = ["read_runtime_state"]
LOW_RISK_NETWORK_SIDE_EFFECTS = ["read_runtime_state", "network_request"]
DEFAULT_HIGH_RISK_PURPOSES = {"search", "detail", "batch_detail", "ocr", "multimodal", "main_chain"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_hash(value: Any, *, length: int = 16) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:length]


@dataclass(frozen=True)
class CapabilityCommand:
    schema: str = "knowledgeradar-capability-command/v1"
    command_id: str = ""
    capability_id: str = ""
    tool_name: str = ""
    purpose: str = "observability"
    risk_scope: str = "runtime"
    input: Dict[str, Any] = field(default_factory=dict)
    input_hash: str = ""
    budget: Dict[str, Any] = field(default_factory=dict)
    requires_network: bool = False
    requires_browser: bool = False
    requires_account: bool = False
    allowed_side_effects: List[str] = field(default_factory=lambda: list(READONLY_ALLOWED_SIDE_EFFECTS))
    idempotency_key: str = ""
    requires_user_consent: bool = False
    policy_context: Dict[str, Any] = field(default_factory=dict)
    expected_evidence_schema: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if not data.get("input_hash"):
            data["input_hash"] = stable_hash(data.get("input", {}))
        if not data.get("command_id"):
            data["command_id"] = stable_hash(
                {
                    "capability_id": data.get("capability_id"),
                    "purpose": data.get("purpose"),
                    "input_hash": data.get("input_hash"),
                },
                length=20,
            )
        if not data.get("idempotency_key"):
            data["idempotency_key"] = stable_hash(
                {
                    "capability_id": data.get("capability_id"),
                    "input_hash": data.get("input_hash"),
                    "side_effects": data.get("allowed_side_effects", []),
                },
                length=20,
            )
        return data


@dataclass(frozen=True)
class ExecutionEnvelope:
    schema: str = "knowledgeradar-execution-envelope/v1"
    command_id: str = ""
    trace_id: str = ""
    span_id: str = ""
    attempt: int = 1
    status: str = "ok"
    selected_strategy: str = "readonly"
    policy_decision: Dict[str, Any] = field(default_factory=dict)
    health_gate: Dict[str, Any] = field(default_factory=dict)
    budget: Dict[str, Any] = field(default_factory=dict)
    checkpoint_ref: str = ""
    input_hash: str = ""
    output_hash: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    degradation: Dict[str, Any] = field(default_factory=dict)
    failure_taxonomy: Dict[str, Any] = field(default_factory=dict)
    retry_advice: Dict[str, Any] = field(default_factory=dict)
    side_effect_boundary: Dict[str, Any] = field(default_factory=dict)
    next_actions: List[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    summary: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EvidenceRecord:
    schema: str = "knowledgeradar-evidence-record/v1"
    evidence_id: str = ""
    trace_id: str = ""
    command_id: str = ""
    source_type: str = "runtime_summary"
    source_url: str = ""
    source_platform: str = "knowledgeradar"
    collector: str = "native_runner"
    retrieved_at: str = field(default_factory=utc_now_iso)
    published_at: str = ""
    claim_supported: str = ""
    evidence_strength: str = "strong"
    freshness: str = "live_runtime"
    verification_status: str = "observed"
    failure_or_gap: str = ""
    privacy_level: str = "redacted_summary"
    redaction_applied: bool = True

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if not data.get("evidence_id"):
            data["evidence_id"] = "EV-" + stable_hash(
                {
                    "trace_id": data.get("trace_id"),
                    "command_id": data.get("command_id"),
                    "claim_supported": data.get("claim_supported"),
                },
                length=12,
            )
        return data


@dataclass(frozen=True)
class DurableState:
    schema: str = "knowledgeradar-durable-state/v1"
    checkpoint_ref: str = ""
    resume_from: str = ""
    attempt_policy: Dict[str, Any] = field(default_factory=dict)
    side_effect_boundary: Dict[str, Any] = field(default_factory=dict)
    human_pause_required: bool = False
    approval_token: str = ""
    event_history_minimal: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if not data.get("approval_token") and data.get("human_pause_required"):
            data["approval_token"] = "approval-" + stable_hash(data.get("checkpoint_ref"), length=12)
        return data


@dataclass(frozen=True)
class TraceEvent:
    schema: str = "knowledgeradar-trace-event/v1"
    trace_id: str = ""
    span_id: str = ""
    command_id: str = ""
    capability_id: str = ""
    event: str = ""
    status: str = ""
    occurred_at: str = field(default_factory=utc_now_iso)
    evidence_ids: List[str] = field(default_factory=list)
    failure_tag: str = ""
    budget: Dict[str, Any] = field(default_factory=dict)
    policy_decision: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def readonly_policy_decision(command: Dict[str, Any]) -> Dict[str, Any]:
    denied = []
    if command.get("requires_browser"):
        denied.append("requires_browser")
    if command.get("requires_account"):
        denied.append("requires_account")
    side_effects = set(command.get("allowed_side_effects") or [])
    if not side_effects.issubset(set(READONLY_ALLOWED_SIDE_EFFECTS)):
        denied.append("side_effect_not_readonly")
    if command.get("requires_user_consent"):
        denied.append("requires_user_consent")
    if denied:
        return {
            "schema": "knowledgeradar-policy-decision/v1",
            "allowed": False,
            "reason": "READONLY_POLICY_DENIED",
            "denied": denied,
            "manual_confirm_required": True,
        }
    return {
        "schema": "knowledgeradar-policy-decision/v1",
        "allowed": True,
        "reason": "READONLY_ALLOWED",
        "denied": [],
        "manual_confirm_required": False,
    }


def plan_only_policy_decision(command: Dict[str, Any]) -> Dict[str, Any]:
    denied = []
    platform = str((command.get("input") or {}).get("platform") or "")
    purpose = str(command.get("purpose") or "")
    xhs_readonly_route = platform == "xiaohongshu" and purpose in {"search", "detail", "main_chain"}
    if command.get("requires_browser"):
        denied.append("requires_browser")
    if command.get("requires_account"):
        denied.append("requires_account")
    if command.get("requires_user_consent"):
        denied.append("requires_user_consent")
    if xhs_readonly_route:
        return {
            "schema": "knowledgeradar-policy-decision/v1",
            "allowed": True,
            "reason": "PLAN_ONLY_XHS_READONLY_ROUTE_REQUIRED",
            "denied": [],
            "manual_confirm_required": False,
            "route_admission_required": True,
        }
    if denied:
        return {
            "schema": "knowledgeradar-policy-decision/v1",
            "allowed": False,
            "reason": "PLAN_ONLY_POLICY_DENIED",
            "denied": denied,
            "manual_confirm_required": True,
        }
    return {
        "schema": "knowledgeradar-policy-decision/v1",
        "allowed": True,
        "reason": "PLAN_ONLY_ALLOWED",
        "denied": [],
        "manual_confirm_required": False,
    }


def _compact_value(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {"value_type": type(value).__name__}
    return {
        "schema": value.get("schema") or value.get("schema_version") or "",
        "status": value.get("status", "ok"),
        "summary": bool(value.get("summary", False)),
        "keys": sorted(str(key) for key in value.keys())[:30],
    }


def _default_retry_advice(status: str) -> Dict[str, Any]:
    if status == "ok":
        return {"retryable": False, "reason": "not_needed"}
    if status == "planned":
        return {"retryable": False, "reason": "await_explicit_execution"}
    if status == "blocked":
        return {"retryable": False, "reason": "policy_denied"}
    return {"retryable": True, "reason": "readonly_observation_can_retry"}


def build_event_history(command: Dict[str, Any], envelope: Dict[str, Any], evidence: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {
            "event": "capability.command.created",
            "command_id": command.get("command_id"),
            "capability_id": command.get("capability_id"),
        },
        {
            "event": "policy.gate.evaluated",
            "command_id": command.get("command_id"),
            "allowed": (envelope.get("policy_decision") or {}).get("allowed"),
            "reason": (envelope.get("policy_decision") or {}).get("reason"),
        },
        {
            "event": "evidence.registered",
            "trace_id": envelope.get("trace_id"),
            "evidence_id": evidence.get("evidence_id"),
        },
        {
            "event": "checkpoint.created",
            "checkpoint_ref": envelope.get("checkpoint_ref"),
            "status": envelope.get("status"),
        },
    ]


def build_trace_events(command: Dict[str, Any], envelope: Dict[str, Any], evidence: Dict[str, Any]) -> List[Dict[str, Any]]:
    base = {
        "trace_id": envelope.get("trace_id", ""),
        "span_id": envelope.get("span_id", ""),
        "command_id": command.get("command_id", ""),
        "capability_id": command.get("capability_id", ""),
        "status": envelope.get("status", ""),
        "budget": command.get("budget", {}),
        "policy_decision": envelope.get("policy_decision", {}),
    }
    evidence_ids = list(envelope.get("evidence_ids") or [])
    failure = envelope.get("failure_taxonomy") or {}
    return [
        TraceEvent(**base, event="capability.command.created").to_dict(),
        TraceEvent(**base, event="policy.gate.evaluated").to_dict(),
        TraceEvent(**base, event="tool.call.finished", failure_tag=str(failure.get("code") or "")).to_dict(),
        TraceEvent(**base, event="evidence.registered", evidence_ids=evidence_ids).to_dict(),
        TraceEvent(**base, event="checkpoint.created", metadata={"checkpoint_ref": envelope.get("checkpoint_ref", "")}).to_dict(),
    ]


def build_durable_state(command: Dict[str, Any], envelope: Dict[str, Any], evidence: Dict[str, Any]) -> Dict[str, Any]:
    status = str(envelope.get("status") or "")
    side_effect_boundary = envelope.get("side_effect_boundary") or {}
    human_pause = status == "blocked" or bool((envelope.get("policy_decision") or {}).get("manual_confirm_required"))
    retryable = bool((envelope.get("retry_advice") or {}).get("retryable"))
    if status == "ok":
        resume_from = "next_step"
    elif status == "planned":
        resume_from = "explicit_execution"
    else:
        resume_from = "manual_review"
    return DurableState(
        checkpoint_ref=str(envelope.get("checkpoint_ref") or ""),
        resume_from=resume_from,
        attempt_policy={
            "attempt": int(envelope.get("attempt") or 1),
            "max_attempts": 1 if human_pause else 2,
            "retryable": retryable,
            "retry_reason": (envelope.get("retry_advice") or {}).get("reason", ""),
        },
        side_effect_boundary=side_effect_boundary,
        human_pause_required=human_pause,
        event_history_minimal=build_event_history(command, envelope, evidence),
    ).to_dict()


def runtime_contract_summary() -> Dict[str, Any]:
    return {
        "schema": "knowledgeradar-runtime-contract/v1",
        "status": "ok",
        "public_mcp_tool_surface": "unchanged",
        "resource_style_status": [
            "health.summary",
            "capabilities.summary",
            "task_status.summary",
            "decision_logs.compact",
            "profile_registry.summary",
            "account_pool.summary",
        ],
        "prompt_workflow_templates": [
            "standard_research_flow",
            "cross_source_fact_check",
            "evidence_register_output",
        ],
        "schemas": native_execution_schema_summary(),
        "allowed_recent_execution": [
            "search_academic",
            "extract_web_page",
            "kr_web_search(open_web)",
            "get_content_detail(low_risk_platforms)",
        ],
        "route_admission_required": [
            "xiaohongshu.search",
            "xiaohongshu.detail",
            "xiaohongshu.main_chain",
        ],
        "blocked_or_manual": [
            "xiaohongshu.interactive_actions",
            "xiaohongshu.account_maintenance",
            "browser_account_switch_without_registry_admission",
        ],
        "side_effect_boundaries": {
            "readonly": list(READONLY_ALLOWED_SIDE_EFFECTS),
            "low_risk_network": list(LOW_RISK_NETWORK_SIDE_EFFECTS),
            "browser_operation_default": False,
            "account_operation_default": False,
            "writes_default": False,
        },
        "handoff_targets": ["Open Cloud", "OpenClaw native adapter"],
    }


def l2_multimodal_task_contract() -> Dict[str, Any]:
    return {
        "schema": "knowledgeradar-l2-multimodal-task-contract/v1",
        "status": "ready_for_design",
        "execution_mode": "contract_only",
        "default_auto_multimodal": False,
        "task_types": [
            "bilibili_video_l2_analysis",
            "youtube_video_l2_analysis",
            "xiaohongshu_image_ocr_or_multimodal_manual_only",
        ],
        "idempotency_key_fields": [
            "platform",
            "content_id",
            "source_url",
            "model_version",
            "strategy",
            "content_fingerprint",
        ],
        "cache_key_fields": [
            "platform",
            "content_id",
            "content_fingerprint",
            "model_version",
        ],
        "queue_contract": {
            "queue": "runtime.tasks",
            "checkpoint_ref": "l2:<platform>:<content_id>:<content_fingerprint>",
            "resume_from": "last_completed_stage",
            "dead_letter_queue": "degradation_dead_letters",
        },
        "retry_policy": {
            "max_attempts_default": 2,
            "retryable": ["MODEL_TIMEOUT", "NETWORK_ERROR", "TEMPORARY_PROVIDER_ERROR"],
            "non_retryable": ["CAPTCHA_REQUIRED", "ACCOUNT_RISK", "IP_OR_DEVICE_RISK", "UNSUPPORTED_MEDIA"],
        },
        "evidence_mapping": {
            "source_url": "original platform URL",
            "transcript": "subtitle or ASR text evidence",
            "ocr": "image/frame OCR evidence",
            "frames": "sampled frame references only; no raw image dump in compact output",
            "comments": "comment evidence when explicitly enabled",
            "model_output": "summary, claims, timestamps, confidence",
        },
        "side_effect_boundary": {
            "browser_operation_default": False,
            "account_operation_default": False,
            "network_or_model_request_requires_explicit_trigger": True,
            "writes": ["task_state", "cache", "evidence_record"],
        },
        "allowed_triggers": [
            "enable_deep_analysis=True",
            "explicit low-risk video L2 task",
            "routing_recommends_l2 with user-visible flag",
        ],
        "blocked_or_manual": [
            "xiaohongshu auto_multimodal without manual confirmation",
            "batch video L2 without budget",
            "raw frame dump in compact reports",
        ],
    }


def compact_patrol_contract() -> Dict[str, Any]:
    return {
        "schema": "knowledgeradar-compact-patrol-contract/v1",
        "status": "ready",
        "execution_mode": "summary_only_by_default",
        "steps": [
            "health_check(mode='summary')",
            "get_capabilities(summary=True)",
            "native_readonly_runner summary",
            "governed_capability_plan summary",
            "trace_evidence_ledger summary",
            "gh_cli_admission summary",
            "docs status index check",
        ],
        "optional_explicit_probes": [
            "health_check(mode='low_risk_execution_probe')",
        ],
        "forbidden_by_default": [
            "xiaohongshu browser launch without readonly route admission",
            "visible browser launch",
            "account switch without registry admission",
            "batch multimodal execution",
        ],
        "output_requirements": [
            "compact",
            "redacted",
            "include status labels: ok/degraded/blocked/manual/retired",
            "include evidence IDs when explicit probes run",
        ],
    }


def run_readonly_command(command: CapabilityCommand, fn: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    command_data = command.to_dict()
    trace_id = stable_hash({"command_id": command_data["command_id"], "started_at": utc_now_iso()}, length=16)
    span_id = stable_hash({"trace_id": trace_id, "capability_id": command_data.get("capability_id")}, length=12)
    started = time.time()
    policy = readonly_policy_decision(command_data)
    output: Dict[str, Any] = {}
    status = "ok"
    failure_taxonomy: Dict[str, Any] = {}
    if not policy.get("allowed"):
        status = "blocked"
        failure_taxonomy = {"code": "POLICY_DENIED", "reason": policy.get("reason")}
    else:
        try:
            output = fn() or {}
            if str(output.get("status") or "ok") in {"down", "error", "failed"}:
                status = "degraded"
                failure_taxonomy = {"code": str(output.get("status")), "reason": str(output.get("detail") or "")[:160]}
        except Exception as exc:  # pragma: no cover - defensive summary path
            status = "failed"
            output = {"status": "failed", "error": str(exc)[:240]}
            failure_taxonomy = {"code": type(exc).__name__, "reason": str(exc)[:240]}
    elapsed = round(time.time() - started, 3)
    output_hash = stable_hash(output, length=20)
    evidence = EvidenceRecord(
        trace_id=trace_id,
        command_id=command_data["command_id"],
        claim_supported=f"{command_data.get('capability_id')} returned {status}",
        failure_or_gap=failure_taxonomy.get("reason", ""),
    ).to_dict()
    envelope = ExecutionEnvelope(
        command_id=command_data["command_id"],
        trace_id=trace_id,
        span_id=span_id,
        status=status,
        policy_decision=policy,
        health_gate={"status": "ok", "reason": "readonly_summary"},
        budget=command_data.get("budget", {}),
        checkpoint_ref=f"readonly:{command_data.get('capability_id')}:{output_hash}",
        input_hash=command_data.get("input_hash", ""),
        output_hash=output_hash,
        evidence_ids=[evidence["evidence_id"]],
        degradation={} if status == "ok" else {"status": status, "failure": failure_taxonomy},
        failure_taxonomy=failure_taxonomy,
        retry_advice={"retryable": True, "reason": "low_risk_network_probe_can_retry"} if status in {"degraded", "failed"} else _default_retry_advice(status),
        side_effect_boundary={
            "allowed_side_effects": command_data.get("allowed_side_effects", []),
            "executed_side_effects": ["read_runtime_state"] if policy.get("allowed") else [],
            "browser_operation": False,
            "account_operation": False,
            "network_search": False,
        },
        next_actions=[] if status == "ok" else ["inspect_source_summary"],
        elapsed_s=elapsed,
        summary=_compact_value(output),
    ).to_dict()
    trace_events = build_trace_events(command_data, envelope, evidence)
    durable_state = build_durable_state(command_data, envelope, evidence)
    envelope["durable_state"] = durable_state
    envelope["trace_events"] = trace_events
    return {
        "command": command_data,
        "envelope": envelope,
        "evidence": evidence,
        "durable_state": durable_state,
        "trace_events": trace_events,
    }


def run_plan_only_command(command: CapabilityCommand) -> Dict[str, Any]:
    command_data = command.to_dict()
    trace_id = stable_hash({"command_id": command_data["command_id"], "planned_at": utc_now_iso()}, length=16)
    span_id = stable_hash({"trace_id": trace_id, "capability_id": command_data.get("capability_id")}, length=12)
    policy = plan_only_policy_decision(command_data)
    status = "planned" if policy.get("allowed") else "blocked"
    output = {
        "status": status,
        "planned": True,
        "capability_id": command_data.get("capability_id"),
        "execution": "not_executed",
    }
    output_hash = stable_hash(output, length=20)
    failure_taxonomy = {} if status == "planned" else {"code": "PLAN_ONLY_POLICY_DENIED", "reason": policy.get("reason")}
    evidence = EvidenceRecord(
        trace_id=trace_id,
        command_id=command_data["command_id"],
        claim_supported=f"{command_data.get('capability_id')} is {status} under plan-only policy",
        failure_or_gap=failure_taxonomy.get("reason", ""),
    ).to_dict()
    envelope = ExecutionEnvelope(
        command_id=command_data["command_id"],
        trace_id=trace_id,
        span_id=span_id,
        status=status,
        selected_strategy="plan_only",
        policy_decision=policy,
        health_gate={"status": "not_executed", "reason": "plan_only"},
        budget=command_data.get("budget", {}),
        checkpoint_ref=f"plan:{command_data.get('capability_id')}:{output_hash}",
        input_hash=command_data.get("input_hash", ""),
        output_hash=output_hash,
        evidence_ids=[evidence["evidence_id"]],
        degradation={} if status == "planned" else {"status": status, "failure": failure_taxonomy},
        failure_taxonomy=failure_taxonomy,
        retry_advice=_default_retry_advice(status),
        side_effect_boundary={
            "allowed_side_effects": command_data.get("allowed_side_effects", []),
            "executed_side_effects": [],
            "browser_operation": False,
            "account_operation": False,
            "network_search": False,
        },
        next_actions=["explicit_execution_required"] if status == "planned" else ["manual_review_required"],
        elapsed_s=0.0,
        summary=_compact_value(output),
    ).to_dict()
    trace_events = build_trace_events(command_data, envelope, evidence)
    durable_state = build_durable_state(command_data, envelope, evidence)
    envelope["durable_state"] = durable_state
    envelope["trace_events"] = trace_events
    return {
        "command": command_data,
        "envelope": envelope,
        "evidence": evidence,
        "durable_state": durable_state,
        "trace_events": trace_events,
    }


def run_low_risk_execution_command(command: CapabilityCommand, fn: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    """Execute a low-risk network command behind the native envelope contract."""
    command_data = command.to_dict()
    trace_id = stable_hash({"command_id": command_data["command_id"], "executed_at": utc_now_iso()}, length=16)
    span_id = stable_hash({"trace_id": trace_id, "capability_id": command_data.get("capability_id")}, length=12)
    started = time.time()
    denied = []
    if command_data.get("requires_browser"):
        denied.append("requires_browser")
    if command_data.get("requires_account"):
        denied.append("requires_account")
    if command_data.get("requires_user_consent"):
        denied.append("requires_user_consent")
    side_effects = set(command_data.get("allowed_side_effects") or [])
    if not side_effects.issubset(set(LOW_RISK_NETWORK_SIDE_EFFECTS)):
        denied.append("side_effect_not_low_risk_network")
    policy = {
        "schema": "knowledgeradar-policy-decision/v1",
        "allowed": not denied,
        "reason": "LOW_RISK_EXECUTION_ALLOWED" if not denied else "LOW_RISK_EXECUTION_DENIED",
        "denied": denied,
        "manual_confirm_required": bool(denied),
    }
    output: Dict[str, Any] = {}
    status = "ok"
    failure_taxonomy: Dict[str, Any] = {}
    executed_side_effects: List[str] = []
    if denied:
        status = "blocked"
        failure_taxonomy = {"code": "POLICY_DENIED", "reason": policy.get("reason")}
    else:
        try:
            executed_side_effects = ["read_runtime_state", "network_request"] if command_data.get("requires_network") else ["read_runtime_state"]
            output = fn() or {}
            output_status = str(output.get("status") or output.get("provider_status") or "ok")
            if output_status in {"down", "error", "failed"} or output.get("error"):
                status = "degraded"
                failure_taxonomy = {"code": output_status, "reason": str(output.get("error") or output.get("detail") or "")[:160]}
        except Exception as exc:  # pragma: no cover - defensive runtime path
            status = "failed"
            output = {"status": "failed", "error": str(exc)[:240]}
            failure_taxonomy = {"code": type(exc).__name__, "reason": str(exc)[:240]}
    elapsed = round(time.time() - started, 3)
    output_hash = stable_hash(output, length=20)
    evidence = EvidenceRecord(
        trace_id=trace_id,
        command_id=command_data["command_id"],
        source_type="low_risk_execution",
        collector=command_data.get("tool_name") or "native_runner",
        claim_supported=f"{command_data.get('capability_id')} explicit execution returned {status}",
        failure_or_gap=failure_taxonomy.get("reason", ""),
    ).to_dict()
    envelope = ExecutionEnvelope(
        command_id=command_data["command_id"],
        trace_id=trace_id,
        span_id=span_id,
        status=status,
        selected_strategy="low_risk_explicit_execution",
        policy_decision=policy,
        health_gate={"status": "ok" if not denied else "blocked", "reason": policy.get("reason")},
        budget=command_data.get("budget", {}),
        checkpoint_ref=f"execute:{command_data.get('capability_id')}:{output_hash}",
        input_hash=command_data.get("input_hash", ""),
        output_hash=output_hash,
        evidence_ids=[evidence["evidence_id"]],
        degradation={} if status == "ok" else {"status": status, "failure": failure_taxonomy},
        failure_taxonomy=failure_taxonomy,
        retry_advice=_default_retry_advice(status),
        side_effect_boundary={
            "allowed_side_effects": command_data.get("allowed_side_effects", []),
            "executed_side_effects": executed_side_effects,
            "browser_operation": False,
            "account_operation": False,
            "network_search": bool(command_data.get("requires_network")),
        },
        next_actions=[] if status == "ok" else ["inspect_low_risk_execution"],
        elapsed_s=elapsed,
        summary=_compact_value(output),
    ).to_dict()
    trace_events = build_trace_events(command_data, envelope, evidence)
    durable_state = build_durable_state(command_data, envelope, evidence)
    envelope["durable_state"] = durable_state
    envelope["trace_events"] = trace_events
    return {
        "command": command_data,
        "envelope": envelope,
        "evidence": evidence,
        "durable_state": durable_state,
        "trace_events": trace_events,
        "output_summary": _compact_value(output),
    }


def build_readonly_command(capability_id: str, tool_name: str, *, purpose: str = "observability") -> CapabilityCommand:
    return CapabilityCommand(
        capability_id=capability_id,
        tool_name=tool_name,
        purpose=purpose,
        risk_scope="runtime_readonly",
        input={"summary": True},
        budget={"profile": "compact", "max_output": "summary"},
        expected_evidence_schema="knowledgeradar-evidence-record/v1",
    )


def native_execution_schema_summary() -> Dict[str, Any]:
    return {
        "schema": "knowledgeradar-native-execution-schemas/v1",
        "public_mcp_tool_surface": "unchanged",
        "command_schema": "knowledgeradar-capability-command/v1",
        "envelope_schema": "knowledgeradar-execution-envelope/v1",
        "evidence_schema": "knowledgeradar-evidence-record/v1",
        "durable_state_schema": "knowledgeradar-durable-state/v1",
        "policy_schema": "knowledgeradar-policy-decision/v1",
        "readonly_allowed_side_effects": list(READONLY_ALLOWED_SIDE_EFFECTS),
        "default_boundaries": {
            "browser_operation": False,
            "account_operation": False,
            "network_search": False,
            "writes": False,
        },
    }


def _compact_command(command: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "command_id": command.get("command_id"),
        "capability_id": command.get("capability_id"),
        "tool_name": command.get("tool_name"),
        "purpose": command.get("purpose"),
        "risk_scope": command.get("risk_scope"),
        "idempotency_key": command.get("idempotency_key"),
    }


def _compact_envelope(envelope: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "command_id": envelope.get("command_id"),
        "trace_id": envelope.get("trace_id"),
        "span_id": envelope.get("span_id"),
        "status": envelope.get("status"),
        "checkpoint_ref": envelope.get("checkpoint_ref"),
        "evidence_ids": envelope.get("evidence_ids", []),
        "policy_decision": {
            "allowed": (envelope.get("policy_decision") or {}).get("allowed"),
            "reason": (envelope.get("policy_decision") or {}).get("reason"),
        },
        "retry_advice": envelope.get("retry_advice", {}),
        "durable_state": _compact_durable_state(envelope.get("durable_state", {})),
        "trace_event_count": len(envelope.get("trace_events", []) or []),
        "summary": envelope.get("summary", {}),
    }


def _compact_durable_state(durable_state: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "checkpoint_ref": durable_state.get("checkpoint_ref"),
        "resume_from": durable_state.get("resume_from"),
        "attempt_policy": durable_state.get("attempt_policy", {}),
        "human_pause_required": bool(durable_state.get("human_pause_required", False)),
        "approval_token": durable_state.get("approval_token", ""),
        "event_count": len(durable_state.get("event_history_minimal", []) or []),
    }


def _compact_evidence(evidence: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "evidence_id": evidence.get("evidence_id"),
        "trace_id": evidence.get("trace_id"),
        "command_id": evidence.get("command_id"),
        "claim_supported": evidence.get("claim_supported"),
        "evidence_strength": evidence.get("evidence_strength"),
        "verification_status": evidence.get("verification_status"),
    }


def _compact_trace_event(event: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "trace_id": event.get("trace_id"),
        "command_id": event.get("command_id"),
        "capability_id": event.get("capability_id"),
        "event": event.get("event"),
        "status": event.get("status"),
        "evidence_ids": event.get("evidence_ids", []),
        "failure_tag": event.get("failure_tag", ""),
    }


def trace_evidence_ledger_summary(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    trace_events: List[Dict[str, Any]] = []
    evidence: List[Dict[str, Any]] = []
    envelopes: List[Dict[str, Any]] = []
    capabilities: Dict[str, int] = {}
    failures: Dict[str, int] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        envelope = record.get("envelope") or record
        if isinstance(envelope, dict) and envelope.get("schema") == "knowledgeradar-execution-envelope/v1":
            envelopes.append(envelope)
            status = str(envelope.get("status") or "unknown")
            failures[status] = failures.get(status, 0) + 1
            for event in envelope.get("trace_events", []) or []:
                if isinstance(event, dict):
                    trace_events.append(event)
        item = record.get("evidence")
        if isinstance(item, dict):
            evidence.append(item)
        command = record.get("command") or {}
        capability_id = str(command.get("capability_id") or "")
        if capability_id:
            capabilities[capability_id] = capabilities.get(capability_id, 0) + 1
    return {
        "schema": "knowledgeradar-trace-evidence-ledger/v1",
        "status": "ok",
        "record_count": len(envelopes),
        "trace_event_count": len(trace_events),
        "evidence_count": len(evidence),
        "by_capability": capabilities,
        "by_status": failures,
        "sample_trace_events": [_compact_trace_event(event) for event in trace_events[:12]],
        "sample_evidence": [_compact_evidence(item) for item in evidence[:12]],
        "privacy": {
            "redacted": True,
            "no_cookie_or_token": True,
            "compact_only": True,
        },
    }


def compact_readonly_patrol(result: Dict[str, Any]) -> Dict[str, Any]:
    envelopes = result.get("envelopes", []) if isinstance(result.get("envelopes"), list) else []
    evidence = result.get("evidence", []) if isinstance(result.get("evidence"), list) else []
    return {
        "schema": result.get("schema", "knowledgeradar-native-readonly-patrol/v1"),
        "status": result.get("status", "unknown"),
        "execution_mode": result.get("execution_mode", "readonly"),
        "tool_surface": result.get("tool_surface", "unchanged"),
        "step_count": result.get("step_count", 0),
        "evidence_count": len(evidence),
        "schema_summary": native_execution_schema_summary(),
        "commands": [_compact_command(command) for command in result.get("commands", [])],
        "envelopes": [_compact_envelope(envelope) for envelope in envelopes],
        "evidence": [_compact_evidence(item) for item in evidence],
        "trace_evidence_links": [
            {
                "trace_id": envelope.get("trace_id"),
                "command_id": envelope.get("command_id"),
                "evidence_ids": envelope.get("evidence_ids", []),
            }
            for envelope in envelopes
        ],
        "durable_checkpoints": [
            _compact_durable_state(envelope.get("durable_state", {}))
            for envelope in envelopes
        ],
        "ledger": trace_evidence_ledger_summary([
            {"command": command, "envelope": envelope, "evidence": item}
            for command, envelope, item in zip(result.get("commands", []), envelopes, evidence)
        ]),
        "side_effect_boundary": result.get("side_effect_boundary", {}),
        "notes": result.get("notes", []),
    }


def run_readonly_patrol(steps: Iterable[Dict[str, Any]], *, compact: bool = True) -> Dict[str, Any]:
    results = []
    for step in steps:
        command = build_readonly_command(
            str(step.get("capability_id") or ""),
            str(step.get("tool_name") or ""),
            purpose=str(step.get("purpose") or "observability"),
        )
        results.append(run_readonly_command(command, step["fn"]))
    envelopes = [item["envelope"] for item in results]
    evidence = [item["evidence"] for item in results]
    statuses = [str(item.get("status") or "unknown") for item in envelopes]
    if any(status == "failed" for status in statuses):
        status = "failed"
    elif any(status in {"blocked", "degraded"} for status in statuses):
        status = "degraded"
    else:
        status = "ok"
    result = {
        "schema": "knowledgeradar-native-readonly-patrol/v1",
        "status": status,
        "execution_mode": "readonly",
        "tool_surface": "unchanged",
        "step_count": len(results),
        "commands": [item["command"] for item in results],
        "envelopes": envelopes,
        "evidence": evidence,
        "side_effect_boundary": {
            "browser_operation": False,
            "account_operation": False,
            "network_search": False,
            "writes": False,
            "allowed_side_effects": list(READONLY_ALLOWED_SIDE_EFFECTS),
        },
        "notes": [
            "internal runner only; no MCP tool signature change",
            "readonly patrol does not call search/detail/browser/account actions",
        ],
    }
    if compact:
        return compact_readonly_patrol(result)
    return result


def governed_command_plan(
    *,
    capability_id: str,
    tool_name: str,
    purpose: str,
    platform: str = "",
    requires_network: bool = False,
    requires_browser: bool = False,
    requires_account: bool = False,
    policy_context: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Return a plan-only envelope for a governed command without executing it."""
    policy_context = policy_context or {}
    allowed_side_effects = list(LOW_RISK_NETWORK_SIDE_EFFECTS if requires_network else READONLY_ALLOWED_SIDE_EFFECTS)
    command = CapabilityCommand(
        capability_id=capability_id,
        tool_name=tool_name,
        purpose=purpose,
        risk_scope=str(policy_context.get("risk_scope") or platform or "generic"),
        input={
            "platform": platform,
            "plan_only": True,
        },
        budget={"profile": "planned", "execution": "not_executed"},
        requires_network=requires_network,
        requires_browser=requires_browser,
        requires_account=requires_account,
        allowed_side_effects=allowed_side_effects,
        requires_user_consent=bool(policy_context.get("requires_user_consent", False)),
        policy_context=policy_context,
        expected_evidence_schema="knowledgeradar-evidence-record/v1",
    )
    return run_plan_only_command(command)


def governed_capability_plan_summary() -> Dict[str, Any]:
    """Plan common sensing commands without executing search/detail/browser actions."""
    plans = [
        governed_command_plan(
            capability_id="academic.search.plan",
            tool_name="search_academic",
            purpose="search",
            platform="academic",
            requires_network=True,
            policy_context={"risk_scope": "low_risk_network", "execution": "plan_only"},
        ),
        governed_command_plan(
            capability_id="web.search.plan",
            tool_name="kr_web_search",
            purpose="search",
            platform="web",
            requires_network=True,
            policy_context={"risk_scope": "low_risk_network", "execution": "plan_only"},
        ),
        governed_command_plan(
            capability_id="web.extract.plan",
            tool_name="extract_web_page",
            purpose="extract",
            platform="web",
            requires_network=True,
            policy_context={"risk_scope": "low_risk_network", "execution": "plan_only"},
        ),
        governed_command_plan(
            capability_id="content.detail.low_risk.plan",
            tool_name="get_content_detail",
            purpose="detail",
            platform="bilibili_zhihu_youtube",
            requires_network=True,
            policy_context={"risk_scope": "platform_low_risk", "execution": "plan_only"},
        ),
        governed_command_plan(
            capability_id="xiaohongshu.search.plan",
            tool_name="search_xiaohongshu",
            purpose="search",
            platform="xiaohongshu",
            requires_network=True,
            requires_browser=True,
            requires_account=True,
            policy_context={
                "risk_scope": "xiaohongshu_readonly_route",
                "execution": "plan_only_runtime_route_required",
                "requires_user_consent": False,
            },
        ),
        governed_command_plan(
            capability_id="xiaohongshu.detail.plan",
            tool_name="get_content_detail",
            purpose="detail",
            platform="xiaohongshu",
            requires_network=True,
            requires_browser=True,
            requires_account=True,
            policy_context={
                "risk_scope": "xiaohongshu_readonly_route",
                "execution": "plan_only_runtime_route_required",
                "requires_user_consent": False,
            },
        ),
    ]
    return {
        "schema": "knowledgeradar-governed-capability-plan/v1",
        "status": "ok",
        "execution_mode": "plan_only",
        "plans": [
            {
                "capability_id": plan["command"].get("capability_id"),
                "tool_name": plan["command"].get("tool_name"),
                "purpose": plan["command"].get("purpose"),
                "platform": plan["command"].get("input", {}).get("platform"),
                "status": plan["envelope"].get("status"),
                "policy_decision": plan["envelope"].get("policy_decision"),
                "side_effect_boundary": plan["envelope"].get("side_effect_boundary"),
                "durable_state": _compact_durable_state(plan["envelope"].get("durable_state", {})),
                "evidence_ids": plan["envelope"].get("evidence_ids", []),
            }
            for plan in plans
        ],
        "ledger": trace_evidence_ledger_summary(plans),
        "notes": [
            "plan only; no search/extract/detail execution",
            "low-risk network entries are plan-ready but not executed",
            "xiaohongshu readonly search/detail require runtime route admission before execution",
        ],
    }


def build_low_risk_execution_command(
    *,
    capability_id: str,
    tool_name: str,
    purpose: str,
    input_data: Dict[str, Any] | None = None,
    budget: Dict[str, Any] | None = None,
) -> CapabilityCommand:
    return CapabilityCommand(
        capability_id=capability_id,
        tool_name=tool_name,
        purpose=purpose,
        risk_scope="low_risk_network",
        input=input_data or {},
        budget=budget or {"profile": "compact", "execution": "explicit_low_risk"},
        requires_network=True,
        requires_browser=False,
        requires_account=False,
        allowed_side_effects=list(LOW_RISK_NETWORK_SIDE_EFFECTS),
        requires_user_consent=False,
        policy_context={"execution": "explicit_low_risk", "risk_scope": "low_risk_network"},
        expected_evidence_schema="knowledgeradar-evidence-record/v1",
    )


def low_risk_execution_summary(executions: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    records = [item for item in executions if isinstance(item, dict)]
    statuses = [str((item.get("envelope") or {}).get("status") or "unknown") for item in records]
    if any(status == "failed" for status in statuses):
        status = "failed"
    elif any(status in {"blocked", "degraded"} for status in statuses):
        status = "degraded"
    else:
        status = "ok"
    return {
        "schema": "knowledgeradar-low-risk-execution-summary/v1",
        "status": status,
        "execution_mode": "explicit_low_risk",
        "execution_count": len(records),
        "commands": [_compact_command(item.get("command") or {}) for item in records],
        "envelopes": [_compact_envelope(item.get("envelope") or {}) for item in records],
        "ledger": trace_evidence_ledger_summary(records),
        "side_effect_boundary": {
            "browser_operation": False,
            "account_operation": False,
            "network_search": True,
            "writes": False,
            "allowed_side_effects": list(LOW_RISK_NETWORK_SIDE_EFFECTS),
        },
        "notes": [
            "explicit execution is limited to low-risk network capabilities",
            "no browser, account, xiaohongshu, or write operation is executed",
        ],
    }


def xhs_policy_gate_matrix(switcher_summary: Dict[str, Any] | None = None) -> Dict[str, Any]:
    switcher_summary = switcher_summary or {}
    plans = switcher_summary.get("plans", {}) if isinstance(switcher_summary.get("plans"), dict) else {}
    purposes = ["diagnostic", "patrol", "search", "detail", "main_chain"]
    rows = []
    for purpose in purposes:
        plan = plans.get(purpose, {}) if isinstance(plans.get(purpose), dict) else {}
        rows.append(
            {
                "purpose": purpose,
                "execution_mode": plan.get("execution_mode", "plan_only"),
                "executable": bool(plan.get("executable", False)),
                "recommended_profile_id": plan.get("recommended_profile_id", ""),
                "risk_level": plan.get("risk_level"),
                "denial_reason": plan.get("denial_reason", ""),
                "policy": "safe_auto_plan_allowed" if bool(plan.get("executable", False)) else "manual_or_denied",
                "browser_operation": False,
                "account_operation": False,
            }
        )
    return {
        "schema": "knowledgeradar-xhs-policy-gate-matrix/v1",
        "status": "ok",
        "execution_mode": "plan_only",
        "rows": rows,
        "notes": [
            "safe_auto plans follow registry policy",
            "interactive maintenance flows remain denied or manual-confirm only",
            "matrix does not launch browsers or switch accounts",
        ],
    }
