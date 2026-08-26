"""Version-neutral lifecycle entry point for the local KnowledgeRadar console.

This file is copied beside ``active.json`` during activation.  It never reads
or prints configuration values; it only resolves the active program/data roots
and keeps one loopback-only console at the product's fixed address.
"""

from __future__ import annotations

import argparse
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
import webbrowser


CONSOLE_HOST = "127.0.0.1"
CONSOLE_PORT = 18882
CONSOLE_HEALTH_SCHEMA = "knowledgeradar-local-console/v1"
STARTUP_FILENAME = "KnowledgeRadar Local Console.cmd"


def _install_root() -> Path:
    return Path(__file__).resolve().parent


def _load_active(root: Path) -> dict[str, Path]:
    payload = json.loads((root / "active.json").read_text(encoding="utf-8"))
    program = Path(str(payload.get("program_root") or "")).resolve()
    data = Path(str(payload.get("data_root") or "")).resolve()
    if payload.get("schema") != "knowledgeradar-active-install/v1" or not (program / "src" / "onboarding" / "setup_wizard.py").is_file() or not data.is_dir():
        raise RuntimeError("当前 KnowledgeRadar 安装不可用；请运行产品安装器的 inspect 后再试。")
    return {"program": program, "data": data}


def _configure_environment(active: dict[str, Path], install_root: Path) -> None:
    program, data = active["program"], active["data"]
    os.environ.update(
        {
            "KR_PROJECT_ROOT": str(program),
            "KR_SOURCE_ROOT": str(program / "src"),
            "KR_INSTALL_ROOT": str(install_root),
            "KR_DATA_ROOT": str(data),
            "KR_RUNTIME_ENV_PATH": str(data / "config" / "runtime.env"),
            "KR_PROFILE_REGISTRY_PATH": str(data / "config" / "profile_registry.json"),
            "KR_BROWSER_DATA_DIR": str(data / "browser_data"),
            "KR_STATE_DIR": str(data / "state"),
            "KR_LOG_DIR": str(data / "logs"),
            "KR_MEDIA_CACHE_DIR": str(data / "state" / "media_cache"),
        }
    )
    source = str(program / "src")
    if source not in sys.path:
        sys.path.insert(0, source)


def _health(port: int) -> str:
    """Return ``ready``, ``absent``, or ``foreign`` without leaking a body."""
    try:
        connection = HTTPConnection(CONSOLE_HOST, port, timeout=0.8)
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8")) if response.status == 200 else {}
        return "ready" if payload.get("schema") == CONSOLE_HEALTH_SCHEMA and payload.get("status") == "ready" else "foreign"
    except (ConnectionError, OSError, TimeoutError, HTTPException, json.JSONDecodeError):
        return "absent"


def _request_stop(port: int) -> None:
    """Ask the known local console to stop through its per-page CSRF token."""
    connection = HTTPConnection(CONSOLE_HOST, port, timeout=2)
    connection.request("GET", "/")
    page = connection.getresponse().read().decode("utf-8")
    marker = 'nonce="'
    start = page.find(marker)
    if start < 0:
        raise RuntimeError("现有控制台不支持安全重启；请先关闭它后重试。")
    start += len(marker)
    end = page.find('"', start)
    token = page[start:end]
    if not token:
        raise RuntimeError("现有控制台未返回本地会话令牌；请先关闭它后重试。")
    connection = HTTPConnection(CONSOLE_HOST, port, timeout=2)
    connection.request(
        "POST",
        "/api/console/stop",
        body="{}",
        headers={"Content-Type": "application/json", "Origin": f"http://{CONSOLE_HOST}:{port}", "X-KR-Setup-Token": token},
    )
    if connection.getresponse().status != 200:
        raise RuntimeError("无法安全停止现有控制台。")


def _start_background(port: int) -> None:
    command = [sys.executable, str(Path(__file__).resolve()), "--serve", "--port", str(port), "--no-open"]
    options: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(command, **options)


def _wait_until_ready(port: int, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _health(port) == "ready":
            return True
        time.sleep(0.1)
    return False


def open_console(*, port: int, restart: bool, open_browser: bool) -> str:
    state = _health(port)
    if state == "foreign":
        raise RuntimeError(f"{CONSOLE_HOST}:{port} 已被其他程序占用；KnowledgeRadar 不会偷偷改用随机端口。")
    if state == "ready" and restart:
        _request_stop(port)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _health(port) == "ready":
            time.sleep(0.1)
        state = _health(port)
    if state == "absent":
        _start_background(port)
        if not _wait_until_ready(port):
            raise RuntimeError(f"KnowledgeRadar 控制台未能在 {CONSOLE_HOST}:{port} 启动。请检查该端口是否被其他程序占用。")
    elif state == "ready" and restart:
        raise RuntimeError("现有 KnowledgeRadar 控制台没有停止；未启动第二个实例。")
    url = f"http://{CONSOLE_HOST}:{port}/"
    if open_browser:
        webbrowser.open(url)
    print(url)
    return url


def serve_console(*, port: int) -> None:
    active = _load_active(_install_root())
    _configure_environment(active, _install_root())
    from onboarding.setup_wizard import run_wizard

    run_wizard(port=port, open_browser=False)


def _startup_path() -> Path:
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / STARTUP_FILENAME


def set_autostart(*, enabled: bool) -> str:
    path = _startup_path()
    if not enabled:
        path.unlink(missing_ok=True)
        return "disabled"
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    runtime = pythonw if pythonw.is_file() else Path(sys.executable)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "@echo off\r\n"
        f'"{runtime}" "{Path(__file__).resolve()}" --serve --port {CONSOLE_PORT} --no-open\r\n',
        encoding="utf-8",
    )
    return "enabled"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open or host the active local KnowledgeRadar console.")
    parser.add_argument("--port", type=int, default=CONSOLE_PORT)
    parser.add_argument("--serve", action="store_true", help="Host the console in this process.")
    parser.add_argument("--restart", action="store_true", help="Restart a known local console before opening it.")
    parser.add_argument("--no-open", action="store_true", help="Do not open the default browser.")
    autostart = parser.add_mutually_exclusive_group()
    autostart.add_argument("--enable-autostart", action="store_true")
    autostart.add_argument("--disable-autostart", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.enable_autostart:
        print(set_autostart(enabled=True))
        return 0
    if args.disable_autostart:
        print(set_autostart(enabled=False))
        return 0
    if args.serve:
        serve_console(port=args.port)
        return 0
    open_console(port=args.port, restart=args.restart, open_browser=not args.no_open)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
