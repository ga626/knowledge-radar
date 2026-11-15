from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from runtime import xhs_opencli as oc
from runtime.resource_concurrency import ResourceDeferred


@pytest.mark.parametrize("contention", ["lease", "bridge"])
def test_unsent_contention_returns_durable_work_to_existing_queue(monkeypatch, contention):
    monkeypatch.setenv("KR_DURABLE_WORKER", "1")
    monkeypatch.setattr(oc, "_stop_gate", lambda: {})
    monkeypatch.setattr(oc, "_json", lambda *args: {})
    sent = []

    @contextmanager
    def lease(operation):
        if contention == "lease":
            raise RuntimeError("browser_session_busy")
        yield

    monkeypatch.setattr(oc, "browser", lambda: SimpleNamespace(context_id="fixture", lease=lease))
    monkeypatch.setattr(oc, "_run", lambda *a, **kw: sent.append(True) or oc._failure("browser_session_busy", executed=False))
    with pytest.raises(ResourceDeferred) as raised:
        oc.invoke("detail", url="https://www.xiaohongshu.com/explore/0123456789abcdef01234567")
    assert raised.value.resource_kind == "opencli:xiaohongshu"
    assert len(sent) == (contention == "bridge")


def test_direct_caller_receives_busy_without_hidden_retry(monkeypatch):
    monkeypatch.delenv("KR_DURABLE_WORKER", raising=False)
    monkeypatch.setattr(oc, "_stop_gate", lambda: {})
    monkeypatch.setattr(oc, "_json", lambda *args: {})

    @contextmanager
    def lease(operation):
        raise RuntimeError("browser_session_busy")
        yield

    monkeypatch.setattr(oc, "browser", lambda: SimpleNamespace(context_id="fixture", lease=lease))
    assert oc.invoke("detail")["request_executed"] is False


def test_queue_waits_for_actual_browser_lease(monkeypatch):
    from platform_adapters.xiaohongshu_adapter import XiaohongshuAdapter
    from runtime import leases

    active = [True]
    calls = []
    def lookup(kind, key):
        calls.append((kind, key))
        return active if active else []
    monkeypatch.setattr(leases, "get_runtime_lease_coordinator", lambda: SimpleNamespace(resource_leases=lookup))
    assert XiaohongshuAdapter.resource_ready("opencli:xiaohongshu") is False
    active.clear()
    assert XiaohongshuAdapter.resource_ready("opencli:xiaohongshu") is True
    assert XiaohongshuAdapter.resource_ready("local_heavy") is None
    assert calls == [("browser_site", "daily-chrome:xiaohongshu")] * 2


@pytest.mark.parametrize("code,executed", [("browser_session_busy", True), ("browser_session_busy", None), ("COMMAND_RESULT_UNKNOWN", False)])
def test_possible_execution_and_unknown_outcomes_do_not_requeue(monkeypatch, code, executed):
    from contextlib import nullcontext
    monkeypatch.setenv("KR_DURABLE_WORKER", "1")
    monkeypatch.setattr(oc, "_stop_gate", lambda: {})
    monkeypatch.setattr(oc, "_json", lambda *args: {})
    monkeypatch.setattr(oc, "browser", lambda: SimpleNamespace(context_id="fixture", lease=lambda op: nullcontext()))
    result = {"ok": False, "failure_type": code, "request_executed": executed}
    monkeypatch.setattr(oc, "_run", lambda *a, **kw: result)
    assert oc.invoke("detail") is result
