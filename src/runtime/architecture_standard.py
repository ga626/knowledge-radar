"""Architecture standard summaries for KnowledgeRadar runtime governance.

This module is intentionally side-effect free. It exposes the architecture
contracts that higher-level agents can read through existing capabilities and
health summaries without adding MCP tools.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List


ARCHITECTURE_BLOCKS = [
    "capability_registry",
    "route_policy",
    "runtime_contract",
    "task_durable_runtime",
    "trace_evidence_ledger",
    "external_candidate_admission",
    "knowledge_asset_interface",
    "openclaw_native_adapter",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def architecture_standard_summary(*, tool_surface: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Return KR Architecture Standard v1 as a compact machine-readable summary."""
    tool_surface = tool_surface or {}
    return {
        "schema": "knowledgeradar-architecture-standard/v1",
        "status": "ok",
        "generated_at": _now_iso(),
        "actual_mcp_tool_count": tool_surface.get("actual_mcp_tool_count"),
        "tool_surface_source": tool_surface.get("source_of_truth", "src/server.py @mcp.tool() registrations"),
        "excluded_tracks": ["A4.jd_research_pipeline", "A8.self_evolution", "A9.multi_agent_service_mode"],
        "blocks": [
            {
                "id": "capability_registry",
                "purpose": "Register tools, virtual capabilities, platforms, risk and execution modes.",
                "code_sources": ["src/capabilities.py", "src/server.py", "src/runtime/native_runner.py"],
                "primary_schemas": ["knowledgeradar-tool-surface/v2", "knowledgeradar-capabilities/v1"],
                "status": "active",
            },
            {
                "id": "route_policy",
                "purpose": "Choose the lowest-risk collection route before adding platform-specific collectors.",
                "code_sources": ["capabilities.platform_onboarding_policy", "capabilities.route_policy_matrix"],
                "primary_schemas": ["knowledgeradar-platform-onboarding/v1", "knowledgeradar-route-policy/v1"],
                "status": "active",
            },
            {
                "id": "runtime_contract",
                "purpose": "Normalize command, envelope, evidence, policy and durable-state contracts.",
                "code_sources": ["src/runtime/native_runner.py"],
                "primary_schemas": [
                    "knowledgeradar-capability-command/v1",
                    "knowledgeradar-execution-envelope/v1",
                    "knowledgeradar-evidence-record/v1",
                    "knowledgeradar-durable-state/v1",
                    "knowledgeradar-trace-event/v1",
                ],
                "status": "active",
            },
            {
                "id": "task_durable_runtime",
                "purpose": "Reuse existing task queue, checkpoint, retry and dead-letter primitives globally.",
                "code_sources": ["src/runtime/tasks.py", "src/runtime/degradation.py", "src/runtime/native_runner.py"],
                "primary_schemas": [
                    "knowledgeradar-runtime-task/v1",
                    "knowledgeradar-durable-state/v1",
                    "knowledgeradar-dead-letter-record/v1",
                ],
                "status": "active",
            },
            {
                "id": "trace_evidence_ledger",
                "purpose": "Join tool trace, decision log, task status, usage and evidence records under compact ledger semantics.",
                "code_sources": [
                    "src/runtime/tool_trace.py",
                    "src/runtime/tasks.py",
                    "src/runtime/usage_tracker.py",
                    "src/runtime/degradation.py",
                    "src/kr_core/evidence_store.py",
                ],
                "primary_schemas": ["knowledgeradar-trace-evidence-ledger/v1"],
                "status": "active",
            },
            {
                "id": "external_candidate_admission",
                "purpose": "Gate third-party APIs, CLIs, sidecars and browser candidates before main-chain use.",
                "code_sources": ["src/capabilities.py", "src/runtime/profile_registry.py", "src/runtime/channel_admission.py"],
                "primary_schemas": ["knowledgeradar-candidate-admission/v1"],
                "status": "active",
            },
            {
                "id": "knowledge_asset_interface",
                "purpose": "Expose sensing outputs as EvidencePack, ClaimRecord and SourceRecord without owning the future personal knowledge base.",
                "code_sources": ["src/runtime/knowledge_assets.py", "src/kr_core/evidence_store.py"],
                "primary_schemas": [
                    "knowledgeradar-evidence-pack/v1",
                    "knowledgeradar-claim-record/v1",
                    "knowledgeradar-source-record/v1",
                ],
                "status": "active",
            },
            {
                "id": "openclaw_native_adapter",
                "purpose": "Define direct OpenClaw handoff/status contracts; Open Cloud is optional, not required.",
                "code_sources": ["src/runtime/openclaw_native_adapter.py"],
                "primary_schemas": [
                    "knowledgeradar-openclaw-handoff-request/v1",
                    "knowledgeradar-openclaw-handoff-status/v1",
                ],
                "status": "design_ready",
            },
        ],
        "global_rules": {
            "do_not_expand_actual_mcp_tools_by_default": True,
            "reuse_existing_task_runtime": True,
            "browser_operation_default": False,
            "account_operation_default": False,
            "xiaohongshu_search_detail_main_chain": "readonly_route_admission_required",
            "open_cloud_required_for_l7": False,
        },
    }


def architecture_completion_summary(checks: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Summarize whether architecture blocks are represented by runtime summaries."""
    rows: List[Dict[str, Any]] = [row for row in checks if isinstance(row, dict)]
    by_id = {str(row.get("id") or ""): row for row in rows}
    missing = [block for block in ARCHITECTURE_BLOCKS if block not in by_id]
    return {
        "schema": "knowledgeradar-architecture-completion/v1",
        "status": "ok" if not missing else "degraded",
        "block_count": len(ARCHITECTURE_BLOCKS),
        "represented_count": len(ARCHITECTURE_BLOCKS) - len(missing),
        "missing_blocks": missing,
        "blocks": rows,
    }
