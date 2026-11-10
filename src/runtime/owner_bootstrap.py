"""标准库启动壳：执行内核模块导入失败时仍留下有限、同代的失败事实。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import runpy
import uuid


def record_failure(root, exc, *, identity, stage):
    from runtime.runtime_control import classify_failure
    root = Path(root)
    path = root/'bootstrap-failure.json'
    temporary = root/('bootstrap-'+uuid.uuid4().hex+'.tmp')
    try:
        root.mkdir(parents=True, exist_ok=True)
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump({'schema': 'kr-runtime-bootstrap-failure/v1', 'launcher_identity': identity,
                       'launch_id': os.environ.get('KR_OWNER_LAUNCH_ID'),
                       'failure': classify_failure(exc, stage=stage)}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    try:
        runpy.run_module('runtime.execution_owner', run_name='__main__')
    except Exception as exc:
        root = os.environ.get('KR_OWNER_ROOT')
        if root:
            try:
                record_failure(root, exc, identity=os.environ.get('KR_OWNER_SOURCE_IDENTITY'),
                               stage='owner_import_or_bootstrap')
            except (OSError, ImportError):
                pass  # 记录也无法写入时仍按原异常退出，stderr保留实际失败。
        raise


if __name__ == '__main__':
    main()
