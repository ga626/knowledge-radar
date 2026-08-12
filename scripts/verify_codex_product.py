"""Public, self-contained checks for the KnowledgeRadar Codex stdio contract."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_TOOLS = {
    "health_check",
    "get_capabilities",
    "kr_research",
    "finalize_research_task",
    "search_bilibili",
    "search_wechat_articles",
    "search_xiaohongshu",
    "search_zhihu",
}


def _reader(handle: Any, lines: queue.Queue[str]) -> None:
    for line in iter(handle.readline, ""):
        lines.put(line)


def _request(process: subprocess.Popen[str], lines: queue.Queue[str], request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    assert process.stdin is not None
    process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
    process.stdin.flush()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=0.2).strip()
        except queue.Empty:
            if process.poll() is not None:
                raise RuntimeError(f"stdio server exited with {process.returncode}")
            continue
        if not line:
            continue
        message = json.loads(line)
        if message.get("id") == request_id:
            if "error" in message:
                raise RuntimeError(json.dumps(message["error"], ensure_ascii=False))
            return message
    raise RuntimeError(f"timed out waiting for {method}")


def _stop_process(process: subprocess.Popen[str], reader: threading.Thread | None) -> None:
    """Release all stdio handles before Windows removes temporary probe state."""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    for handle in (process.stdin, process.stdout, process.stderr):
        if handle is not None:
            handle.close()
    if reader is not None:
        reader.join(timeout=2)


def stdio_probe(root: Path = ROOT) -> dict[str, Any]:
    root = root.resolve()
    src = root / "src"
    python_exe = root / ".python312" / "python.exe"
    if not python_exe.is_file():
        python_exe = Path(sys.executable).resolve()
    env = dict(os.environ)
    with tempfile.TemporaryDirectory(prefix="knowledgeradar-stdio-probe-") as state:
        state_root = Path(state)
        env.update({
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(src),
            "KR_MCP_TRANSPORT": "stdio",
            "KR_PROJECT_ROOT": str(root),
            "KR_SOURCE_ROOT": str(src),
            "KR_DATA_ROOT": str(state_root / "data"),
            "KR_RUNTIME_ENV_PATH": str(state_root / "data" / "config" / "runtime.env"),
            "KR_STATE_DIR": str(state_root / "state"),
            "KR_LOG_DIR": str(state_root / "logs"),
        })
        process = subprocess.Popen(
            [str(python_exe), "-X", "utf8", str(src / "server.py")],
            cwd=root,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        lines: queue.Queue[str] = queue.Queue()
        reader: threading.Thread | None = None
        try:
            assert process.stdout is not None
            reader = threading.Thread(target=_reader, args=(process.stdout, lines), daemon=True)
            reader.start()
            _request(process, lines, 1, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "knowledgeradar-public-verifier", "version": "1"}})
            response = _request(process, lines, 2, "tools/list", {})
            names = sorted(item["name"] for item in response.get("result", {}).get("tools", []) if isinstance(item, dict) and item.get("name"))
            missing = sorted(REQUIRED_TOOLS - set(names))
            return {"status": "PASS" if not missing else "FAIL", "tool_count": len(names), "tools": names, "missing_required": missing}
        except (OSError, RuntimeError, json.JSONDecodeError) as exc:
            return {"status": "FAIL", "error": str(exc)}
        finally:
            _stop_process(process, reader)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the public KnowledgeRadar Codex stdio contract.")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = stdio_probe(Path(args.root))
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else f"{result['status']}: {result.get('tool_count', 0)} tools")
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
