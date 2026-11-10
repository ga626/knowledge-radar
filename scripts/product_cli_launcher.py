"""稳定CLI入口和安装维护交接；只依赖标准库，不注册另一套服务。"""
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from urllib.request import Request, ProxyHandler, build_opener


def _stable_task(action):
    if os.name != 'nt':
        raise RuntimeError('runtime_user_session_task_requires_windows')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
        "$ErrorActionPreference='Stop'; " + action + "-ScheduledTask -TaskName 'KnowledgeRadar Stable Local Console' | Out-Null"],
        capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError('runtime_user_session_task_control_failed_' + action.lower())


def _json(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _process_identity_module(package_root):
    spec = importlib.util.spec_from_file_location('kr_handoff_identity', Path(package_root)/'src/runtime/process_identity.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wake_runtime(install_root):
    active = _json(Path(install_root)/'active.json')
    if not active:
        return
    path = Path(active['data_root'])/'state/execution_owner/start.request.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'request_id': uuid.uuid4().hex}), encoding='utf-8')
    temporary.replace(path)


@contextmanager
def runtime_manager_handoff(active, *, install_root, package_root):
    """先暂停OS入口并请求管理器安全退出，再进入内核维护锁。

    只发送本安装的控制文件，不按PID杀进程。过时PID无法确认退出时停止交接。
    失败也恢复原任务；启动发生在调用者释放maintenance锁之后。
    """
    root = Path(install_root)
    registered = any((root/name).is_file() for name in ('runtime-autostart.task', 'console-autostart.task'))
    try:
        if active:
            if registered:
                _stable_task('Disable')
            state = Path(active['data_root'])/'state/console_supervisor/stable'
            snapshot = _json(state/'status.json')
            process_id = snapshot.get('supervisor_pid')
            identity = _process_identity_module(package_root)
            observed = identity.process_identity(process_id) if type(process_id) is int and process_id > 0 else ''
            if observed:
                request = state/'shutdown.request.json'
                state.mkdir(parents=True, exist_ok=True)
                request.write_text(json.dumps({'schema':'knowledgeradar-console-shutdown-request/v1', 'reason':'installation_handoff'}), encoding='utf-8')
                deadline = time.monotonic()+40
                try:
                    while identity.process_identity(process_id) == observed:
                        runtime = _json(Path(active['data_root'])/'state/execution_owner/supervision.json')
                        if runtime.get('event') == 'manager_stop_refused_busy' and not request.exists():
                            raise RuntimeError('execution_owner_busy_before_install')
                        if time.monotonic() >= deadline:
                            raise RuntimeError('runtime_manager_shutdown_unconfirmed')
                        time.sleep(.1)
                finally:
                    request.unlink(missing_ok=True)
        yield
    finally:
        if any((root/name).is_file() for name in ('runtime-autostart.task', 'console-autostart.task')):
            _wake_runtime(root)
            _stable_task('Enable')
            _stable_task('Start')


def active_environment(active):
    program = Path(active['program_root']).resolve()
    data = Path(active['data_root']).resolve()
    return {'PYTHONPATH': str(program/'src'), 'KR_MCP_TRANSPORT': 'stdio',
            'KR_PROJECT_ROOT': str(program), 'KR_SOURCE_ROOT': str(program/'src'),
            'KR_INSTALL_ROOT': str(program.parents[1]), 'KR_DATA_ROOT': str(data),
            'KR_RUNTIME_ENV_PATH': str(data/'config/runtime.env'),
            'KR_PROFILE_REGISTRY_PATH': str(data/'config/profile_registry.json'),
            'KR_BROWSER_DATA_DIR': str(data/'browser_data'), 'KR_STATE_DIR': str(data/'state'),
            'KR_BROWSER_LINK_STATE_FILE': str(data/'runtime/browser_link/host.json'),
            'KR_LOG_DIR': str(data/'logs'), 'KR_ACTIVE_INSTALL_ROOT': str(program.parents[1]),
            'KR_ACTIVE_VERSION': str(active.get('installation_id') or active.get('version') or ''),
            'KR_MEDIA_CACHE_DIR': str(data/'state/media_cache'),
            'PLAYWRIGHT_BROWSERS_PATH': str(data/'playwright')}


@contextmanager
def maintenance(active, *, package_root, owner_directory=None):
    """与启动共用锁；只停空闲owner，不杀进程或自动取消任务。"""
    root = Path(owner_directory) if owner_directory else Path(active['data_root'])/'state/execution_owner'
    spec = importlib.util.spec_from_file_location('kr_install_file_lock', Path(package_root)/'src/runtime/file_write_lock.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with module.file_write_lock(root/'launch'):
        path = root/'owner.json'
        if path.is_file():
            descriptor = json.loads(path.read_text(encoding='utf-8'))
            # 失主描述符由下次owner恢复；不能把PID复用误认成活跃服务。
            identity_spec = importlib.util.spec_from_file_location('kr_install_process_identity', Path(package_root)/'src/runtime/process_identity.py')
            identity = importlib.util.module_from_spec(identity_spec)
            identity_spec.loader.exec_module(identity)
            if type(descriptor.get('pid')) is not int or descriptor['pid'] <= 0:
                raise RuntimeError('invalid_execution_owner_descriptor')
            if identity.process_identity(descriptor['pid']) == descriptor.get('process_identity') and descriptor.get('process_identity'):
                if descriptor.get('protocol') != 1:
                    raise RuntimeError('execution_owner_protocol_mismatch_before_install')
                payload = {key: descriptor[key] for key in ('protocol','generation','source_identity')}
                request = Request(f"http://127.0.0.1:{descriptor['port']}/stop", data=json.dumps(payload).encode(),
                                  headers={'Content-Type':'application/json','Authorization':'Bearer '+descriptor['token']})
                try:
                    with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
                        value = json.load(response)
                except OSError as exc:
                    raise RuntimeError('execution_owner_unreachable_before_install') from exc
                if value.get('status') != 'stopping':
                    raise RuntimeError('execution_owner_busy_before_install')
                end = time.monotonic()+10
                while (path.is_file() or identity.process_identity(descriptor['pid']) == descriptor['process_identity']) and time.monotonic() < end:
                    time.sleep(.05)
                if path.is_file() or identity.process_identity(descriptor['pid']) == descriptor['process_identity']:
                    raise RuntimeError('execution_owner_shutdown_incomplete')
        yield


def main(argv=None, *, install_root=None):
    try:
        root = Path(install_root or Path(__file__).resolve().parent)
        active = json.loads((root/'active.json').read_text(encoding='utf-8'))
        program = Path(active['program_root']).resolve()
        runtime = root/'runtime'/str(active.get('installation_id') or active['version'])
        executable = runtime/('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        if not executable.is_file() or not (program/'src/runtime/cli.py').is_file():
            raise RuntimeError('active_cli_runtime_unavailable')
        env = dict(os.environ)
        env.update(active_environment(active))
        env.pop('KR_OWNER_ROOT', None)
        env.pop('KR_RUNTIME_ROLE', None)
        bootstrap = 'import sys,runpy; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module("runtime.cli",run_name="__main__")'
        return subprocess.run([str(executable),'-X','utf8','-c',bootstrap,str(program/'src'),*(sys.argv[1:] if argv is None else argv)],
                              cwd=program,env=env,check=False).returncode
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(json.dumps({'status':'error','error':{'layer':'product_launcher','code':type(exc).__name__,
                         'execution_state':'not_started','next_action':'Inspect active.json and installation receipt.'}}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
