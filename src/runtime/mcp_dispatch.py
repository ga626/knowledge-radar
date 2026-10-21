"""MCP 异步边界：有限线程接短I/O，保留直接Python调用合同。"""
from concurrent.futures import ThreadPoolExecutor
import asyncio
import contextvars
from functools import wraps
import inspect
import threading

_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="kr-short-io")
_PENDING = threading.BoundedSemaphore(24)
_CONTROL_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kr-control")
_CONTROL_PENDING = threading.BoundedSemaphore(8)


def nonblocking_mcp_tool(mcp):
    def register(fn):
        @wraps(fn)
        async def dispatch(*args, **kwargs):
            control = fn.__name__ in {"health_check", "get_capabilities"} or (fn.__name__ == "get_task_status" and not kwargs.get("wait"))
            pending = _CONTROL_PENDING if control else _PENDING
            executor = _CONTROL_EXECUTOR if control else _EXECUTOR
            if not pending.acquire(blocking=False):
                return {"error": {"type": "runtime_busy", "retryable": True}, "status": "degraded"}
            context = contextvars.copy_context()
            try:
                future = executor.submit(context.run, fn, *args, **kwargs)
            except BaseException:
                pending.release()
                raise
            future.add_done_callback(lambda _: pending.release())
            return await asyncio.wrap_future(future)
        dispatch.__signature__ = inspect.signature(fn)
        mcp.tool()(dispatch)
        return fn
    return register
