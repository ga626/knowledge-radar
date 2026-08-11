"""Direct OpenClaw handoff contracts for KnowledgeRadar.

This module does not call OpenClaw. It defines the contract OpenClaw can use to
start, resume and observe KnowledgeRadar work through the existing MCP surface.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Dict, List


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_hash(value: Any, *, length: int = 16) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:length]


@dataclass(frozen=True)
class OpenClawHandoffRequest:
    schema: str = "knowledgeradar-openclaw-handoff-request/v1"
    request_id: str = ""
    task_goal: str = ""
    scope: Dict[str, Any] = field(default_factory=dict)
    allowed_capabilities: List[str] = field(default_factory=list)
    forbidden_capabilities: List[str] = field(default_factory=list)
    budget: Dict[str, Any] = field(default_factory=dict)
    execution_mode: str = "plan_or_low_risk"
    human_approval_policy: Dict[str, Any] = field(default_factory=dict)
    evidence_output: Dict[str, Any] = field(default_factory=dict)
    resume_policy: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if not data["request_id"]:
            data["request_id"] = "OCR-" + stable_hash([data["task_goal"], data["scope"]], length=14)
        return data


@dataclass(frozen=True)
class OpenClawHandoffStatus:
    schema: str = "knowledgeradar-openclaw-handoff-status/v1"
    task_id: str = ""
    trace_id: str = ""
    checkpoint_ref: str = ""
    resume_from: str = ""
    status: str = "planned"
    policy_decision: Dict[str, Any] = field(default_factory=dict)
    evidence_pack_ref: str = ""
    blocked_or_manual_reason: str = ""
    next_actions: List[str] = field(default_factory=list)
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def openclaw_native_adapter_summary() -> Dict[str, Any]:
    request = OpenClawHandoffRequest(
        task_goal="<task goal>",
        scope={"topic": "<topic>", "time_range": "<optional>", "platforms": ["web", "academic"]},
        allowed_capabilities=[
            "health_check.summary",
            "get_capabilities.summary",
            "kr_web_search",
            "search_academic",
            "extract_web_page",
            "analyze_decision_logs.compact",
            "get_task_status.summary",
        ],
        forbidden_capabilities=[
            "search_xiaohongshu",
            "browser_launch",
            "account_switch",
            "auto_high_risk_detail",
        ],
        budget={"mcp_calls": "8-12", "output": "compact_evidence_register"},
        human_approval_policy={"required_for": ["browser", "account", "high_risk_platform"], "default": "deny"},
        evidence_output={"format": "EvidencePack + Evidence Register", "redacted": True},
        resume_policy={"checkpoint_required": True, "resume_from": "last_completed_step"},
    ).to_dict()
    status = OpenClawHandoffStatus(
        task_id="<task_id>",
        trace_id="<trace_id>",
        checkpoint_ref="<checkpoint_ref>",
        resume_from="last_completed_step",
        status="planned",
        policy_decision={"allowed": True, "reason": "contract_only"},
        evidence_pack_ref="<evidence_pack_ref>",
        next_actions=["call health_check(summary)", "call get_capabilities(summary)", "execute allowed low-risk tools"],
    ).to_dict()
    return {
        "schema": "knowledgeradar-openclaw-native-adapter/v1",
        "status": "design_ready",
        "open_cloud_required": False,
        "actual_mcp_tool_surface": "unchanged",
        "request_schema": request["schema"],
        "status_schema": status["schema"],
        "sample_request": request,
        "sample_status": status,
        "prompt_templates": [
            "standard_fact_check",
            "candidate_admission_review",
            "compact_patrol",
            "long_running_research",
            "multimodal_task_planning",
        ],
        "safety_rules": {
            "do_not_bypass_kr_policy_gate": True,
            "browser_operation_default": False,
            "account_operation_default": False,
            "read_compact_status_instead_of_raw_logs": True,
        },
    }
