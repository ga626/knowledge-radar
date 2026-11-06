"""入口中立合同、单owner、断连/崩溃恢复和薄CLI的真实隔离进程验证。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import httpx
import pytest

from runtime.execution_owner import OwnerClient, source_identity
from kr_core.evidence_assets import EvidenceAssets

ROOT = Path(__file__).resolve().parents[1]


def until(function, predicate, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = function()
        if predicate(value):
            return value
        time.sleep(.04)
    pytest.fail(f'isolated runtime timed out: {value}')


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    variables = {'KR_DATA_ROOT': tmp_path/'data', 'KR_STATE_DIR': tmp_path/'state',
                 'KR_LOG_DIR': tmp_path/'logs', 'KR_EVIDENCE_ROOT': tmp_path/'assets',
                 'KR_RUNTIME_LEASE_DB_PATH': tmp_path/'leases.sqlite3',
                 'KR_OWNER_ROOT': tmp_path/'state'/'execution_owner',
                 'KR_RUNTIME_ENV_PATH': tmp_path/'absent.env',
                 'KR_PROFILE_REGISTRY_PATH': tmp_path/'profiles.json'}
    for key, value in variables.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.setenv('KR_TASK_TEST_MODE', '1')
    monkeypatch.delenv('KR_ACTIVE_INSTALL_ROOT', raising=False)
    monkeypatch.delenv('KR_RUNTIME_ROLE', raising=False)
    monkeypatch.setenv('KR_DESKTOP_NOTIFICATIONS_DISABLED', '1')
    monkeypatch.setenv('KR_DISABLE_MODEL_PREWARM', '1')
    monkeypatch.setenv('PYTHONPATH', str(ROOT/'src'))
    children = []
    def launch(*, fixture=True):
        script = f"import sys,runpy; sys.path.insert(0,{str(ROOT/'src')!r}); "
        script += "from runtime import env_loader; env_loader.load_runtime_env=lambda:''; "
        script += f"runpy.run_path({str(ROOT/'tests/runtime_owner_fixture.py')!r},run_name='__main__')" if fixture else "runpy.run_module('runtime.execution_owner',run_name='__main__')"
        log = (tmp_path/f'owner-{len(children)}.log').open('ab')
        child = subprocess.Popen([sys.executable, '-X', 'utf8', '-c', script], cwd=ROOT,
                                 env=dict(os.environ), stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=log)
        log.close()
        children.append(child)
        return child
    client = OwnerClient(root=variables['KR_OWNER_ROOT'], timeout=5)
    yield tmp_path, client, launch
    for child in children:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=10)


@pytest.fixture
def running(isolated):
    path, client, launch = isolated
    child = launch()
    until(client.status, lambda x: x['status'] == 'ready')
    return path, client, child, launch


def terminal(client, task_id):
    return until(lambda: client.call('get_task_status', {'task_id': task_id}),
                 lambda x: x.get('status') not in {'queued', 'running'})


def test_status_and_receipt_do_not_start_or_create(isolated):
    _, client, _ = isolated
    assert client.status()['status'] == 'unavailable'
    assert client.receipt('missing')['status'] == 'error'
    assert client.receipts()['status'] == 'error'
    assert not client.root.exists()


def test_disconnect_reconcile_single_execution_and_busy_stop(running):
    path, client, _, _ = running
    impatient = OwnerClient(root=client.root, timeout=.1)
    arguments = {'value': 'once', 'wait_for_release': True}
    result = impatient.call('fixture_effect', arguments, request_id='same')
    assert result['error']['execution_state'] != 'not_started'
    assert client.stop()['status'] == 'busy'
    assert not (path/'state/effects.txt').exists()
    (path/'state/effect-release').write_text('release', encoding='utf-8')
    receipt = until(lambda: client.receipt('same'), lambda x: x.get('status') == 'completed')
    assert receipt['result'] == {'value': 'once'}
    assert client.receipts(1)['requests'][0]['request_id'] == 'same'
    assert client.call('fixture_effect', arguments, request_id='same') == {'value': 'once'}
    conflict = client.call('fixture_effect', {'value': 'different'}, request_id='same')
    assert conflict['status'] == 'error'
    assert (path/'state/effects.txt').read_text(encoding='utf-8') == 'once\n'
    with ThreadPoolExecutor(2) as pool:
        statuses = list(pool.map(lambda _: OwnerClient(root=client.root).start(), range(2)))
    assert len({s['generation'] for s in statuses}) == 1
    assert client.stop()['status'] == 'stopping'
    until(client.descriptor, lambda x: x is None)


def test_client_process_exit_task_keeps_running_cancel_deadline(running):
    _, client, _, _ = running
    code = f"import sys; sys.path.insert(0,{str(ROOT/'src')!r}); import json; from runtime.execution_owner import OwnerClient; print(json.dumps(OwnerClient().call('fixture_submit', {{'sleep':1.2}})))"
    done = subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=dict(os.environ), capture_output=True, text=True, encoding='utf-8', timeout=15)
    assert done.returncode == 0, done.stderr
    task = json.loads(done.stdout)
    assert terminal(client, task['task_id'])['status'] == 'completed'
    task = client.call('fixture_submit', {'sleep': 3})
    client.call('control_task', {'task_id': task['task_id'], 'action': 'cancel'})
    assert terminal(client, task['task_id'])['status'] == 'cancelled'
    task = client.call('fixture_submit', {'sleep': 2, 'timeout_s': .3})
    assert terminal(client, task['task_id'])['status'] == 'failed'
    quick = client.call('fixture_submit', {'sleep': 0})
    assert terminal(client, quick['task_id'])['status'] == 'completed'


def test_crashed_owner_never_replays_unknown_work_and_explicit_resume(running):
    path, client, child, launch = running
    # worker独立于owner；必须确保恢复前它尚未完成，否则completed本来就是合法终态。
    slow = client.call('fixture_submit', {'sleep': 30, 'timeout_s': 90})
    until(lambda: client.call('get_task_status', {'task_id': slow['task_id']}), lambda x: x.get('status') == 'running')
    impatient = OwnerClient(root=client.root, timeout=.1)
    impatient.call('fixture_effect', {'sleep': 4}, request_id='crash')
    old = client.descriptor()['generation']
    child.kill()
    child.wait(timeout=10)
    launch()
    until(client.status, lambda x: x.get('status') == 'ready' and x.get('generation') != old)
    assert client.receipt('crash')['status'] == 'interrupted'
    assert client.call('fixture_effect', {'sleep': 4}, request_id='crash')['status'] == 'error'
    assert not (path/'state/effects.txt').exists()
    assert client.call('get_task_status', {'task_id': slow['task_id']})['status'] == 'interrupted'
    retry = client.call('control_task', {'task_id': slow['task_id'], 'action': 'resume'})
    assert retry['task_id'] != slow['task_id']
    result = until(lambda: client.call('get_task_status', {'task_id': retry['task_id']}),
                   lambda x: x.get('status') not in {'queued', 'running'}, timeout=45)
    assert result['status'] == 'completed'
    folder = path/'logs/task_artifacts'
    old_job = json.loads((folder/slow['task_id']/'input.json').read_text(encoding='utf-8'))
    new_job = json.loads((folder/retry['task_id']/'input.json').read_text(encoding='utf-8'))
    assert old_job['billing_scope_id'] == new_job['billing_scope_id']


def test_authentication_generation_and_contract_validation(running):
    _, client, _, _ = running
    descriptor = client.descriptor()
    address = f"http://127.0.0.1:{descriptor['port']}/status"
    with httpx.Client(trust_env=False) as http:
        assert http.post(address, json={}).status_code == 403
        response = http.post(address, headers={'Authorization': 'Bearer '+descriptor['token']},
                             json={'generation':'stale','source_identity':source_identity(),'protocol':1})
    assert response.json()['status'] == 'error'
    error = client.call('fixture_submit', {'unknown': True})
    assert error['error']['execution_state'] == 'not_started'
    assert client.receipt('not-run')['status'] == 'not_found'


def test_install_maintenance_preserves_busy_work_and_stops_idle(running):
    path, client, _, _ = running
    from scripts.product_cli_launcher import maintenance
    active = {'data_root':str(path)}
    # 实际owner的数据根在临时目录，安装交接只改测试描述。
    task = client.call('fixture_submit', {'sleep':1})
    with pytest.raises(RuntimeError, match='busy'):
        with maintenance(active, package_root=ROOT):
            pytest.fail('must not switch while work is accepted')
    assert terminal(client, task['task_id'])['status'] == 'completed'
    with maintenance(active, package_root=ROOT):
        assert client.descriptor() is None


def test_installed_mcp_and_cli_use_same_environment_and_current_active(tmp_path, monkeypatch):
    from scripts import product_install
    from scripts import product_cli_launcher as launcher
    root = tmp_path/'install'
    program = root/'app/candidate'
    (program/'src/runtime').mkdir(parents=True)
    (program/'src/runtime/cli.py').write_text('# fixture', encoding='utf-8')
    active = {'program_root':str(program),'data_root':str(tmp_path/'data'),'version':'v1','installation_id':'candidate'}
    runtime = root/'runtime/candidate'/('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b'fixture')
    (root/'active.json').write_text(json.dumps(active), encoding='utf-8')
    import tomllib
    env = tomllib.loads(product_install.active_mcp_block(active,runtime))['mcp_servers']['knowledgeradar']['env']
    assert env == launcher.active_environment(active)
    from scripts.browser_link_install import plan
    production = plan(ROOT, root, Path(active['data_root']))
    assert Path(env['KR_BROWSER_LINK_STATE_FILE']) == Path(production['state_file'])
    captured=[]
    from types import SimpleNamespace
    def run(command, **kwargs):
        captured.append((command,kwargs))
        return SimpleNamespace(returncode=7)
    monkeypatch.setattr(launcher.subprocess,'run',run)
    assert launcher.main(['runtime','status'],install_root=root) == 7
    assert captured[-1][0][0] == str(runtime)
    assert captured[-1][1]['env']['KR_STATE_DIR'] == env['KR_STATE_DIR']
    active['installation_id'] = 'next'
    following = root/'runtime/next'/runtime.relative_to(root/'runtime/candidate')
    following.parent.mkdir(parents=True)
    following.write_bytes(b'fixture')
    (root/'active.json').write_text(json.dumps(active),encoding='utf-8')
    assert launcher.main(['runtime','status'],install_root=root) == 7
    assert captured[-1][0][0] == str(following)


def test_old_installation_cannot_restart_after_active_changes(isolated, monkeypatch):
    path, client, _ = isolated
    root = path/'install'
    root.mkdir()
    (root/'active.json').write_text(json.dumps({'program_root':str(path/'old')}),encoding='utf-8')
    monkeypatch.setenv('KR_ACTIVE_INSTALL_ROOT',str(root))
    assert client.start()['status'] == 'incompatible'
    assert client.call('fixture_submit',{})['error']['execution_state'] == 'not_started'
    assert client.descriptor() is None


def test_explicit_runtime_env_never_falls_back_to_source(monkeypatch, tmp_path):
    from runtime import env_loader
    monkeypatch.setenv('KR_RUNTIME_ENV_PATH', str(tmp_path/'absent.env'))
    inspected=[]
    monkeypatch.setattr(env_loader.os.path, 'isfile', lambda path: inspected.append(path) or False)
    assert env_loader.load_runtime_env() == ''
    assert inspected == [str(tmp_path/'absent.env')]


def test_cold_competing_clients_start_exactly_one_owner(isolated, monkeypatch):
    _, client, _ = isolated
    import runtime.execution_owner as module
    original = subprocess.Popen
    spawned=[]
    def tracked(*args, **kwargs):
        child = original(*args, **kwargs)
        spawned.append(child)
        return child
    monkeypatch.setattr(module.subprocess, 'Popen', tracked)
    try:
        with ThreadPoolExecutor(2) as pool:
            values = list(pool.map(lambda _: OwnerClient(root=client.root).start(), range(2)))
        assert len(spawned) == 1
        assert len({value['generation'] for value in values}) == 1
        assert client.stop()['status'] == 'stopping'
        spawned[0].wait(timeout=10)
    finally:
        for child in spawned:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=10)


def test_cli_invalid_arguments_are_structured_and_discovery_is_pure(isolated, capsys):
    _, client, _ = isolated
    from runtime.cli import main
    assert main(['knowledge','not-a-command']) == 2
    error = json.loads(capsys.readouterr().out)
    assert error['error']['code'] == 'invalid_cli_arguments' and error['error']['usage']
    assert main(['capabilities','--tool','read_knowledge']) == 0
    card = json.loads(capsys.readouterr().out)
    assert card['tool']['input_schema']['properties']['request']
    assert not client.root.exists()


def test_cli_fixed_evidence_batch_comparison_and_no_overwrite(running):
    path, client, _, _ = running
    assets = EvidenceAssets(path/'assets')
    left = assets.put('https://example.org/one', '中文 English 20K\n😀', title='研究')
    right = assets.put('https://example.org/one', '中文 English 25K\n😀', title='研究')
    requests = [{'action':'fetch','version_id':item['version_id']} for item in (left, right)]
    source = path/'in.json'
    source.write_text(json.dumps(requests), encoding='utf-8')
    output = path/'out.json'
    def cli(*args):
        code = f"import sys,runpy; sys.path.insert(0,{str(ROOT/'src')!r}); runpy.run_module('runtime.cli',run_name='__main__')"
        return subprocess.run([sys.executable,'-X','utf8','-c',code,*args],cwd=ROOT,env=dict(os.environ),capture_output=True,text=True,encoding='utf-8',timeout=15)
    result = cli('knowledge','batch','--input',str(source),'--output',str(output))
    assert result.returncode == 0, result.stdout+result.stderr
    saved = json.loads(output.read_text(encoding='utf-8'))
    expected = [client.call('read_knowledge', {'request':req}) for req in requests]
    assert saved['results'] == expected
    output.write_text('USER EDIT', encoding='utf-8')
    assert cli('knowledge','batch','--input',str(source),'--output',str(output)).returncode != 0
    assert output.read_text(encoding='utf-8') == 'USER EDIT'
    diff = path/'diff.json'
    assert cli('knowledge','compare',left['version_id'],right['version_id'],'--output',str(diff)).returncode == 0
    value = json.loads(diff.read_text(encoding='utf-8'))
    assert value['coverage'] == 'full' and value['left'] == expected[0]['citation']
    assert any('25K' in line for line in value['diff'])
    huge = assets.put('https://example.org/huge', '大量原文 abc\n'*1000)
    partial = path/'partial.json'
    assert cli('knowledge','compare',left['version_id'],huge['version_id'],'--max-chars','2000','--output',str(partial)).returncode == 0
    assert json.loads(partial.read_text(encoding='utf-8'))['coverage'] == 'partial'


def test_real_stdio_mcp_is_thin_and_owner_survives_session(isolated):
    path, client, launch = isolated
    assets = EvidenceAssets(path/'assets')
    item = assets.put('https://example.org/stdio', '原生协议 中文 English', title='固定证据')
    from runtime.research_ledger import open_task
    research_id = open_task(objective='protocol research',budget='balanced',considered=[])['research_task_id']
    owner = launch(fixture=False)
    until(client.status, lambda x: x.get('status') == 'ready', timeout=30)
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    async def scenario():
        params = StdioServerParameters(command=sys.executable,args=['-X','utf8',str(ROOT/'src/server.py')],cwd=str(ROOT),env=dict(os.environ,KR_MCP_TRANSPORT='stdio'))
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader,writer) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert len({t.name for t in tools.tools}) == len(tools.tools)
                from runtime.knowledge import Fetch
                good = await session.call_tool('read_knowledge', {'request':Fetch(action='fetch',version_id=item['version_id']).model_dump()})
                assert not good.isError
                value = good.structuredContent
                value = value.get('result', value)
                assert value['text'] == '原生协议 中文 English'
                assert good.meta['kr_execution_receipt']['request_id']
                question = await session.call_tool('update_research', {'request':{'action':'question','task_id':research_id,'text':'证据缺什么？'}})
                assert not question.isError
                question_value = question.structuredContent.get('result',question.structuredContent)
                assert question_value['status']=='recorded'
                feedback = await session.call_tool('read_knowledge', {'request':{'action':'research','task_id':research_id,'view':'feedback'}})
                feedback_value = feedback.structuredContent.get('result',feedback.structuredContent)
                assert question_value['record_id'] in feedback_value['questions_without_valid_links']
                assert feedback_value['stop_decision']=='agent_owned'

                destination = path / 'vault'
                destination.mkdir()
                arguments = {'request': {'action': 'authorize_destination', 'approved_root': str(destination), 'project_id': 'stage12'},
                             'request_id': 'same-cross-entry'}
                granted = await session.call_tool('manage_research_artifacts', arguments)
                assert not granted.isError
                assert granted.meta['kr_execution_receipt']['request_id'] == 'same-cross-entry'
                # CLI与MCP同一动作号，真正回读同一授权，不重复授予。
                from runtime.cli import execute, parser
                source = path / 'authorize.json'
                source.write_text(json.dumps({'request': arguments['request']}), encoding='utf-8')
                repeated = execute(parser().parse_args(['invoke', '--tool', 'manage_research_artifacts',
                    '--input', str(source), '--request-id', 'same-cross-entry']), client)
                assert repeated['execution_receipt']['reused']
                original = granted.structuredContent
                original = original.get('result', original)
                assert repeated['result']['grant_id'] == original['grant_id']
                lookup = await session.call_tool('get_task_status', {'receipt_request_id': 'same-cross-entry'})
                receipt = lookup.structuredContent
                receipt = receipt.get('result', receipt)
                assert receipt['status'] == 'completed'
                revoked = client.call('manage_research_artifacts', {'request': {'action': 'revoke_destination',
                    'grant_id': original['grant_id']}}, request_id='revoke-cross-entry')
                assert revoked['status'] == 'revoked'
                await session.call_tool('manage_research_artifacts', arguments)  # 重放旧回执不能重新授权。
                blocked = await session.call_tool('manage_research_artifacts', {'request': {'action': 'deliver',
                    'grant_id': original['grant_id'], 'project_id': 'stage12', 'task_id': 'task',
                    'version_ids': [item['version_id']]}, 'request_id': 'after-revocation'})
                assert blocked.isError and 'destination_not_authorized' in str(blocked.structuredContent)
                bad = await session.call_tool('read_knowledge', {'request':{'action':'wrong'}})
                assert bad.isError
                assert bad.structuredContent['error']['execution_state'] == 'not_started'
                assert bad.structuredContent['error']['fields']
    asyncio.run(scenario())
    assert owner.poll() is None
    assert client.status()['pid'] == owner.pid


def test_request_identity_reaches_job_artifact_and_spans(running):
    path, client, _, _ = running
    task = client.call('fixture_submit', {}, request_id='origin-lifecycle')
    result = terminal(client, task['task_id'])
    assert result['status'] == 'completed'
    folder = path / 'logs/task_artifacts' / task['task_id']
    assert json.loads((folder / 'input.json').read_text(encoding='utf-8'))['origin_request_id'] == 'origin-lifecycle'
    artifact = json.loads(next(folder.glob('result-*.json')).read_text(encoding='utf-8'))
    assert artifact['origin_request_id'] == 'origin-lifecycle'
    assert all(json.loads(row)['origin_request_id'] == 'origin-lifecycle'
               for row in (folder / 'spans.jsonl').read_text(encoding='utf-8').splitlines())


def test_cli_partial_batch_retry_and_interruption(tmp_path, monkeypatch):
    from runtime import cli
    source = tmp_path / 'in.json'
    source.write_text(json.dumps([{'action': 'search', 'query': str(i)} for i in range(3)]), encoding='utf-8')
    class Client:
        last_receipt = None
        def __init__(self): self.calls = []
        def call(self, name, arguments):
            query = arguments['request']['query']
            self.calls.append(query)
            self.last_receipt = {'request_id': query}
            if query == '1': raise KeyboardInterrupt()
            return {'items': [query]}
    client = Client()
    output = tmp_path / 'first.json'
    args = cli.parser().parse_args(['knowledge', 'batch', '--input', str(source), '--output', str(output)])
    result = cli.execute(args, client)
    assert result['batch_status'] == 'partial' and client.calls == ['0', '1']
    saved = json.loads(output.read_text(encoding='utf-8'))
    assert saved['results'][0] == {'items': ['0']} and saved['failed_indices'] == [1, 2]
    calls = []
    monkeypatch.setattr(client, 'call', lambda name, arguments: calls.append(arguments['request']['query']) or {'items': []})
    next_output = tmp_path / 'retry.json'
    args.output, args.retry_from = str(next_output), str(output)
    assert cli.execute(args, client)['batch_status'] == 'ok'
    assert calls == ['1', '2']
    retry = json.loads(next_output.read_text(encoding='utf-8'))
    assert retry['outcomes'][0]['observation'] == 'previous_run_not_revalidated'
    saved['failed_indices'] = []
    output.write_text(json.dumps(saved), encoding='utf-8')
    args.output = str(tmp_path / 'invalid.json')
    with pytest.raises(ValueError, match='identity_mismatch'):
        cli.execute(args, client)


def test_cli_atomic_output_rejects_competing_writer_and_cleans_failed_publish(tmp_path, monkeypatch):
    from runtime import cli
    from concurrent.futures import ThreadPoolExecutor
    target = tmp_path / 'out.json'
    def write(i):
        try: return cli._write_output(target, {'writer': i})
        except FileExistsError: return None
    with ThreadPoolExecutor(2) as pool:
        replies = list(pool.map(write, range(2)))
    assert sum(item is not None for item in replies) == 1
    assert json.loads(target.read_text(encoding='utf-8'))['writer'] in {0, 1}
    assert not list(tmp_path.glob('*.pending'))
    if os.name == 'nt':
        monkeypatch.setattr(cli.os, 'rename', lambda *a: (_ for _ in ()).throw(OSError('disk publication failed')))
    else:
        monkeypatch.setattr(cli.os, 'link', lambda *a: (_ for _ in ()).throw(OSError('disk publication failed')))
    with pytest.raises(OSError): cli._write_output(tmp_path / 'never.json', {'large': '字' * 5000})
    assert not (tmp_path / 'never.json').exists() and not list(tmp_path.glob('*.pending'))


def test_owner_token_directory_is_private(tmp_path):
    from runtime.execution_owner import private_owner_directory
    root = tmp_path / 'owner'
    private_owner_directory(root)
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        adv = ctypes.WinDLL('advapi32', use_last_error=True)
        adv.GetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p,
                                        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p, wintypes.DWORD,
            wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD)]
        raw, size = ctypes.create_string_buffer(4096), wintypes.DWORD()
        assert adv.GetFileSecurityW(str(root), 4, raw, 4096, ctypes.byref(size))
        sddl = wintypes.LPWSTR()
        assert adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(raw, 1, 4, ctypes.byref(sddl), None)
        try:
            assert sddl.value.startswith('D:P') and sddl.value.count('(A;') == 2
            assert ';SY)' in sddl.value
        finally:
            kernel = ctypes.WinDLL('kernel32')
            kernel.LocalFree.argtypes = [ctypes.c_void_p]
            kernel.LocalFree(ctypes.cast(sddl, ctypes.c_void_p))
    else:
        assert root.stat().st_mode & 0o777 == 0o700
