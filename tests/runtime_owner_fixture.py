"""真实子进程验收夹具；不随产品分发，不加载账号或模型。"""
import os
from pathlib import Path
import time

from runtime import application
from runtime.execution_owner import serve
from runtime.knowledge import ReadRequest, ReadResult, read
from kr_core.evidence_assets import EvidenceAssets
from runtime.task_service import TaskService


def bootstrap():
    application.clear()
    service = TaskService()
    service.recover()

    def fixture_submit(sleep: float = 0, timeout_s: float = 10) -> dict:
        return service.submit('fixture_sleep', {'sleep': sleep, 'value': '中英 evidence'}, timeout_s=timeout_s)

    def get_task_status(task_id: str) -> dict:
        return service.result(task_id)

    def control_task(task_id: str, action: str) -> dict:
        if action == 'cancel':
            service.cancel(task_id)
            return service.result(task_id)
        if action == 'resume':
            return service.retry(task_id)
        raise ValueError('unsupported_action')

    def fixture_effect(sleep: float = 0, value: str = 'effect', wait_for_release: bool = False) -> dict:
        if wait_for_release:
            deadline = time.monotonic() + 30
            while not (Path(os.environ['KR_STATE_DIR']) / 'effect-release').is_file():
                if time.monotonic() >= deadline:
                    raise TimeoutError('fixture_release_timeout')
                time.sleep(.02)
        time.sleep(sleep)
        with (Path(os.environ['KR_STATE_DIR']) / 'effects.txt').open('a', encoding='utf-8') as stream:
            stream.write(value + '\n')
        return {'value': value}

    def read_knowledge(request: ReadRequest) -> ReadResult:
        return read(request, EvidenceAssets(Path(os.environ['KR_EVIDENCE_ROOT']), read_only=True))

    for function, readonly in [(fixture_submit, False), (get_task_status, True), (control_task, False),
                               (fixture_effect, False), (read_knowledge, True)]:
        application.register(function, group='fixture', read_only=readonly)
    return service.start_maintenance(interval_s=.05, keep_alive=True)


if __name__ == '__main__':
    serve(bootstrap=bootstrap)
