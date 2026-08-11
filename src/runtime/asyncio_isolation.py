"""Helpers for running sync browser code from async-hosted MCP tools."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Callable, TypeVar


T = TypeVar("T")


def running_in_asyncio_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def run_sync_in_worker_if_asyncio(func: Callable[[], T], *, timeout_s: float, thread_name_prefix: str) -> T:
    """Run sync APIs in a worker when the current thread already owns an asyncio loop."""
    if not running_in_asyncio_loop():
        return func()

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix=thread_name_prefix) as executor:
        future = executor.submit(func)
        try:
            return future.result(timeout=max(1.0, float(timeout_s)))
        except FutureTimeoutError as exc:
            future.cancel()
            raise TimeoutError(f"sync browser worker timed out after {timeout_s:.1f}s") from exc
