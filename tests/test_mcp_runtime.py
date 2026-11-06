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


def test_public_runtime_cli_preserves_busy_work(monkeypatch, capsys) -> None:
    from runtime import cli
    class Client:
        def stop(self):
            return {'status': 'busy', 'next_action': 'wait_for_accepted_work'}
    monkeypatch.setattr(cli, 'OwnerClient', Client)
    assert cli.main(['runtime', 'stop']) == 2
    assert 'busy' in capsys.readouterr().out


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


def test_public_runtime_status_never_starts_or_stops_a_transport(monkeypatch) -> None:
    from runtime.execution_owner import OwnerClient, source_identity, PROTOCOL
    client = OwnerClient()
    monkeypatch.setattr(client, 'descriptor', lambda: {'pid': 1, 'source_identity': source_identity(), 'protocol': PROTOCOL})
    calls = []
    monkeypatch.setattr(client, '_request', lambda descriptor, path, **kwargs: calls.append(path) or {'status': 'ready'})
    assert client.status()['status'] == 'ready'
    assert calls == ['status']
