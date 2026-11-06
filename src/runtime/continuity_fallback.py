"""Versioned, explicit L3 caller for a configured KnowledgeRadar stdio server.

This is intentionally *not* an MCP registration and never claims that it
refreshed a Codex Desktop thread. It creates a short-lived, independent stdio
client only after the caller has established that the host-native tool surface
is unavailable. The same configured server command and task ledger are used,
so platform adapters and research evidence are not duplicated.
"""

from __future__ import annotations

import asyncio
import ast
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
REQUIRED_TOOL_NAMES = frozenset({
    "health_check", "get_capabilities", "kr_research", "get_task_status",
    "get_content_detail", "finalize_research_task",
})
RETIRED_TOOL_NAMES = frozenset({"expand_keywords", "plan_research"})
MAX_READINESS_ATTEMPTS = 2


class FallbackContractError(ValueError):
    """The requested fallback call is outside the explicit L3 contract."""

    def __init__(self, message: str, *, stage: str = "configuration", retryable: bool = False):
        super().__init__(message)
        self.code = message.split(":", 1)[0]
        self.stage = stage
        self.retryable = retryable
        self.attempt_count = 1


def _failure(exc: Exception, stage: str) -> FallbackContractError:
    """Unwrap task-group errors without publishing arbitrary exception text."""
    leaves = []

    def collect(error):
        if isinstance(error, BaseExceptionGroup):
            for child in error.exceptions:
                collect(child)
        else:
            leaves.append(error)

    collect(exc)
    contractual = next((item for item in leaves if isinstance(item, FallbackContractError)), None)
    if contractual is not None:
        return contractual
    transport = bool(leaves) and all(isinstance(item, (OSError, TimeoutError)) for item in leaves)
    return FallbackContractError(
        "fallback_transport_failed" if transport else "fallback_protocol_failed",
        stage=stage, retryable=transport and stage in {"connection", "initialization"})


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
        raise FallbackContractError("codex_config_unreadable") from exc
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
        "project_root": str(root),
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


def _validate_tool_catalog(tools: list[str], expected_tools: list[str]) -> None:
    """Reject a partial or unexpected server before any tool is invoked."""

    actual = frozenset(tools)
    expected = frozenset(expected_tools)
    if len(tools) != len(actual):
        raise FallbackContractError("configured_server_duplicate_tools", stage="catalog")
    if actual != expected:
        missing = ",".join(sorted(expected - actual))
        unexpected = ",".join(sorted(actual - expected))
        raise FallbackContractError(
            f"configured_server_tool_catalog_mismatch:expected={len(expected)}:actual={len(actual)}:"
            f"missing={missing}:unexpected={unexpected}", stage="catalog"
        )


def _declared_tool_catalog(server: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    """Use the selected artifact's contract, or its explicit legacy decorators."""
    root = Path(server["cwd"])
    path = root / "config/codex-product/tool-contracts.json"
    kind = "artifact_contract"
    try:
        if path.exists():
            raw = path.read_bytes()
            contract = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(contract, dict) or contract.get("schema") != "kr-tool-catalog/v1":
                raise ValueError("invalid_contract")
            names = [item["name"] for item in contract["tools"]]
            if contract.get("tool_count") != len(names):
                raise ValueError("invalid_count")
        else:
            # Pre-contract installations register top-level functions through
            # this exact decorator. Never import a second server in this client.
            kind = "legacy_source_registration"
            path = root / "src/server.py"
            raw = path.read_bytes()
            tree = ast.parse(raw.decode("utf-8-sig"))
            names = [
                node.name for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and any(isinstance(d, ast.Call) and isinstance(d.func, ast.Name)
                        and d.func.id == "nonblocking_mcp_tool" and len(d.args) == 1
                        and isinstance(d.args[0], ast.Name) and d.args[0].id == "mcp"
                        and not d.keywords for d in node.decorator_list)
            ]
        if (not names or any(not isinstance(n, str) or not n.isidentifier() for n in names)
                or len(set(names)) != len(names) or not REQUIRED_TOOL_NAMES <= set(names)
                or RETIRED_TOOL_NAMES.intersection(names)):
            raise ValueError("invalid_names")
    except (OSError, ValueError, KeyError, TypeError, SyntaxError) as exc:
        raise FallbackContractError("configured_server_tool_contract_invalid", stage="catalog") from exc
    return names, {"kind": kind, "path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def _revalidate_selection(server: dict[str, Any], catalog_identity: dict[str, str]) -> None:
    selected = configured_stdio_server(config_path=Path(server["config_path"]), project_root=Path(server["project_root"]))
    if any(selected.get(k) != server.get(k) for k in ("command", "args", "cwd", "env", "identity")):
        raise FallbackContractError("configured_server_selection_changed", stage="identity")
    try:
        unchanged = hashlib.sha256(Path(catalog_identity["path"]).read_bytes()).hexdigest() == catalog_identity["sha256"]
    except OSError:
        unchanged = False
    if not unchanged:
        raise FallbackContractError("configured_server_tool_contract_changed", stage="identity")


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


def _business_failed(payload: dict[str, Any]) -> bool:
    body = payload.get("structuredContent")
    if not isinstance(body, dict):
        blocks = payload.get("content") or []
        if len(blocks) == 1 and isinstance(blocks[0], dict) and blocks[0].get("type") == "text":
            try:
                body = json.loads(blocks[0].get("text") or "")
            except (ValueError, TypeError):
                body = None
    if not isinstance(body, dict):
        return False
    if "status" not in body and isinstance(body.get("result"), dict):
        body = body["result"]
    return body.get("ok") is False or (body.get("status") in {"error", "failed"} and bool(body.get("error")))


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
            try:
                await session.initialize()
            except Exception as exc:
                raise _failure(exc, "initialization") from exc
            yield session


async def _invoke(server: dict[str, Any], tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    expected, catalog_identity = _declared_tool_catalog(server)
    stage = "connection"
    try:
        async with _configured_session(server) as session:
            stage = "catalog"
            listing = await session.list_tools()
            tools = sorted(str(item.name) for item in getattr(listing, "tools", []))
            _validate_tool_catalog(tools, expected)
            if tool not in tools:
                raise FallbackContractError("configured_server_does_not_expose_tool", stage=stage)
            _revalidate_selection(server, catalog_identity)
            stage = "call"
            result = await session.call_tool(tool, arguments=arguments)
            stage = "cleanup"
    except Exception as exc:
        raise _failure(exc, stage) from exc
    payload = _jsonable(result)
    mcp_is_error = bool(getattr(result, "isError", False))
    if isinstance(payload, dict):
        mcp_is_error = mcp_is_error or bool(payload.get("isError") or payload.get("is_error")) or _business_failed(payload)
    return {
        "result": payload,
        "tools": tools,
        "tool_list_fingerprint": _catalog_fingerprint(tools),
        "tool_count": len(tools),
        "mcp_call_status": "error" if mcp_is_error else "ok",
        "catalog_identity": catalog_identity,
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
            except Exception as exc:
                failure = _failure(exc, "connection")
                failure.attempt_count = attempt
                failures.append(f"attempt_{attempt}:{failure.stage}:{failure.code}")
                if not failure.retryable or attempt == attempts:
                    if failure.retryable and tool in READINESS_TOOLS:
                        failure = FallbackContractError("fallback_readiness_retry_exhausted", stage=failure.stage)
                        failure.attempt_count = attempt
                    raise failure from exc
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
