"""内核退出、熔断及薄MCP目录的回归；不触碰正式安装。"""
import asyncio
import json
import os
from pathlib import Path
import sys
import subprocess
import time
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('active', [False, True])
def test_mcp_catalog_does_not_launch_kernel(tmp_path, monkeypatch, active):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    env = dict(os.environ, KR_MCP_TRANSPORT='stdio', KR_OWNER_ROOT=str(tmp_path/'owner'),
               KR_DATA_ROOT=str(tmp_path/'data'), KR_STATE_DIR=str(tmp_path/'state'),
               KR_LOG_DIR=str(tmp_path/'logs'), KR_RUNTIME_ENV_PATH=str(tmp_path/'absent.env'),
               KR_ACTIVE_INSTALL_ROOT=str(tmp_path/'missing-install'),
               KR_DESKTOP_NOTIFICATIONS_DISABLED='1', KR_DISABLE_MODEL_PREWARM='1')
    if active:
        install = tmp_path/'missing-install'
        install.mkdir()
        (install/'active.json').write_text(json.dumps({'program_root': str(ROOT), 'data_root': str(tmp_path/'data')}))

    async def scenario():
        params = StdioServerParameters(command=sys.executable,
            args=['-X', 'utf8', str(ROOT/'src/server.py')], cwd=str(ROOT), env=env)
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert len(tools.tools) == 27
                result = await session.call_tool('health_check', {'mode': 'summary'})
                assert result.isError
                assert ('execution_owner_unavailable' if active else 'inactive_installation_client') in str(result)
    asyncio.run(asyncio.wait_for(scenario(), timeout=20))
    assert not (tmp_path/'owner/owner.json').exists()


class Child:
    pid = 12345
    returncode = None
    def poll(self):
        return self.returncode


def supervisor(tmp_path, monkeypatch, *, limit=2):
    from runtime import execution_owner as owner
    monkeypatch.setattr(owner, 'active_client', lambda: True)
    client = owner.OwnerClient(root=tmp_path/'owner')
    children = []
    def start(**kwargs):
        assert kwargs['managed'] is True
        child = Child()
        children.append(child)
        client.child = child
        return {'status': 'ready', 'generation': str(len(children)), 'pid': child.pid}
    monkeypatch.setattr(client, 'start', start)
    return owner.OwnerSupervisor(client, max_restarts=limit), children


def test_crash_records_exit_and_recovers_with_bounded_budget(tmp_path, monkeypatch):
    manager, children = supervisor(tmp_path, monkeypatch)
    manager.tick(now=0)
    for moment in (1, 10, 30):
        children[-1].returncode = 137
        manager.tick(now=moment)
        manager.tick(now=moment+8)
    assert len(children) == 3  # initial + two retries, no fourth process
    assert manager.state == 'circuit_open'
    records = [json.loads(line) for line in (manager.client.root/'lifecycle.jsonl').read_text().splitlines()]
    exited = [item for item in records if item['event'] == 'owner_exited']
    assert len(exited) == 3
    assert exited[-1]['exit_code'] == 137
    assert exited[-1]['generation'] == '3'
    manager.tick(now=10000)
    assert len(children) == 3  # no infinite restart after a timed cooldown
    from runtime.execution_owner import OwnerSupervisor
    replacement = OwnerSupervisor(manager.client, max_restarts=2)
    replacement.tick(now=20000)
    assert replacement.state == 'circuit_open'
    assert len(children) == 3  # OS重启管理器也不能清空熔断


def test_intentional_stop_and_install_switch_do_not_restart(tmp_path, monkeypatch):
    from runtime import execution_owner as owner
    manager, children = supervisor(tmp_path, monkeypatch)
    manager.tick(now=0)
    (manager.client.root/'stop.json').write_text(json.dumps({'generation': '1', 'reason': 'explicit_stop'}))
    children[-1].returncode = 0
    manager.tick(now=1)
    manager.tick(now=100)
    assert manager.state == 'stopped'
    assert len(children) == 1
    monkeypatch.setattr(owner, 'active_client', lambda: False)
    manager.tick(now=101)
    assert manager.state == 'inactive'
    assert len(children) == 1


