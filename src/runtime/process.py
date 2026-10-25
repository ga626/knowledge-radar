"""Subprocess helpers for quiet Windows child processes."""

from __future__ import annotations

import os
import subprocess
from typing import Any


def _show_child_consoles() -> bool:
    return os.environ.get("KR_SHOW_CHILD_CONSOLES", "").strip().lower() in {"1", "true", "yes", "on"}


def silent_startupinfo() -> subprocess.STARTUPINFO | None:
    if os.name != "nt" or _show_child_consoles():
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return startupinfo


def silent_creationflags(existing: int = 0) -> int:
    if os.name != "nt" or _show_child_consoles():
        return existing
    return existing | getattr(subprocess, "CREATE_NO_WINDOW", 0)


def silent_subprocess_run(*popenargs: Any, **kwargs: Any) -> subprocess.CompletedProcess:
    """Run a child process without flashing a console window on Windows."""
    kwargs.setdefault("startupinfo", silent_startupinfo())
    kwargs["creationflags"] = silent_creationflags(int(kwargs.get("creationflags") or 0))
    return subprocess.run(*popenargs, **kwargs)


def silent_subprocess_popen(*popenargs: Any, **kwargs: Any) -> subprocess.Popen:
    """Start a child process without flashing a console window on Windows."""
    kwargs.setdefault("startupinfo", silent_startupinfo())
    kwargs["creationflags"] = silent_creationflags(int(kwargs.get("creationflags") or 0))
    return subprocess.Popen(*popenargs, **kwargs)


def isolated_worker_run(command, *, input: str, env: dict, timeout: float) -> subprocess.CompletedProcess:
    """模型超时须结束整棵自有进程树，包括 Windows venv 启动器的子进程。"""
    import signal
    from pathlib import Path

    process = silent_subprocess_popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", env=env, start_new_session=os.name != "nt",
    )
    try:
        stdout, stderr = process.communicate(input=input, timeout=timeout)
    except BaseException as error:
        if process.poll() is None:
            if os.name == "nt":
                taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"
                silent_subprocess_run([str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                                      capture_output=True, timeout=10)
            else:
                os.killpg(process.pid, signal.SIGKILL)
            if process.poll() is None:
                process.kill()
        stdout, stderr = process.communicate(timeout=10)
        if isinstance(error, subprocess.TimeoutExpired):
            error.output, error.stderr = stdout, stderr
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
