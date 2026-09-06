"""Explicit, non-native continuity control plane for KnowledgeRadar.

This command never claims to refresh Codex Desktop and never registers an MCP
server. It records recovery receipts and creates a task handoff that a new
Codex turn or thread can inherit while the host MCP surface is unavailable.
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from runtime.mcp_continuity import (  # noqa: E402
    ACCESS_FALLBACK,
    record_fallback,
    record_fallback_call,
    record_native_call,
    snapshot,
    state_path,
)
from runtime.continuity_fallback import (  # noqa: E402
    FallbackContractError,
    READINESS_TOOLS,
    invoke_configured_tool,
    source_fingerprint,
)
from runtime.research_ledger import open_task, read_task, record_tool_receipt  # noqa: E402
def _json(value: object) -> None:
    # This entry point belongs to the public product package, where the
    # development-only console helper is intentionally absent.  Keep UTF-8
    # output self-contained so a handoff receipt is readable from PowerShell.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _load_arguments(args: argparse.Namespace) -> dict[str, object]:
    raw = str(args.arguments or "")
    if args.arguments_file:
        try:
            raw = Path(args.arguments_file).read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ValueError(f"arguments_file_unreadable:{exc}") from exc
    elif args.arguments_base64url:
        try:
            encoded = str(args.arguments_base64url)
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f"arguments_base64url_invalid:{exc}") from exc
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid_arguments_json:{exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("arguments_must_be_json_object")
    return dict(parsed)


def _cmd_status(_: argparse.Namespace) -> int:
    path = state_path()
    if not path.is_file():
        _json({"schema": "knowledgeradar-mcp-continuity/v1", "status": "unknown", "state_path": str(path)})
        return 0
    try:
        _json(json.loads(path.read_text(encoding="utf-8-sig")))
    except (OSError, json.JSONDecodeError) as exc:
        _json({"schema": "knowledgeradar-mcp-continuity/v1", "status": "invalid_state", "error": str(exc), "state_path": str(path)})
        return 1
    return 0


def _cmd_mark_native(args: argparse.Namespace) -> int:
    _json(record_native_call(tool=args.tool, source_fingerprint=args.source_fingerprint, tool_list_fingerprint=args.tool_list_fingerprint))
    return 0


def _cmd_activate_fallback(args: argparse.Namespace) -> int:
    if not args.reason.strip():
        _json({"schema": "knowledgeradar-mcp-continuity/v1", "status": "invalid_request", "error": "--reason is required"})
        return 2
    _json(record_fallback(reason=args.reason, task_id=args.task_id))
    return 0


def _cmd_call(args: argparse.Namespace) -> int:
    if not args.reason.strip():
        _json({"schema": "knowledgeradar-continuity-fallback/v1", "status": "invalid_request", "error": "--reason is required"})
        return 2
    try:
        arguments = _load_arguments(args)
    except ValueError as exc:
        _json({"schema": "knowledgeradar-continuity-fallback/v1", "status": "invalid_request", "error": str(exc)})
        return 2
    for item in args.argument:
        key, separator, raw_value = str(item).partition("=")
        if not separator or not key.strip():
            _json({"schema": "knowledgeradar-continuity-fallback/v1", "status": "invalid_request", "error": "--argument must use key=value"})
            return 2
        try:
            arguments[key.strip()] = json.loads(raw_value)
        except json.JSONDecodeError:
            arguments[key.strip()] = raw_value
    task_id = str(args.task_id or "").strip()
    tool = str(args.tool or "").strip()
    if tool not in READINESS_TOOLS and not task_id:
        _json({"schema": "knowledgeradar-continuity-fallback/v1", "status": "invalid_request", "error": "--task-id is required for non-readiness tool calls"})
        return 2
    if task_id and read_task(task_id=task_id).get("status") == "unknown_task":
        _json({"schema": "knowledgeradar-continuity-fallback/v1", "status": "unknown_task", "research_task_id": task_id})
        return 1

    fingerprint = source_fingerprint(ROOT)
    record_fallback(reason=args.reason, task_id=task_id)
    try:
        invocation = invoke_configured_tool(
            tool=tool,
            arguments=arguments,
            config_path=Path(args.config).resolve() if args.config else None,
            project_root=ROOT,
        )
    except (FallbackContractError, OSError, ValueError) as exc:
        continuity = record_fallback_call(tool=tool, outcome="failed", reason=args.reason, task_id=task_id, source_fingerprint=fingerprint)
        _json({
            "schema": "knowledgeradar-continuity-fallback/v1",
            "status": "fallback_unavailable",
            "access_path": ACCESS_FALLBACK,
            "native_mcp_claim": "not_claimed",
            "degraded_reason": args.reason,
            "research_task_id": task_id,
            "tool": tool,
            "error": str(exc),
            "source_fingerprint": fingerprint,
            "continuity": continuity,
        })
        return 1
    if invocation.get("mcp_call_status") != "ok":
        continuity = record_fallback_call(
            tool=tool,
            outcome="failed",
            reason=args.reason,
            task_id=task_id,
            source_fingerprint=fingerprint,
            tool_list_fingerprint=str(invocation.get("tool_list_fingerprint") or ""),
        )
        _json({
            "schema": "knowledgeradar-continuity-fallback/v1",
            "status": "tool_error",
            "access_path": ACCESS_FALLBACK,
            "native_mcp_claim": "not_claimed",
            "degraded_reason": args.reason,
            "research_task_id": task_id,
            "tool": tool,
            "source_fingerprint": fingerprint,
            "tool_list_fingerprint": invocation.get("tool_list_fingerprint"),
            "continuity": continuity,
            "result": invocation.get("result"),
        })
        return 1
    receipt = {}
    if task_id:
        receipt = record_tool_receipt(
            task_id=task_id,
            trace_id=f"continuity:{fingerprint}:{tool}",
            tool=tool,
            status="ok",
            source_ecology="continuity_fallback",
            association="explicit_task_scope",
        )
    continuity = record_fallback_call(
        tool=tool,
        outcome="ok",
        reason=args.reason,
        task_id=task_id,
        source_fingerprint=fingerprint,
        tool_list_fingerprint=str(invocation.get("tool_list_fingerprint") or ""),
    )
    _json({
        "schema": "knowledgeradar-continuity-fallback/v1",
        "status": "ok",
        "access_path": ACCESS_FALLBACK,
        "native_mcp_claim": "not_claimed",
        "degraded_reason": args.reason,
        "research_task_id": task_id,
        "tool": tool,
        "source_fingerprint": fingerprint,
        "tool_list_fingerprint": invocation.get("tool_list_fingerprint"),
        "tool_receipt": receipt.get("receipt", {}),
        "continuity": continuity,
        "result": invocation.get("result"),
    })
    return 0


def _cmd_start_task(args: argparse.Namespace) -> int:
    considered = []
    if args.considered:
        try:
            considered = json.loads(args.considered)
        except json.JSONDecodeError as exc:
            _json({"schema": "knowledgeradar-research-task-ledger/v2", "status": "invalid_request", "error": str(exc)})
            return 2
    result = open_task(objective=args.objective, budget=args.budget, considered=considered if isinstance(considered, list) else [], task_id=args.task_id)
    _json({"schema": "knowledgeradar-continuity-handoff/v1", "status": "task_ready", "access_path": ACCESS_FALLBACK, "research_task": result})
    return 0


def _cmd_resume_task(args: argparse.Namespace) -> int:
    result = read_task(task_id=args.task_id)
    status = str(result.get("status") or "unknown_task")
    _json({"schema": "knowledgeradar-continuity-handoff/v1", "status": "task_resumable" if status != "unknown_task" else status, "access_path": ACCESS_FALLBACK, "research_task": result})
    return 0 if status != "unknown_task" else 1


def _cmd_export_handoff(args: argparse.Namespace) -> int:
    task = read_task(task_id=args.task_id)
    if task.get("status") == "unknown_task":
        _json({"schema": "knowledgeradar-continuity-handoff/v1", "status": "unknown_task", "research_task_id": args.task_id})
        return 1
    payload = {
        "schema": "knowledgeradar-continuity-handoff/v1",
        "status": "ready_for_new_turn",
        "access_path": ACCESS_FALLBACK,
        "native_mcp_claim": "not_claimed",
        "continuity_state_path": str(state_path()),
        "continuity_state": snapshot(
            config_ok=True,
            service_ok=True,
            session_status="unobserved",
            tool_list_ok=False,
            fallback_active=True,
            fallback_reason="host_mcp_surface_unavailable",
        ),
        "research_task": task,
        "next_action": "在宿主 refresh 或新线程后，先真实调用 mcp__knowledgeradar.health_check，再继续该 research_task_id。",
    }
    if args.output:
        output = Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        payload["output"] = str(output)
    _json(payload)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KnowledgeRadar explicit non-native continuity receipts")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status")
    status.set_defaults(handler=_cmd_status)

    mark = sub.add_parser("mark-native-call")
    mark.add_argument("--tool", required=True)
    mark.add_argument("--source-fingerprint", default="")
    mark.add_argument("--tool-list-fingerprint", default="")
    mark.set_defaults(handler=_cmd_mark_native)

    fallback = sub.add_parser("activate-fallback")
    fallback.add_argument("--reason", required=True)
    fallback.add_argument("--task-id", default="")
    fallback.set_defaults(handler=_cmd_activate_fallback)

    call = sub.add_parser("call", help="explicit non-native L3 call to the configured KR stdio server")
    call.add_argument("--reason", required=True)
    call.add_argument("--tool", required=True)
    call.add_argument("--arguments", default="", help="JSON object passed to the configured MCP tool")
    group = call.add_mutually_exclusive_group()
    group.add_argument("--arguments-file", default="", help="UTF-8 JSON object file for complex arguments")
    group.add_argument("--arguments-base64url", default="", help="base64url UTF-8 JSON object for complex arguments")
    call.add_argument("--argument", action="append", default=[], help="PowerShell-safe key=value argument; repeat as needed")
    call.add_argument("--task-id", default="")
    call.add_argument("--config", default="", help="optional explicit Codex config path")
    call.set_defaults(handler=_cmd_call)

    start = sub.add_parser("start-task")
    start.add_argument("--objective", required=True)
    start.add_argument("--budget", default="balanced")
    start.add_argument("--task-id", default="")
    start.add_argument("--considered", default="[]", help="JSON array of source ecologies")
    start.set_defaults(handler=_cmd_start_task)

    resume = sub.add_parser("resume-task")
    resume.add_argument("--task-id", required=True)
    resume.set_defaults(handler=_cmd_resume_task)

    export = sub.add_parser("export-handoff")
    export.add_argument("--task-id", required=True)
    export.add_argument("--output", default="")
    export.set_defaults(handler=_cmd_export_handoff)

    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
