"""Versioned, explicit L3 caller for a configured KnowledgeRadar stdio server.

This is intentionally *not* an MCP registration and never claims that it
refreshed a Codex Desktop thread. It creates a short-lived, independent stdio
client only after the caller has established that the host-native tool surface
is unavailable. The same configured server command and task ledger are used,
so platform adapters and research evidence are not duplicated.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import tomllib
from typing import Any, AsyncIterator
import uuid


SCHEMA = "knowledgeradar-continuity-fallback/v1"
READINESS_TOOLS = {"health_check", "get_capabilities"}
EXPECTED_TOOL_NAMES = frozenset(
    {
        "kr_research", "finalize_research_task", "analyze_decision_logs", "get_task_status",
        "kr_web_search", "search_github_repositories", "search_youtube", "search_wechat_articles",
        "search_academic", "extract_web_page", "extract_dynamic_page", "search_bilibili",
        "search_xiaohongshu", "search_zhihu", "search_recruitment", "get_capabilities", "health_check",
        "get_content_detail", "manage_xiaohongshu_accounts", "record_research_candidates_tool",
        "advance_research_candidate", "review_research_progress",
    }
)
MAX_READINESS_ATTEMPTS = 2


class FallbackContractError(ValueError):
    """The requested fallback call is outside the explicit L3 contract."""


def default_codex_config_path() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    return codex_home / "config.toml"


def _as_string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items() if isinstance(key, str)}


def _active_install_identity(cwd: Path) -> dict[str, str] | None:
    """Return the active-product identity only when ``cwd`` is that product.

    The product installation lives at ``<install>/app/<version>`` and its
    authoritative selector is ``<install>/active.json``.  This is deliberately
    stricter than accepting an arbitrary Python file, while still allowing a
    development checkout when it is the explicitly supplied project root.
    """

    install_root = cwd.parent.parent
    active_path = install_root / "active.json"
    try:
        active = json.loads(active_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(active, dict) or active.get("schema") != "knowledgeradar-active-install/v1":
        return None
    try:
        active_root = Path(str(active.get("program_root") or "")).expanduser().resolve()
    except OSError:
        return None
    if active_root != cwd or not (cwd / "src" / "server.py").is_file():
        return None
    return {
        "kind": "active_install",
        "active_path": str(active_path),
        "version": str(active.get("version") or ""),
        "program_root": str(active_root),
    }


def configured_stdio_server(*, config_path: Path | None = None, project_root: Path | None = None) -> dict[str, Any]:
    """Read and validate the one registered KR stdio server identity.

    A continuity client must use the same selected product that Codex would
    launch.  The historical source-root-only check made the fallback unusable
    after the product installer correctly moved Codex to an active artifact.
    """

    config = Path(config_path or default_codex_config_path()).expanduser().resolve()
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    try:
        raw = tomllib.loads(config.read_text(encoding="utf-8-sig"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise FallbackContractError(f"codex_config_unreadable:{exc}") from exc
    servers = raw.get("mcp_servers") if isinstance(raw, dict) else None
    entry = servers.get("knowledgeradar") if isinstance(servers, dict) else None
    if not isinstance(entry, dict) or entry.get("enabled") is False:
        raise FallbackContractError("knowledgeradar_stdio_registration_missing_or_disabled")
    command = str(entry.get("command") or "").strip()
    args = [str(item) for item in entry.get("args") or []]
    cwd = Path(str(entry.get("cwd") or "")).expanduser().resolve()
    if not command or not args or entry.get("url"):
        raise FallbackContractError("knowledgeradar_registration_is_not_stdio")
    expected_server = (root / "src" / "server.py").resolve()
    configured_servers = [Path(item).resolve() for item in args if item.lower().endswith(".py")]
    configured_server = cwd / "src" / "server.py"
    if configured_server.resolve() not in configured_servers:
        raise FallbackContractError("knowledgeradar_registration_does_not_target_cwd_server")
    if cwd == root and expected_server in configured_servers:
        identity = {"kind": "development_source", "program_root": str(root)}
    else:
        identity = _active_install_identity(cwd)
        if identity is None:
            raise FallbackContractError("knowledgeradar_registration_does_not_target_active_install")
    return {
        "config_path": str(config),
        "command": command,
        "args": args,
        "cwd": str(cwd),
        "env": _as_string_map(entry.get("env")),
        "identity": identity,
    }


def source_fingerprint(project_root: Path | None = None) -> str:
    root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
    digest = hashlib.sha256()
    for relative in ("src/server.py", "src/runtime/continuity_fallback.py"):
        path = root / relative
        digest.update(relative.encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"missing")
    return digest.hexdigest()[:16]


def _catalog_fingerprint(tools: list[str]) -> str:
    digest = hashlib.sha256("\n".join(sorted(tools)).encode("utf-8")).hexdigest()[:20]
    return f"sha256:{digest}"


def _validate_tool_catalog(tools: list[str]) -> None:
    """Reject a partial or unexpected server before any tool is invoked."""

    actual = frozenset(tools)
    if actual != EXPECTED_TOOL_NAMES:
        missing = ",".join(sorted(EXPECTED_TOOL_NAMES - actual))
        unexpected = ",".join(sorted(actual - EXPECTED_TOOL_NAMES))
        raise FallbackContractError(
            f"configured_server_tool_catalog_mismatch:expected={len(EXPECTED_TOOL_NAMES)}:actual={len(actual)}:"
            f"missing={missing}:unexpected={unexpected}"
        )


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "dict"):
        return value.dict()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


@asynccontextmanager
async def _configured_session(server: dict[str, Any]) -> AsyncIterator[Any]:
    try:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
    except ImportError as exc:  # pragma: no cover - deployment dependency boundary
        raise FallbackContractError(f"mcp_client_unavailable:{exc}") from exc
    env = {
        **os.environ,
        **dict(server.get("env") or {}),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "KR_CONTINUITY_FALLBACK": "1",
        "KR_CONTINUITY_INVOCATION_ID": uuid.uuid4().hex,
    }
    parameters = StdioServerParameters(
        command=str(server["command"]),
        args=[str(item) for item in server["args"]],
        env=env,
        cwd=str(server["cwd"]),
        encoding="utf-8",
        encoding_error_handler="replace",
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def _invoke(server: dict[str, Any], tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async with _configured_session(server) as session:
        listing = await session.list_tools()
        tools = sorted({str(item.name) for item in getattr(listing, "tools", [])})
        _validate_tool_catalog(tools)
        if tool not in tools:
            raise FallbackContractError(f"configured_server_does_not_expose_tool:{tool}")
        result = await session.call_tool(tool, arguments=arguments)
    payload = _jsonable(result)
    mcp_is_error = bool(getattr(result, "isError", False))
    if isinstance(payload, dict):
        mcp_is_error = mcp_is_error or bool(payload.get("isError") or payload.get("is_error"))
    return {
        "result": payload,
        "tools": tools,
        "tool_list_fingerprint": _catalog_fingerprint(tools),
        "tool_count": len(tools),
        "mcp_call_status": "error" if mcp_is_error else "ok",
    }


def _invoke_sync(server: dict[str, Any], tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        attempts = MAX_READINESS_ATTEMPTS if tool in READINESS_TOOLS else 1
        failures: list[str] = []
        for attempt in range(1, attempts + 1):
            try:
                result = asyncio.run(_invoke(server, tool, arguments))
                return {**result, "attempt_count": attempt, "retry_failures": failures}
            except (OSError, asyncio.TimeoutError) as exc:
                failures.append(f"attempt_{attempt}:{type(exc).__name__}:{exc}")
                if attempt == attempts:
                    raise FallbackContractError(f"fallback_readiness_retry_exhausted:{'|'.join(failures)}") from exc
    raise FallbackContractError("fallback_call_cannot_run_inside_existing_event_loop")


def invoke_configured_tool(
    *,
    tool: str,
    arguments: dict[str, Any],
    config_path: Path | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Call one configured KR tool in an independent L3 session.

    There is no hidden generic-web route. If this configured MCP server cannot
    execute a tool, the explicit caller receives a failure receipt instead of a
    fake native-recovery claim.
    """

    if not str(tool or "").strip():
        raise FallbackContractError("tool_required")
    if not isinstance(arguments, dict):
        raise FallbackContractError("arguments_must_be_json_object")
    server = configured_stdio_server(config_path=config_path, project_root=project_root)
    target_fingerprint = source_fingerprint(Path(str(server["cwd"])))
    return {
        **_invoke_sync(server, str(tool), arguments),
        "server": {
            "cwd": server["cwd"],
            "config_path": server["config_path"],
            "identity": server["identity"],
            "source_fingerprint": target_fingerprint,
        },
        "source_fingerprint": target_fingerprint,
    }
