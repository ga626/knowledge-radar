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


def record_execution_progress(stage, **metrics):
    """事件驱动的有界诊断；与原任务同库，旧执行及终态不可回写。"""
    current = current_execution()
    if current is None:
        return
    store, task_id = current['store'], current['KR_DURABLE_TASK_ID']
    row = store.get_task(task_id) or {}
    meta = row.get('metadata') or {}
    if (row.get('status') != 'running' or meta.get('attempt_id') != current['attempt_id']
            or meta.get('execution_id') != current['execution_id']):
        return
    allowed = {'model_load_s','model_cache_hit','audio_decode_s','transcription_s','worker_total_s',
               'completed_chunks','resumed_chunks','processed_audio_s','download_s','resource_wait_s'}
    values = {k:v for k,v in metrics.items() if k in allowed and isinstance(v,(int,float)) and v >= 0}
    prior = meta.get('media_progress') or {}
    progress = {**prior, **values, 'stage': str(stage)[:80], 'observed_at': time.time()}
    if prior.get('stage') != stage:
        progress['stage_started_at'] = time.time()
    store._mark(task_id, 'running', metadata={'attempt_id':current['attempt_id'],
        'execution_id':current['execution_id'], 'media_progress':progress}, expected_updated_at=row['updated_at'])


@contextmanager
def execution_scope(values):
    token = _CURRENT.set(values)
    try:
        yield
    finally:
        _CURRENT.reset(token)
