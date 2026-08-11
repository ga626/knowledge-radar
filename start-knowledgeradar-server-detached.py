"""Detached launcher for the KnowledgeRadar MCP server on Windows."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from urllib.parse import urlparse


KR_ROOT = os.environ.get("KR_ROOT", "").strip() or os.path.dirname(os.path.abspath(__file__))
PYTHON_EXE = os.path.join(KR_ROOT, ".python312", "python.exe")
SERVER_SCRIPT = os.path.join(KR_ROOT, "src", "server.py")
BRIDGE_SCRIPT = os.path.join(KR_ROOT, "bridge", "xhs_mcp_bridge.cjs")
STATE_DIR = (
    os.environ.get("KR_STATE_DIR", "").strip()
    or os.path.join(KR_ROOT, "runtime")
)
LOG_DIR = os.environ.get("KR_LOG_DIR", "").strip() or os.path.join(STATE_DIR, "logs")
MCP_URL = os.environ.get("KR_MCP_URL", "").strip() or "http://127.0.0.1:18765/mcp"
MCP_PORT = int(os.environ.get("KR_MCP_PORT", "").strip() or (urlparse(MCP_URL).port or 18765))


def main() -> int:
    stdout_log = os.path.join(LOG_DIR, "knowledgeradar-mcp-server.stdout.log")
    stderr_log = os.path.join(LOG_DIR, "knowledgeradar-mcp-server.stderr.log")

    os.makedirs(LOG_DIR, exist_ok=True)

    preflight = _preflight()
    if preflight:
        for message in preflight:
            print(message, file=sys.stderr)
        return 1

    port_status = _resolve_port_conflict()
    if port_status == "blocked":
        return 1
    if port_status == "current":
        print("already-running")
        print(f"endpoint={MCP_URL}")
        return 0

    if _server_already_running():
        print("already-running")
        print(f"endpoint={MCP_URL}")
        return 0

    env = dict(os.environ)
    env.update(
        {
            "KR_STATE_DIR": STATE_DIR,
            "OPENCLAW_HOME": STATE_DIR,
            "OPENCLAW_STATE_DIR": STATE_DIR,
            "KR_LOG_DIR": LOG_DIR,
            "KR_CHROME_PREWARM": "0",
            "KR_CHROME_IDLE_SECONDS": os.environ.get("KR_CHROME_IDLE_SECONDS", "30"),
            "KR_CHROME_CLOSE_AFTER_OPERATION": os.environ.get("KR_CHROME_CLOSE_AFTER_OPERATION", "1"),
        }
    )

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW

    with open(stdout_log, "ab", buffering=0) as stdout, open(stderr_log, "ab", buffering=0) as stderr:
        proc = subprocess.Popen(
            [PYTHON_EXE, "-X", "utf8", "src\\server.py"],
            cwd=KR_ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            close_fds=True,
            creationflags=creationflags,
        )

    time.sleep(1.5)
    if proc.poll() is not None:
        print(f"KnowledgeRadar MCP server exited during startup: code={proc.returncode}", file=sys.stderr)
        print(f"stdout={stdout_log}", file=sys.stderr)
        print(f"stderr={stderr_log}", file=sys.stderr)
        return proc.returncode or 1

    print(proc.pid)
    print(f"endpoint={MCP_URL}")
    print(f"stdout={stdout_log}")
    print(f"stderr={stderr_log}")
    return 0


def _preflight() -> list[str]:
    errors: list[str] = []
    required_files = [
        ("KnowledgeRadar root", KR_ROOT),
        ("Python runtime", PYTHON_EXE),
        ("MCP server", SERVER_SCRIPT),
        ("XHS bridge", BRIDGE_SCRIPT),
    ]
    for label, path in required_files:
        if not os.path.exists(path):
            errors.append(f"{label} not found: {path}")
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        probe = os.path.join(LOG_DIR, ".knowledgeradar-launcher-write-test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("")
        os.remove(probe)
    except Exception as exc:
        errors.append(f"Runtime log dir is not writable: {LOG_DIR}: {exc}")
    return errors


def _server_already_running() -> bool:
    return any(_is_current_server(row.get("commandline", "")) for row in _port_listeners(MCP_PORT))


def _resolve_port_conflict() -> str:
    listeners = _port_listeners(MCP_PORT)
    if not listeners:
        return "free"

    current = [row for row in listeners if _is_current_server(row.get("commandline", ""))]
    if current:
        return "current"

    old_kr = [row for row in listeners if _is_knowledgeradar_server(row.get("commandline", ""))]
    if old_kr and _truthy_env("KR_PORT_CONFLICT_AUTO_CLEAN", "0"):
        for row in old_kr:
            pid = row.get("pid")
            print(f"port-conflict: stopping old KnowledgeRadar server pid={pid}", file=sys.stderr)
            _stop_process(pid)
        deadline = time.time() + 8
        while time.time() < deadline:
            remaining = _port_listeners(MCP_PORT)
            if not remaining:
                return "free"
            if any(_is_current_server(row.get("commandline", "")) for row in remaining):
                return "current"
            time.sleep(0.5)

    print(f"Port {MCP_PORT} is occupied before KnowledgeRadar startup.", file=sys.stderr)
    for row in listeners:
        print(f"  pid={row.get('pid')} command={row.get('commandline')}", file=sys.stderr)
    print("Close the process manually, or set KR_PORT_CONFLICT_AUTO_CLEAN=1 to stop an old KnowledgeRadar server automatically.", file=sys.stderr)
    return "blocked"


def _port_listeners(port: int) -> list[dict[str, str]]:
    if os.name != "nt":
        return []
    try:
        cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            (
                "$ErrorActionPreference='SilentlyContinue'; "
                f"Get-NetTCPConnection -LocalPort {port} -State Listen | "
                "ForEach-Object { "
                "$p=Get-CimInstance Win32_Process -Filter \"ProcessId=$($_.OwningProcess)\"; "
                "[pscustomobject]@{pid=$_.OwningProcess; commandline=$p.CommandLine} "
                "} | ConvertTo-Json -Compress"
            ),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=6)
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        import json

        data = json.loads(proc.stdout)
        if isinstance(data, dict):
            data = [data]
        return [{"pid": str(row.get("pid") or ""), "commandline": str(row.get("commandline") or "")} for row in data]
    except Exception:
        return []


def _is_current_server(commandline: str) -> bool:
    text = _norm(commandline)
    return _norm(KR_ROOT) in text and "src\\server.py" in text


def _is_knowledgeradar_server(commandline: str) -> bool:
    text = _norm(commandline)
    return "knowledgeradar" in text and "src\\server.py" in text


def _norm(value: str) -> str:
    return os.path.abspath(value).replace("/", "\\").lower() if value else ""


def _truthy_env(name: str, default: str = "0") -> bool:
    value = os.environ.get(name, default).strip().lower()
    return value in {"1", "true", "yes", "y", "on"}


def _stop_process(pid: str) -> None:
    if not pid:
        return
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True, timeout=8)
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
