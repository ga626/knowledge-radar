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
import os
from pathlib import Path
import tomllib
from typing import Any, AsyncIterator
import uuid


SCHEMA = "knowledgeradar-continuity-fallback/v1"
READINESS_TOOLS = {"health_check", "get_capabilities"}


class FallbackContractError(ValueError):
    """The requested fallback call is outside the explicit L3 contract."""


def default_codex_config_path() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    return codex_home / "config.toml"


def _as_string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items() if isinstance(key, str)}


def configured_stdio_server(*, config_path: Path | None = None, project_root: Path | None = None) -> dict[str, Any]:
    """Read and strictly validate the one registered KR stdio server."""

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
    if cwd != root or expected_server not in configured_servers:
        raise FallbackContractError("knowledgeradar_registration_does_not_target_current_project_source")
    return {
        "config_path": str(config),
        "command": command,
        "args": args,
        "cwd": str(cwd),
        "env": _as_string_map(entry.get("env")),
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
        if tool not in tools:
            raise FallbackContractError(f"configured_server_does_not_expose_tool:{tool}")
        result = await session.call_tool(tool, arguments=arguments)
    tool_list_fingerprint = hashlib.sha256("\n".join(tools).encode("utf-8")).hexdigest()[:20]
    payload = _jsonable(result)
    mcp_is_error = bool(getattr(result, "isError", False))
    if isinstance(payload, dict):
        mcp_is_error = mcp_is_error or bool(payload.get("isError") or payload.get("is_error"))
    return {
        "result": payload,
        "tools": tools,
        "tool_list_fingerprint": f"sha256:{tool_list_fingerprint}",
        "mcp_call_status": "error" if mcp_is_error else "ok",
    }


def _invoke_sync(server: dict[str, Any], tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_invoke(server, tool, arguments))
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
    return {
        **_invoke_sync(server, str(tool), arguments),
        "server": {"cwd": server["cwd"], "config_path": server["config_path"]},
    }
