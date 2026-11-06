"""执行身份的线程上下文；任务状态仍只存在 TaskStore 中。"""
from contextlib import contextmanager
from contextvars import ContextVar
import os
import time

_CURRENT = ContextVar('kr_execution', default=None)


class ExecutionStopped(RuntimeError):
    def __init__(self, code):
        self.error_code = code
        super().__init__(code)


def current_execution():
    return _CURRENT.get()


def execution_value(name, default=None):
    current = current_execution()
    if current is not None and name in current:
        return current[name]
    return os.environ.get(name, default)


def check_execution():
    current = current_execution()
    if current is None:
        return
    if time.time() >= current['deadline_at']:
        raise ExecutionStopped('deadline_exceeded')
    row = current['store'].get_task(current['KR_DURABLE_TASK_ID']) or {}
    meta = row.get('metadata') or {}
    if row.get('status') != 'running' or meta.get('attempt_id') != current['attempt_id'] or meta.get('execution_id') != current['execution_id']:
        raise ExecutionStopped('execution_superseded_or_cancelled')


@contextmanager
def execution_scope(values):
    token = _CURRENT.set(values)
    try:
        yield
    finally:
        _CURRENT.reset(token)