def test_start_failures_are_bounded_and_diagnostics_are_read_only(tmp_path, monkeypatch):
    manager, children = supervisor(tmp_path, monkeypatch, limit=1)
    def denied(**kwargs):
        raise RuntimeError('launch_denied')
    monkeypatch.setattr(manager.client, 'start', denied)
    manager.tick(now=0)
    manager.tick(now=10)
    assert manager.state == 'circuit_open'
    before = list(manager.client.root.iterdir())
    assert manager.client.status()['supervision']['state'] == 'circuit_open'
    assert list(manager.client.root.iterdir()) == before
    assert not children


def test_explicit_start_can_reopen_circuit_once(tmp_path, monkeypatch):
    manager, children = supervisor(tmp_path, monkeypatch, limit=0)
    manager.tick(now=0)
    children[-1].returncode = 9
    manager.tick(now=1)
    assert manager.state == 'circuit_open'
    (manager.client.root/'start.request.json').write_text(json.dumps({'request_id': 'operator-retry'}))
    manager.tick(now=2)
    assert len(children) == 2
    children[-1].returncode = 9
    manager.tick(now=3)
    manager.tick(now=100)
    assert manager.state == 'circuit_open'
    assert len(children) == 2


def test_late_ready_after_launch_timeout_is_observed(tmp_path, monkeypatch):
    manager, children = supervisor(tmp_path, monkeypatch)
    original = manager.client.start
    def delayed(**kwargs):
        original(**kwargs)
        raise TimeoutError('startup-still-running')
    monkeypatch.setattr(manager.client, 'start', delayed)
    monkeypatch.setattr(manager.client, 'status', lambda: {'status': 'ready', 'generation': 'late'})
    manager.tick(now=0)
    assert manager.state == 'starting'
    manager.tick(now=1)
    assert manager.state == 'ready'
    assert manager.generation == 'late'
    assert len(children) == 1


def test_manager_restart_accounts_for_unobserved_kernel_exit(tmp_path, monkeypatch):
    from runtime import execution_owner as owner
    manager, children = supervisor(tmp_path, monkeypatch, limit=0)
    manager.tick(now=0)
    # 模拟管理器与内核一并退出：只留下上一代ready日志，当前描述符已失主。
    replacement = owner.OwnerSupervisor(manager.client, max_restarts=0)
    replacement.tick(now=100)
    assert replacement.state == 'circuit_open'
    assert len(children) == 1
    records = [json.loads(line) for line in (manager.client.root/'lifecycle.jsonl').read_text().splitlines()]
    lost = [item for item in records if item['event'] == 'owner_exit_unobserved']
    assert lost[-1]['exit_code'] is None  # 不能编造不可观测的退出码


@pytest.mark.skipif(os.name != 'nt',reason='Windows user-session task control')
def test_explicit_start_can_resume_previously_disabled_user_task(tmp_path, monkeypatch):
    from runtime import execution_owner as owner
    monkeypatch.setattr(owner,'active_client',lambda:True)
    monkeypatch.setenv('KR_ACTIVE_INSTALL_ROOT',str(tmp_path/'install'))
    client=owner.OwnerClient(root=tmp_path/'owner')
    ready=False
    def run(command, **kwargs):
        nonlocal ready
        ready='Enable-ScheduledTask' in command[-1]
        return subprocess.CompletedProcess(command,0 if ready else 1)
    monkeypatch.setattr(owner,'silent_subprocess_run',run)
    monkeypatch.setattr(client,'status',lambda:{'status':'ready' if ready else 'unavailable'})
    assert client.start(timeout=.1)['status']=='ready'


