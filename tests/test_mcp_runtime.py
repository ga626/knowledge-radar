from __future__ import annotations

# ruff: noqa: E402

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for path in (ROOT, SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from runtime import mcp_runtime


def _load_runtime_cli_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("kr_mcp_runtime_test", ROOT / "scripts" / "kr_mcp_runtime.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_cli_treats_restart_pending_as_successful_deferral(monkeypatch) -> None:
    module = _load_runtime_cli_module()
    monkeypatch.setattr(module, "request_runtime_refresh", lambda *_args, **_kwargs: {"status": "restart_pending"})

    assert module.main(["--request", "--reason", "test", "--json"]) == 0


def test_pending_runtime_defers_without_calling_restart_when_busy(tmp_path: Path, monkeypatch) -> None:
    mcp_runtime.request_runtime_refresh(tmp_path, reason="commit", requested_by="test")
    monkeypatch.setattr(mcp_runtime, "runtime_activity", lambda _root: {"safe": False, "reasons": ["active_tasks"]})
    called = False

    def restart(_root: Path) -> dict:
        nonlocal called
        called = True
        return {"returncode": 0}

    result = mcp_runtime.try_apply_pending_runtime(tmp_path, restart=restart)

    assert result["action"] == "deferred"
    assert result["reason"] == "runtime_busy"
    assert called is False


def test_pending_runtime_restarts_once_when_idle(tmp_path: Path, monkeypatch) -> None:
    mcp_runtime.request_runtime_refresh(tmp_path, reason="commit", requested_by="test")
    monkeypatch.setattr(mcp_runtime, "runtime_activity", lambda _root: {"safe": True, "reasons": []})

    class Lease:
        acquired = True
        lease_id = "test-switch"
        retry_after_s = 0

    class Coordinator:
        def acquire_exclusive(self, *_args, **_kwargs):
            return Lease()

        def release(self, lease_id: str) -> bool:
            return lease_id == "test-switch"

    monkeypatch.setattr(mcp_runtime, "get_runtime_lease_coordinator", Coordinator)

    result = mcp_runtime.try_apply_pending_runtime(tmp_path, restart=lambda _root: {"returncode": 0, "protocol": "ok"})

    assert result["status"] == "PASS"
    assert result["action"] == "restarted"
    assert result["state"]["pending"] is False


def test_http_restart_script_does_not_target_configured_stdio_processes() -> None:
    script = (ROOT / "scripts" / "kr_restart_server.ps1").read_text(encoding="utf-8-sig")
    assert "function Get-KrHttpServerProcesses" in script
    assert "Get-KrServerProcesses" not in script
    assert "$targetPids = @(Get-KrHttpServerProcesses" in script
    assert "preserved_stdio_sessions = $true" in script
