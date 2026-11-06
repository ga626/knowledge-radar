"""公共PDF抽字的自有隔离进程；不读账号、不联网、不调用OCR。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def extract_isolated(source, *, timeout_s, **options):
    from runtime.resource_concurrency import acquire_resource
    started=time.monotonic()
    with acquire_resource('local_heavy',wait_timeout_s=timeout_s):
        remaining=timeout_s-(time.monotonic()-started)
        if remaining<=0:
            raise TimeoutError('pdf_extraction_deadline_exceeded')
        return _extract_isolated(source,timeout_s=remaining,**options)


def _extract_isolated(source, *, timeout_s, **options):
    from runtime.execution_context import check_execution
    from runtime.paths import runtime_state_dir
    check_execution()
    root = runtime_state_dir() / 'document_workers'
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='pdf-', dir=root) as folder:
        folder = Path(folder)
        source_path = folder / 'input.pdf'
        if isinstance(source, bytes):
            source_path.write_bytes(source)
        else:
            import shutil
            shutil.copyfile(source, source_path)
        (folder / 'request.json').write_text(json.dumps(options), encoding='utf-8')
        command = [sys.executable, str(Path(__file__).resolve()), str(folder)]
        # 子进程只需要系统执行环境；不继承API Key或运行数据位置。
        environment = {key:value for key,value in os.environ.items()
            if key.upper() in {'SYSTEMROOT','WINDIR','PATH','TEMP','TMP','COMSPEC','PATHEXT','LOCALAPPDATA'}}
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=environment, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        deadline = time.monotonic() + max(.1, timeout_s)
        try:
            while process.poll() is None:
                check_execution()
                if time.monotonic() >= deadline:
                    raise TimeoutError('pdf_extraction_deadline_exceeded')
                time.sleep(.05)
            check_execution()
            result_path = folder / 'result.json'
            if not result_path.is_file() or result_path.stat().st_size > 32*1024*1024:
                raise ValueError('pdf_worker_resource_limit_or_failure')
            result = json.loads(result_path.read_text(encoding='utf-8'))
            if result.get('worker_error'):
                raise ValueError(result['worker_error'])
            if process.returncode:
                raise ValueError('pdf_worker_nonzero_exit')
            return result
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def _memory_limit():
    limit = 768 * 1024 * 1024
    if os.name == 'nt':
        import win32api
        import win32job
        job = win32job.CreateJobObject(None, '')
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info['BasicLimitInformation']['LimitFlags'] = win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY
        info['ProcessMemoryLimit'] = limit
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        win32job.AssignProcessToJobObject(job, win32api.GetCurrentProcess())
        return job
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    return None


def main():
    folder = Path(sys.argv[1]).resolve()
    # 同一已安装源码入口，兼容嵌入式Python忽略PYTHONPATH的设置。
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        job = _memory_limit()  # 保持句柄直至处理完成。
        from runtime.pdf_text import extract_pdf_text
        options = json.loads((folder / 'request.json').read_text(encoding='utf-8'))
        result = extract_pdf_text(str(folder / 'input.pdf'), **options)
        result['extraction_boundary'] = {'process':'owned_worker','memory_limit_bytes':768*1024*1024}
        del job
    except Exception as exc:
        result = {'worker_error':str(exc) if isinstance(exc, ValueError) else type(exc).__name__}
    (folder / 'result.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')


if __name__ == '__main__':
    main()
