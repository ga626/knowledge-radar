"""Generate local MCP config snippets for KnowledgeRadar."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


DEFAULT_ROOT = Path(__file__).resolve().parents[1]


def configure_console_encoding() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    configure_console_encoding()
    parser = argparse.ArgumentParser(description="Generate KnowledgeRadar MCP config snippets.")
    parser.add_argument(
        "--agent",
        choices=["openclaw", "workbuddy", "codex", "codex-stdio", "claude", "cursor", "stdio"],
        default="openclaw",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default="18765")
    parser.add_argument("--python", default="", help="Python executable for stdio configs. Defaults to bundled .python312 or python.")
    parser.add_argument("--root", default="", help="Project root. Defaults to the checkout containing this script.")
    parser.add_argument("--state-dir", default="", help="Runtime state directory. Defaults to <root>/runtime.")
    parser.add_argument("--log-dir", default="", help="Runtime log directory. Defaults to <state-dir>/logs.")
    parser.add_argument("--profile-registry", default="", help="Optional local profile registry path.")
    args = parser.parse_args()

    root = Path(args.root).resolve() if args.root else DEFAULT_ROOT
    state_dir = Path(args.state_dir).resolve() if args.state_dir else root / "runtime"
    log_dir = Path(args.log_dir).resolve() if args.log_dir else state_dir / "logs"
    endpoint = f"http://{args.host}:{args.port}/mcp"
    bundled_python = root / ".python312" / "python.exe"
    python_exe = args.python or (str(bundled_python) if bundled_python.exists() else "python")
    stdio_server = str(root / "src" / "server.py")
    stdio_env = {
        "PYTHONPATH": str(root / "src"),
        "KR_MCP_TRANSPORT": "stdio",
        "KR_PROJECT_ROOT": str(root),
        "KR_SOURCE_ROOT": str(root / "src"),
        "KR_LOG_DIR": str(log_dir),
        "KR_STATE_DIR": str(state_dir),
    }
    if args.profile_registry:
        stdio_env["KR_PROFILE_REGISTRY_PATH"] = str(Path(args.profile_registry).resolve())

    if args.agent == "openclaw":
        server_name = "全网知识搜索"
        payload = {
            server_name: {
                "transport": "streamable-http",
                "url": endpoint,
                "description": "KnowledgeRadar perception/search/evidence MCP for web, platform, academic, media, and source-discovery research.",
            }
        }
    elif args.agent in {"codex", "codex-stdio", "workbuddy", "stdio", "claude", "cursor"}:
        server_name = "knowledgeradar" if args.agent in {"codex", "codex-stdio", "claude", "cursor"} else "全网知识搜索"
        selected_server = stdio_server
        selected_env = {**stdio_env}
        payload = {
            server_name: {
                "type": "stdio",
                "command": python_exe,
                "args": ["-X", "utf8", selected_server],
                "cwd": str(root),
                "env": selected_env,
            }
        }
    else:
        raise ValueError(f"Unsupported agent: {args.agent}")

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
