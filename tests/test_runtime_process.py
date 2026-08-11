from __future__ import annotations

from pathlib import Path
import re
from types import SimpleNamespace

import runtime.process as process


class _StartupInfo:
    def __init__(self) -> None:
        self.dwFlags = 0
        self.wShowWindow = None


def test_silent_subprocess_run_hides_windows_by_default(monkeypatch) -> None:
    captured: dict = {}

    monkeypatch.setattr(process.os, "name", "nt", raising=False)
    monkeypatch.setattr(process.subprocess, "STARTUPINFO", _StartupInfo, raising=False)
    monkeypatch.setattr(process.subprocess, "STARTF_USESHOWWINDOW", 1, raising=False)
    monkeypatch.setattr(process.subprocess, "SW_HIDE", 0, raising=False)
    monkeypatch.setattr(process.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(process.subprocess, "run", fake_run)

    result = process.silent_subprocess_run(["python", "--version"], creationflags=4)

    assert result.returncode == 0
    assert captured["kwargs"]["creationflags"] == 0x08000004
    startupinfo = captured["kwargs"]["startupinfo"]
    assert startupinfo.dwFlags & 1
    assert startupinfo.wShowWindow == 0


def test_silent_subprocess_run_respects_debug_window_switch(monkeypatch) -> None:
    captured: dict = {}

    monkeypatch.setattr(process.os, "name", "nt", raising=False)
    monkeypatch.setenv("KR_SHOW_CHILD_CONSOLES", "1")

    def fake_run(*args, **kwargs):
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(process.subprocess, "run", fake_run)

    process.silent_subprocess_run(["python", "--version"], creationflags=4)

    assert captured["kwargs"]["creationflags"] == 4
    assert captured["kwargs"]["startupinfo"] is None


def test_runtime_code_uses_silent_subprocess_wrappers() -> None:
    src_root = Path(__file__).resolve().parents[1] / "src"
    allowed = {src_root / "runtime" / "process.py"}
    pattern = re.compile(
        r"\bsubprocess\.(?:run|Popen|check_output|check_call)\s*\("
        r"|\basyncio\.create_subprocess(?:_exec|_shell)?\s*\("
    )
    offenders: list[str] = []
    for path in src_root.rglob("*.py"):
        if path in allowed or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in pattern.finditer(text):
            rel = path.relative_to(src_root.parent)
            line = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{rel}:{line}:{match.group(0).strip()}")

    assert offenders == []
