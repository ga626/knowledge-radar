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