def probe_os_user_task_starts_kernel_outside_restrictive_stdio_job(tmp_path):
    """只注册临时任务；从禁止breakaway的Job请求OS启动，最后回收自己资产。"""
    if os.name != 'nt' or os.environ.get('KR_RUNTIME_USER_TASK_PROBE') != '1':
        raise RuntimeError('explicit_windows_user_task_probe_required')
    from scripts import product_console_launcher as console
    from runtime.execution_owner import OwnerClient
    pythonw = os.environ['KR_RUNTIME_PROBE_PYTHON']
    task = 'KR Lifecycle Probe ' + uuid.uuid4().hex
    helper = tmp_path/'console_product.py'
    helper.write_text((ROOT/'scripts/product_console_launcher.py').read_text(encoding='utf-8').replace(
        'KnowledgeRadarConsoleSupervisor-', task+'-'), encoding='utf-8')
    data = tmp_path/'data'
    (data/'config').mkdir(parents=True)
    (data/'config/runtime.env').write_text('# isolated lifecycle test\n',encoding='utf-8')
    (tmp_path/'active.json').write_text(json.dumps({'schema':'knowledgeradar-active-install/v1',
        'program_root':str(ROOT),'data_root':str(data),'version':'isolated-probe'}),encoding='utf-8')
    command = f'"{helper}" --supervise --runtime-only --role stable --port 18882 --no-open --install-root "{tmp_path}"'
    script = '\n'.join([
        "$ErrorActionPreference='Stop'",
        f"$action=New-ScheduledTaskAction -Execute '{console._ps_literal(pythonw)}' -Argument '{console._ps_literal(command)}'",
        '$owner=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name',
        '$settings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)',
        f"Register-ScheduledTask -TaskName '{task}' -Action $action -Settings $settings -User $owner -RunLevel Limited -Force | Out-Null",
    ])
    client = OwnerClient(root=data/'state/execution_owner',timeout=5)
    def wait_for(fn, predicate, seconds=40):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            value=fn()
            if predicate(value):return value
            time.sleep(.1)
        pytest.fail('isolated OS task timed out: '+str(value))
    try:
        console._run_task_script(script)
        starter = (
            f"import sys,runpy;sys.path.insert(0,{str(ROOT/'src')!r});"
            f"job=runpy.run_path({str(ROOT/'scripts/verify_codex_product.py')!r})['owned_probe_job'](allow_breakaway=False);"
            f"runpy.run_path({str(ROOT/'scripts/product_console_launcher.py')!r})['_run_task_script'](\"$ErrorActionPreference='Stop'; Start-ScheduledTask -TaskName '{task}'\")"
        )
        env=dict(os.environ,KR_TASK_TEST_MODE='1',KR_DISABLE_MODEL_PREWARM='1',KR_DESKTOP_NOTIFICATIONS_DISABLED='1')
        # 任务进程不继承此环境，因此bootstrap仍走真实生产装配；隔离目录无账号。
        started=subprocess.run([sys.executable,'-X','utf8','-c',starter],env=env,
                               capture_output=True,text=True,encoding='utf-8',timeout=15)
        assert started.returncode==0,started.stderr
        ready=wait_for(client.status,lambda value:value['status']=='ready')
        generation=ready['generation']
        assert ready['supervision']['state']=='ready'
        assert client.call('get_capabilities',{'summary':True})['tool_count']==27
        # 发起者Job早已关闭，内核/管理器仍为原代际。
        assert client.descriptor()['generation']==generation
    finally:
        if client.descriptor():
            assert client.stop()['status']=='stopping'
            wait_for(client.descriptor,lambda value:value is None)
        shutdown=data/'state/console_supervisor/stable/shutdown.request.json'
        shutdown.parent.mkdir(parents=True,exist_ok=True)
        shutdown.write_text('{}',encoding='utf-8')
        from runtime.process_identity import process_identity
        state=data/'state/console_supervisor/stable/status.json'
        if state.exists():
            manager=json.loads(state.read_text(encoding='utf-8')).get('supervisor_pid')
            if manager:
                wait_for(lambda:process_identity(manager),lambda value:not value)
        console._run_task_script(f"Unregister-ScheduledTask -TaskName '{task}' -Confirm:$false -ErrorAction SilentlyContinue")


if __name__ == '__main__':
    # 用户级任务注册是明确选择的宿主验证，不放进无账号/无系统写入的CI。
    import tempfile
    with tempfile.TemporaryDirectory(prefix='kr-user-task-probe-') as directory:
        probe_os_user_task_starts_kernel_outside_restrictive_stdio_job(Path(directory))
    print(json.dumps({'status':'PASS','probe':'user_session_task_outside_restrictive_job',
                      'temporary_task_removed':True,'formal_installation_modified':False}))
