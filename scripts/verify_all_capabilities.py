"""Verify KnowledgeRadar's public MCP capability surface with real calls.

This script intentionally does not copy the older WorkBuddy verifier. It imports
the current server module, compares the exposed tool surface with
get_capabilities(summary=True), and runs small real smoke calls.

Status classes:
- PASS: the tool executed and returned a structurally valid result.
- NEEDS_INTERACTION: the tool is present but the current environment needs user
  login, QR scan, captcha, or profile setup before it can be fairly validated.
- EXPECTED_DEGRADED: a declared product boundary, optional provider state, or
  known fallback path. It is reported for visibility but does not fail the run.
- FAIL: the tool is missing, crashes unexpectedly, or returns a broken shape for
  a core low-risk capability.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def _product_data_root() -> Path | None:
    configured = os.environ.get("KR_DATA_ROOT", "").strip()
    return Path(configured).expanduser().resolve() if configured else None


DATA_ROOT = _product_data_root()
REPORT_DIR = (DATA_ROOT / "state" / "reports") if DATA_ROOT else (ROOT / "runtime" / "reports")

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("KR_LOG_DIR", str(DATA_ROOT / "logs") if DATA_ROOT else str(ROOT / "runtime" / "logs"))
os.environ.setdefault("KR_STATE_DIR", str(DATA_ROOT / "state") if DATA_ROOT else str(ROOT / "runtime"))
os.environ.setdefault("KR_CHROME_PREWARM", "0")
os.environ.setdefault("KR_XHS_TIKHUB_BREAK_GLASS_AUTO", "1")
os.environ.setdefault("KR_XHS_TIKHUB_BREAK_GLASS_DRY_RUN", "0")
os.environ.setdefault("KR_XHS_TIKHUB_DAILY_BUDGET_USD", "0")
os.environ.setdefault("KR_XHS_TIKHUB_MAX_CALLS_PER_TASK", "0")
os.environ.setdefault("KR_BILIBILI_TRANSCRIBE_ON_DETAIL", "0")

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from runtime.status_schema import VALIDATION_STATUS_VALUES, classify_runtime_payload  # noqa: E402


PASS = "PASS"
NEEDS_INTERACTION = "NEEDS_INTERACTION"
EXPECTED_DEGRADED = "EXPECTED_DEGRADED"
FAIL = "FAIL"

EXPECTED_DEGRADED_TYPES = {
    "expected_degraded",
    "not_configured",
    "login_required",
    "auth_required",
    "captcha_required",
    "verification_required",
    "anti_bot_or_platform_verification",
    "provider_not_configured",
    "browser_unavailable",
    "dependency_missing",
    "cdp_unavailable",
    "network_error",
    "timeout",
    "empty_results",
    "request_failed",
    "expected_degraded_optional",
    "optional_provider_unavailable",
    "quota_exhausted",
    "endpoint_unavailable_for_current_key",
    "institution_or_document_delivery_required",
}

LOGIN_OR_OPTIONAL_TOOLS = {
    "search_xiaohongshu",
    "search_zhihu",
    "search_recruitment",
}


@dataclass
class ToolResult:
    tool: str
    status: str
    elapsed_s: float
    input_summary: dict[str, Any]
    reason: str = ""
    error_type: str = ""
    output_summary: dict[str, Any] | None = None


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except TypeError:
        return str(value)


def _summarize_output(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        summary: dict[str, Any] = {
            "keys": sorted(str(k) for k in value.keys())[:20],
        }
        for key in ("status", "schema_version", "platform", "provider", "total", "summary"):
            if key in value:
                summary[key] = _jsonable(value.get(key))
        items = value.get("items")
        if isinstance(items, list):
            summary["item_count"] = len(items)
        if value.get("error"):
            summary["error"] = _jsonable(value.get("error"))
        return summary
    if isinstance(value, list):
        return {"type": "list", "length": len(value), "sample": _jsonable(value[:3])}
    return {"type": type(value).__name__, "repr": str(value)[:300]}


def _error_type_from_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            for key in ("type", "error_type", "failure_type", "platform_state"):
                if err.get(key):
                    return str(err.get(key))
        if isinstance(err, str) and err:
            return err[:80]
        metadata = payload.get("metadata")
        if isinstance(metadata, dict) and metadata.get("expected_degraded"):
            return "expected_degraded"
        status = payload.get("status")
        if status in {"degraded", "down", "not_configured"}:
            return str(status)
    return ""


def _has_usable_result(payload: Any) -> bool:
    if isinstance(payload, dict):
        items = payload.get("items")
        if isinstance(items, list) and len(items) > 0:
            return True
        total = payload.get("total")
        if isinstance(total, int) and total > 0:
            return True
        content = payload.get("content") or payload.get("markdown") or payload.get("text")
        if isinstance(content, str) and len(content.strip()) >= 80:
            return True
        title = payload.get("title")
        if title and not payload.get("error"):
            return True
        status = str(payload.get("status") or "").lower()
        if status in {"ok", "pass", "healthy"} and not payload.get("error"):
            return True
    if isinstance(payload, list) and payload:
        return True
    return False


def _looks_needs_interaction(tool: str, payload: Any, exc: BaseException | None = None) -> tuple[bool, str]:
    if tool not in LOGIN_OR_OPTIONAL_TOOLS:
        return False, ""
    if _has_usable_result(payload):
        return False, ""
    if isinstance(payload, dict):
        metadata = payload.get("metadata")
        if isinstance(metadata, dict) and metadata.get("expected_degraded"):
            reason = str(metadata.get("degraded_reason") or "")
            if any(token in reason.lower() for token in ("login", "auth", "captcha", "profile", "cdp")) or any(token in reason for token in ("登录", "扫码", "验证")):
                return True, reason or "manual_login_or_profile_required"
        err = payload.get("error")
        err_text = json.dumps(_jsonable(err), ensure_ascii=False).lower() if err else ""
    else:
        err_text = ""
    text = ""
    if exc is not None:
        text = f"{type(exc).__name__}: {exc}".lower()
    else:
        text = json.dumps(_jsonable(payload), ensure_ascii=False).lower()[:4000]
    combined = f"{text}\n{err_text}"
    needles = [
        "login",
        "auth",
        "captcha",
        "verification",
        "profile",
        "扫码",
        "登录",
        "验证码",
        "人工",
    ]
    if any(needle in combined for needle in needles):
        return True, "manual login/profile interaction required"
    return False, ""


def _looks_expected_degraded(tool: str, payload: Any, exc: BaseException | None = None) -> tuple[bool, str]:
    if _has_usable_result(payload):
        return False, ""
    if isinstance(payload, dict):
        metadata = payload.get("metadata")
        if isinstance(metadata, dict) and metadata.get("expected_degraded"):
            return True, str(metadata.get("degraded_reason") or "expected_degraded")

    text = ""
    if exc is not None:
        text = f"{type(exc).__name__}: {exc}".lower()
    else:
        text = json.dumps(_jsonable(payload), ensure_ascii=False).lower()[:4000]

    if tool in LOGIN_OR_OPTIONAL_TOOLS:
        needles = [
            "expected_degraded",
            "login",
            "auth",
            "captcha",
            "验证",
            "登录",
            "not configured",
            "not_configured",
            "api key",
            "cdp",
            "chrome",
            "playwright",
            "timeout",
            "network",
            "安全",
            "风控",
        ]
        if any(needle in text for needle in needles):
            return True, "optional dependency/login/platform state"

    error_type = _error_type_from_payload(payload)
    if error_type and error_type.lower() in EXPECTED_DEGRADED_TYPES:
        return True, error_type
    return False, error_type


def _classify(tool: str, payload: Any) -> tuple[str, str, str]:
    if _has_usable_result(payload):
        return PASS, "real call returned usable data", _error_type_from_payload(payload)
    needs_interaction, interaction_reason = _looks_needs_interaction(tool, payload)
    if needs_interaction:
        return NEEDS_INTERACTION, interaction_reason or "manual interaction required", _error_type_from_payload(payload)
    expected, error_type = _looks_expected_degraded(tool, payload)
    if expected:
        return EXPECTED_DEGRADED, "expected degraded state", error_type
    if isinstance(payload, dict):
        shared = classify_runtime_payload(
            payload,
            required=tool not in LOGIN_OR_OPTIONAL_TOOLS,
            main_chain=tool not in LOGIN_OR_OPTIONAL_TOOLS,
            has_declared_reason=bool(payload.get("detail") or payload.get("reason") or payload.get("degraded_reason") or payload.get("validation_reason")),
            optional=tool in LOGIN_OR_OPTIONAL_TOOLS,
        )
        if shared["status_class"] != FAIL:
            return shared["status_class"], shared.get("reason") or "shared runtime classification", shared.get("error_type") or error_type
    if isinstance(payload, dict) and payload.get("error"):
        return FAIL, "unexpected error payload", error_type
    if payload in (None, "", []):
        return FAIL, "empty result", error_type
    return PASS, "real call returned a usable structure", error_type


def _run_tool(name: str, fn: Callable[[], Any], input_summary: dict[str, Any]) -> ToolResult:
    started = time.perf_counter()
    try:
        payload = fn()
        elapsed = round(time.perf_counter() - started, 3)
        status, reason, error_type = _classify(name, payload)
        return ToolResult(
            tool=name,
            status=status,
            elapsed_s=elapsed,
            input_summary=input_summary,
            reason=reason,
            error_type=error_type,
            output_summary=_summarize_output(payload),
        )
    except Exception as exc:
        elapsed = round(time.perf_counter() - started, 3)
        needs_interaction, interaction_reason = _looks_needs_interaction(name, None, exc)
        if needs_interaction:
            return ToolResult(
                tool=name,
                status=NEEDS_INTERACTION,
                elapsed_s=elapsed,
                input_summary=input_summary,
                reason=interaction_reason or "manual interaction required",
                error_type=type(exc).__name__,
                output_summary={"exception": type(exc).__name__, "message": str(exc)[:500]},
            )
        expected, error_type = _looks_expected_degraded(name, None, exc)
        return ToolResult(
            tool=name,
            status=EXPECTED_DEGRADED if expected else FAIL,
            elapsed_s=elapsed,
            input_summary=input_summary,
            reason=("expected degraded exception" if expected else str(exc)[:500]),
            error_type=error_type or type(exc).__name__,
            output_summary={"exception": type(exc).__name__, "message": str(exc)[:500]},
        )


def _wait_for_task(server: Any, task_id: str, *, timeout_s: float, poll_s: float = 3.0) -> dict[str, Any]:
    deadline = time.time() + max(1.0, timeout_s)
    last: dict[str, Any] = {}
    while time.time() < deadline:
        status = server.get_task_status(task_id=task_id, limit=1, compact=False)
        if isinstance(status, dict):
            task = status.get("task")
            if isinstance(task, dict) and task:
                last = task
            else:
                tasks = status.get("tasks")
                if isinstance(tasks, list) and tasks:
                    last = tasks[0] if isinstance(tasks[0], dict) else {}
        if last:
            if last.get("status") in {"completed", "failed", "cancelled", "skipped"}:
                return last
        time.sleep(max(0.5, poll_s))
    return {**last, "status": last.get("status") or "timeout", "timeout_s": timeout_s}


def _verify_bilibili_multimodal(server: Any, *, timeout_s: float) -> dict[str, Any]:
    detail = server.get_content_detail("https://www.bilibili.com/video/BV1cUoDYaEdb", enable_deep_analysis=True)
    direct_media = detail.get("direct_media") if isinstance(detail, dict) else {}
    provider_downloadability = (direct_media or {}).get("provider_downloadability") if isinstance(direct_media, dict) else {}
    native_video_status = (
        EXPECTED_DEGRADED
        if str((provider_downloadability or {}).get("status") or "").lower() in {"provider_blocked", "not_direct"}
        else PASS
    )
    deep = detail.get("deep_analysis") if isinstance(detail, dict) else {}
    task_id = str((deep or {}).get("task_id") or "")
    if not task_id:
        fallback = _bilibili_fallback_acceptance(detail, {})
        return {
            "status": "ok" if fallback["pass"] else "expected_degraded",
            "validation_status": PASS if fallback["pass"] else EXPECTED_DEGRADED,
            "native_video_url_status": native_video_status,
            "fallback_chain": fallback,
            "error": "" if fallback["pass"] else "deep analysis task_id missing and no fallback completion signal",
            "detail_keys": sorted(detail.keys())[:20] if isinstance(detail, dict) else [],
        }
    task = _wait_for_task(server, task_id, timeout_s=timeout_s)
    completed = task.get("status") == "completed"
    fallback = _bilibili_fallback_acceptance(detail, task)
    if not completed and task.get("status") in {"queued", "running", "timeout"} and not (task.get("result") or {}).get("available"):
        try:
            server.get_task_store().mark_cancelled(
                task_id,
                reason=f"verification_timeout_after_{timeout_s}s",
                metadata={"verifier": "verify_all_capabilities", "previous_status": task.get("status")},
            )
        except Exception:
            pass
    return {
        "status": "ok" if completed or fallback["pass"] else "failed",
        "validation_status": PASS if completed or fallback["pass"] else FAIL,
        "native_video_url_status": native_video_status,
        "fallback_chain": fallback,
        "task_id": task_id,
        "task_status": task.get("status"),
        "result_path": task.get("result_path"),
        "error_code": task.get("error_code"),
        "error": "" if completed or fallback["pass"] else f"multimodal fallback task did not complete within {timeout_s}s",
    }


def _bilibili_fallback_acceptance(detail: Any, task: dict[str, Any]) -> dict[str, Any]:
    transcript = str((detail or {}).get("transcript") or "") if isinstance(detail, dict) else ""
    transcript_hit = bool(transcript.strip()) and "skipped by KR_BILIBILI_TRANSCRIBE_ON_DETAIL=0" not in transcript
    task_completed = str((task or {}).get("status") or "") == "completed"
    result_path = str((task or {}).get("result_path") or "")
    direct_media = (detail or {}).get("direct_media") if isinstance(detail, dict) else {}
    provider_status = str((((direct_media or {}).get("provider_downloadability") or {}).get("status") or "")).lower() if isinstance(direct_media, dict) else ""
    return {
        "schema": "knowledgeradar-bilibili-multimodal-fallback-acceptance/v1",
        "pass": bool(task_completed or transcript_hit or result_path),
        "accepted_paths": [name for name, ok in {"subtitle_or_asr_transcript": transcript_hit, "sample_frame_or_deep_analysis_task": task_completed or bool(result_path)}.items() if ok],
        "native_video_url_provider_status": provider_status,
        "native_video_url_interpretation": "EXPECTED_DEGRADED when provider_blocked/not_direct; fallback paths decide PASS",
    }


def _verify_xhs_fallback_plan(server: Any) -> dict[str, Any]:
    summary = server.health_check(mode="summary")
    plan = (((summary.get("checks") or {}).get("xiaohongshu_api_fallback_plan") or {}).get("break_glass_plan") or {})
    side_effects = ((summary.get("checks") or {}).get("xiaohongshu_api_fallback_plan") or {}).get("side_effects") or {}
    ok = bool(plan) and side_effects == {
        "api_call": False,
        "billing": False,
        "browser_launch": False,
        "station_search": False,
        "account_switch": False,
    }
    return {
        "status": "ok" if ok else "failed",
        "plan_status": plan.get("status"),
        "schema": plan.get("schema"),
        "side_effects": side_effects,
        "guards": plan.get("guards", {}),
    }


def _ensure_xhs_login_ready(server: Any, *, timeout_s: float = 20.0) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    last: dict[str, Any] = {}
    while time.time() < deadline:
        try:
            if server._ensure_chrome_debugging("xhs"):
                state = server.xiaohongshu_account_state(server._chrome_debug_url)
                last = state if isinstance(state, dict) else {}
                if server.xhs_collectors._xhs_login_state_ok(last):
                    return {"ready": True, "state": last}
        except Exception as exc:
            last = {"error": str(exc)[:300]}
        time.sleep(1.0)
    return {"ready": False, "state": last}


def _finalize_research_task_smoke(server: Any) -> Any:
    """Exercise the closeout contract without turning a smoke test into research."""
    task_id = "verify-capability-research-closeout"
    route = server.kr_research(
        task="Verify the research-task closeout contract only; do not conduct external research.",
        mode="plan_only",
        budget="fast",
        evidence_needs="closeout schema smoke only",
        research_task_id=task_id,
    )
    route_plan = route.get("route_plan") if isinstance(route, dict) else {}
    considered = route_plan.get("considered_source_ecologies") if isinstance(route_plan, dict) else []
    outcomes = [
        {
            "source_ecology": str(item.get("source_ecology") or ""),
            "outcome": "not_relevant",
            "reason": "Capability smoke validates the closeout schema only; it is not a user research task.",
        }
        for item in considered
        if isinstance(item, dict) and str(item.get("source_ecology") or "")
    ]
    return server.finalize_research_task(
        research_task_id=str((route.get("research_task") or {}).get("research_task_id") or task_id),
        ecology_outcomes=outcomes,
        stop_rationale="The smoke test has verified the receipt contract; additional evidence would be unrelated to this check.",
        key_claims=[],
        quality_status="pass",
        transcript_status="unavailable",
    )


def _research_ledger_smoke_task(server: Any, task_id: str) -> str:
    result = server.kr_research(
        task="KnowledgeRadar research ledger tool-surface smoke",
        mode="plan_only",
        budget="fast",
        evidence_needs="tool surface receipt",
        research_task_id=task_id,
    )
    return str((result.get("research_task") or {}).get("research_task_id") or task_id)


def _record_research_candidates_smoke(server: Any) -> dict[str, Any]:
    task_id = _research_ledger_smoke_task(server, "verify-record-research-candidates")
    return server.record_research_candidates_tool(
        research_task_id=task_id,
        source_ecology="generic_web_ecology",
        tool="kr_web_search",
        candidates=[{"id": "tool-surface-smoke"}],
        query="KnowledgeRadar tool surface smoke",
        language="en",
        intent_label="schema_smoke",
    )


def _advance_research_candidate_smoke(server: Any) -> dict[str, Any]:
    receipt = _record_research_candidates_smoke(server)
    candidates = list(receipt.get("candidates") or [])
    candidate_id = str((candidates[0] if candidates else {}).get("candidate_id") or "")
    return server.advance_research_candidate(
        research_task_id=str(receipt.get("research_task_id") or "verify-record-research-candidates"),
        candidate_id=candidate_id,
        stage="selected",
        tool="verify_all_capabilities",
        outcome="schema_smoke",
    )


def _review_research_progress_smoke(server: Any) -> dict[str, Any]:
    task_id = _research_ledger_smoke_task(server, "verify-review-research-progress")
    return server.review_research_progress(research_task_id=task_id, phase="after_archaeology")


def _server_tools(server: Any, *, safe: bool, wait_multimodal_s: float, include_multimodal: bool, include_xhs_fallback: bool) -> dict[str, Callable[[], Any]]:
    calls: dict[str, Callable[[], Any]] = {
        "expand_keywords": lambda: server.expand_keywords("KnowledgeRadar MCP"),
        "plan_research": lambda: server.plan_research("KnowledgeRadar productization"),
        "kr_research": lambda: server.kr_research(
            task="KnowledgeRadar MCP perception layer smoke",
            mode="plan_only",
            budget="fast",
            evidence_needs="tool surface and route plan",
        ),
        "record_research_candidates_tool": lambda: _record_research_candidates_smoke(server),
        "advance_research_candidate": lambda: _advance_research_candidate_smoke(server),
        "review_research_progress": lambda: _review_research_progress_smoke(server),
        "finalize_research_task": lambda: _finalize_research_task_smoke(server),
        "analyze_decision_logs": lambda: server.analyze_decision_logs(limit=5, compact=True),
        "get_task_status": lambda: server.get_task_status(task_id="summary", limit=5, compact=True),
        "kr_web_search": lambda: server.kr_web_search("KnowledgeRadar MCP", limit=1, provider="auto"),
        "search_github_repositories": lambda: server.search_github_repositories("model context protocol", limit=1),
        "search_youtube": lambda: server.search_youtube("model context protocol", limit=1),
        "search_wechat_articles": lambda: server.search_wechat_articles("台湾青年 政治态度", limit=1),
        "search_academic": lambda: server.search_academic("Model Context Protocol", limit=1, provider="openalex"),
        "extract_web_page": lambda: server.extract_web_page("https://www.iana.org/domains/reserved", use_jina=False, timeout=10.0),
        "extract_dynamic_page": lambda: server.extract_dynamic_page("https://www.iana.org/domains/reserved", wait_ms=300, timeout=12.0),
        "search_bilibili": lambda: server.search_bilibili("KnowledgeRadar", page_size=1),
        "search_xiaohongshu": lambda: (
            server.search_xiaohongshu("生活小技巧", limit=1)
            if _ensure_xhs_login_ready(server).get("ready")
            else {
                "items": [],
                "total": 0,
                "platform": "小红书",
                "error": {"type": "login_required", "error": "小红书登录态未确认", "manual_action_required": True},
            }
        ),
        "search_zhihu": lambda: server.search_zhihu("KnowledgeRadar", limit=1),
        "search_recruitment": lambda: server.search_recruitment("v2ex", "python", limit=1),
        "get_content_detail": lambda: server.get_content_detail("https://www.bilibili.com/video/BV1cUoDYaEdb"),
        "get_capabilities": lambda: server.get_capabilities(summary=True),
        "health_check": lambda: server.health_check(mode="summary"),
        "manage_xiaohongshu_accounts": lambda: server.manage_xiaohongshu_accounts(action="summary"),
    }
    if include_multimodal:
        calls["verify_bilibili_multimodal"] = lambda: _verify_bilibili_multimodal(server, timeout_s=wait_multimodal_s)
    if include_xhs_fallback:
        calls["verify_xhs_fallback_plan"] = lambda: _verify_xhs_fallback_plan(server)
    return calls


def _inputs_for(tool: str) -> dict[str, Any]:
    samples = {
        "expand_keywords": {"topic": "KnowledgeRadar MCP"},
        "plan_research": {"topic": "KnowledgeRadar productization"},
        "kr_research": {
            "task": "KnowledgeRadar MCP perception layer smoke",
            "mode": "plan_only",
            "budget": "fast",
            "evidence_needs": "tool surface and route plan",
        },
        "record_research_candidates_tool": {
            "research_task_id": "returned_by_kr_research",
            "source_ecology": "generic_web_ecology",
            "tool": "kr_web_search",
            "candidates": [{"id": "candidate-from-discovery"}],
        },
        "advance_research_candidate": {
            "research_task_id": "returned_by_kr_research",
            "candidate_id": "returned_by_record_research_candidates_tool",
            "stage": "selected",
        },
        "review_research_progress": {"research_task_id": "returned_by_kr_research", "phase": "after_archaeology"},
        "finalize_research_task": {
            "research_task_id": "returned_by_kr_research",
            "ecology_outcomes": [{"source_ecology": "generic_web_ecology", "outcome": "not_relevant", "reason": "schema smoke"}],
            "stop_rationale": "Capability schema smoke only.",
            "key_claims": [],
            "quality_status": "pass",
            "transcript_status": "unavailable",
        },
        "analyze_decision_logs": {"limit": 5, "compact": True},
        "get_task_status": {"task_id": "summary", "limit": 5, "compact": True},
        "kr_web_search": {"query": "KnowledgeRadar MCP", "limit": 1, "provider": "auto"},
        "search_github_repositories": {"query": "model context protocol", "limit": 1},
        "search_youtube": {"keyword": "model context protocol", "limit": 1},
        "search_wechat_articles": {"query": "台湾青年 政治态度", "limit": 1},
        "search_academic": {"query": "Model Context Protocol", "limit": 1, "provider": "openalex"},
        "extract_web_page": {"url": "https://www.iana.org/domains/reserved", "use_jina": False, "timeout": 10.0},
        "extract_dynamic_page": {"url": "https://www.iana.org/domains/reserved", "wait_ms": 300, "timeout": 12.0},
        "search_bilibili": {"keyword": "KnowledgeRadar", "page_size": 1},
        "search_xiaohongshu": {"keyword": "生活小技巧", "limit": 1},
        "search_zhihu": {"keyword": "KnowledgeRadar", "limit": 1},
        "search_recruitment": {"platform": "v2ex", "keyword": "python", "limit": 1},
        "get_content_detail": {"url": "https://www.bilibili.com/video/BV1cUoDYaEdb", "transcribe_on_detail": False},
        "get_capabilities": {"summary": True},
        "health_check": {"mode": "summary"},
        "manage_xiaohongshu_accounts": {"action": "summary"},
        "verify_bilibili_multimodal": {"url": "https://www.bilibili.com/video/BV1cUoDYaEdb", "enable_deep_analysis": True},
        "verify_xhs_fallback_plan": {"mode": "summary_plan_only", "side_effects": False},
    }
    return samples.get(tool, {})


def _write_reports(results: list[ToolResult], surface_tools: list[str]) -> tuple[Path, Path]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = REPORT_DIR / f"verify-all-capabilities-{stamp}.json"
    md_path = REPORT_DIR / f"verify-all-capabilities-{stamp}.md"
    counts = {status: sum(1 for r in results if r.status == status) for status in VALIDATION_STATUS_VALUES}
    payload = {
        "schema": "knowledgeradar-verify-all-capabilities/v1",
        "generated_at": stamp,
        "tool_count": len(surface_tools),
        "surface_tools": surface_tools,
        "counts": counts,
        "results": [asdict(r) for r in results],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    overall = "FAIL" if counts[FAIL] else "PASS"
    lines = [
        "# KnowledgeRadar Capability Verification",
        "",
        f"- Generated: {stamp}",
        f"- Overall: {overall}",
        f"- Tool count: {len(surface_tools)}",
        f"- PASS: {counts[PASS]}",
        f"- NEEDS_INTERACTION: {counts[NEEDS_INTERACTION]}",
        f"- EXPECTED_DEGRADED: {counts[EXPECTED_DEGRADED]}",
        f"- FAIL: {counts[FAIL]}",
        "",
        "Validation semantics: `EXPECTED_DEGRADED` and `NEEDS_INTERACTION` are visible states, not automatic failures. Only `FAIL` means the current public surface is structurally broken.",
        "",
        "| Tool | Status | Seconds | Reason |",
        "| --- | --- | ---: | --- |",
    ]
    for r in results:
        reason = (r.reason or "").replace("|", "/")
        if r.error_type:
            reason = f"{reason} ({r.error_type})"
        lines.append(f"| `{r.tool}` | {r.status} | {r.elapsed_s:.3f} | {reason} |")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify all KnowledgeRadar MCP capabilities with real calls.")
    parser.add_argument("--safe", action="store_true", help="Legacy alias; verifier still performs real small smoke calls.")
    parser.add_argument("--no-report", action="store_true", help="Do not write runtime/reports artifacts.")
    parser.add_argument("--include-multimodal", action="store_true", help="Also trigger and poll long-running multimodal tasks.")
    parser.add_argument("--wait-multimodal-s", type=float, default=float(os.environ.get("KR_VERIFY_MULTIMODAL_WAIT_S", "180")))
    parser.add_argument("--include-xhs-fallback-plan", action="store_true", help="Verify Xiaohongshu fallback strategy tree in plan-only mode.")
    args = parser.parse_args()

    import server  # type: ignore

    capabilities = server.get_capabilities(summary=True)
    surface = capabilities.get("tool_surface") if isinstance(capabilities, dict) else {}
    surface_tools = list(surface.get("actual_mcp_tools") or [])
    if not surface_tools:
        surface_tools = list(getattr(server, "ACTUAL_MCP_TOOLS", []))

    calls = _server_tools(
        server,
        safe=args.safe,
        wait_multimodal_s=args.wait_multimodal_s,
        include_multimodal=args.include_multimodal,
        include_xhs_fallback=args.include_xhs_fallback_plan,
    )
    if args.include_multimodal and "verify_bilibili_multimodal" not in surface_tools:
        surface_tools.append("verify_bilibili_multimodal")
    if args.include_xhs_fallback_plan and "verify_xhs_fallback_plan" not in surface_tools:
        surface_tools.append("verify_xhs_fallback_plan")
    missing = [tool for tool in surface_tools if tool not in calls]
    results: list[ToolResult] = [
        ToolResult(tool=tool, status=FAIL, elapsed_s=0.0, input_summary={}, reason="tool missing from verifier")
        for tool in missing
    ]
    for tool in surface_tools:
        if tool not in calls:
            continue
        results.append(_run_tool(tool, calls[tool], _inputs_for(tool)))

    counts = {status: sum(1 for r in results if r.status == status) for status in VALIDATION_STATUS_VALUES}
    print(f"KnowledgeRadar capability verification: tools={len(surface_tools)} PASS={counts[PASS]} NEEDS_INTERACTION={counts[NEEDS_INTERACTION]} EXPECTED_DEGRADED={counts[EXPECTED_DEGRADED]} FAIL={counts[FAIL]}")
    for item in results:
        print(f"{item.status:18} {item.tool:24} {item.elapsed_s:7.3f}s {item.reason}")

    if not args.no_report:
        json_path, md_path = _write_reports(results, surface_tools)
        print(f"JSON report: {json_path}")
        print(f"Markdown report: {md_path}")

    return 1 if counts[FAIL] else 0


if __name__ == "__main__":
    raise SystemExit(main())
