import asyncio
from pathlib import Path
import time

from mcp.server.fastmcp import FastMCP

from runtime.mcp_dispatch import nonblocking_mcp_tool
from runtime.tasks import TaskStore


def test_sdk_handler_keeps_event_loop_and_health_responsive():
    mcp = FastMCP("isolated-dispatch")
    @nonblocking_mcp_tool(mcp)
    def busy(delay: float = 0.25) -> dict:
        """模拟阻塞I/O。"""
        time.sleep(delay)
        return {"done": True}
    @nonblocking_mcp_tool(mcp)
    def health_check() -> dict:
        """独立控制通道。"""
        return {"status": "ok"}
    async def scenario():
        workers = [asyncio.create_task(mcp.call_tool("busy", {})) for _ in range(8)]
        start = time.monotonic()
        await asyncio.sleep(0.02)
        await mcp.call_tool("health_check", {})
        elapsed = time.monotonic() - start
        await asyncio.gather(*workers)
        return elapsed
    assert asyncio.run(scenario()) < 0.18
    assert health_check()["status"] == "ok"  # 原Python调用仍同步。


def test_cancelled_task_cannot_be_completed_or_failed_by_late_writer(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    store.upsert_task("task", "fixture", "test")
    store.mark_running("task")
    store.cancel_task("task")
    assert store.mark_completed("task")["status"] == "cancelled"
    assert store.mark_failed("task", "late error")["status"] == "cancelled"


def test_task_claim_is_exclusive_and_interrupt_terminal(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    store.upsert_task("task", "fixture", "test")
    assert store.claim_queued("task", {"attempt_id": "a"})
    assert not store.claim_queued("task", {"attempt_id": "b"})
    assert store.get_task("task")["attempts"] == 1
    store.mark_interrupted("task")
    assert store.mark_completed("task")["status"] == "interrupted"


def test_local_task_cannot_promote_structured_failure_to_completed(tmp_path, monkeypatch):
    from runtime.task_adapter import LocalTaskAdapter, LocalTaskSpec
    monkeypatch.setenv("KR_DURABLE_WORKER", "1")
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path / "state"))
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    adapter = LocalTaskAdapter(store)
    adapter.submit(LocalTaskSpec("failure", "fixture", "test"), lambda: {"failure_type": "empty_detail", "status": "empty"})
    assert store.get_task("failure")["status"] == "failed"
    adapter.submit(LocalTaskSpec("manual", "fixture", "test"), lambda: {"manual_action_required": True})
    assert store.get_task("manual")["status"] == "waiting_for_user"
