"""真实编译 Host + 隔离 Native peer；不注册、不触碰用户 Chrome 或账号。"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import queue
import re
import struct
import subprocess
import threading
import time
import uuid

import httpx
import pytest

ROOT=Path(__file__).resolve().parents[1]


def test_host_and_extension_operation_contracts_agree():
    host=(ROOT/'browser_link/host/BrowserLinkHost.cs').read_text(encoding='utf-8')
    extension=(ROOT/'browser_link/extension/background.js').read_text(encoding='utf-8')
    host_ops=set(re.findall(r'"([a-z_]+)"', re.search(r'Operations = new HashSet<string> \{([^}]+)\}',host)[1]))
    extension_ops=set(re.findall(r"'([a-z_]+)'", re.search(r'const OPS=new Set\(\[([^]]+)\]\)',extension)[1]))
    assert host_ops==extension_ops


def test_transcript_crosses_real_host_and_returns_browser_result(peer):
    payload={'url':'https://www.youtube.com/watch?v=sobPBztQTtU','language':'en'}
    with ThreadPoolExecutor() as pool:
        future=pool.submit(peer.command,'youtube-transcript','youtube',operation='transcript',payload=payload)
        command=peer.next()
        assert command['operation']=='transcript' and command['payload']==payload
        peer.result(command,data={'transcript':'fixture transcript','completeness':'unverified'},request_executed=True)
        response=future.result()
    assert response.status_code==200
    assert response.json()['data']['transcript']=='fixture transcript'
    assert response.json()['request_executed'] is True


def test_python_bridge_preserves_transcript_across_real_host(peer):
    from runtime.browser_link import BrowserLink
    bridge = BrowserLink(state_file=peer.path)
    with ThreadPoolExecutor() as pool:
        result = pool.submit(bridge._request, 'transcript',
            {'url':'https://www.youtube.com/watch?v=sobPBztQTtU','language':'en'}, platform='youtube')
        command = peer.next()
        assert command['operation'] == 'transcript'
        peer.result(command, data={'transcript':'controlled native peer body','completeness':'unverified'})
        value = result.result(timeout=10)
    assert value['ok'] and value['data']['transcript'] == 'controlled native peer body'
    assert value['request_id'] == command['request_id']


@pytest.mark.parametrize('observe,operation,expected', [('1','search',False), ('','search',False), ('1','probe',False)])
def test_routine_work_ignores_stale_observe_environment(monkeypatch,tmp_path,observe,operation,expected):
    from runtime.browser_link import BrowserLink
    monkeypatch.delenv('KR_DURABLE_TASK_ID',raising=False)
    monkeypatch.setenv('KR_BROWSER_LINK_OBSERVE',observe)
    state=tmp_path/'host.json';state.write_text(json.dumps({'port':12345,'token':'test','generation':'gen'}))
    observed=[]
    def respond(request):
        if request.url.path=='/health':return httpx.Response(200,json={'connected':True,'protocol':1,'generation':'gen'})
        body=json.loads(request.content);observed.append(body)
        return httpx.Response(200,json={'ok':True,'generation':'gen','request_id':body['request_id']})
    payload={'url':'https://www.zhihu.com/search?q=test'}
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert BrowserLink(state_file=state,client=client).request(operation,payload,platform='zhihu')['ok']
    assert observed[0]['payload'].get('observe',False) is expected
    assert 'observe' not in payload


@pytest.fixture(scope='module')
def executable(tmp_path_factory):
    compiler=Path(os.environ.get('WINDIR',r'C:\Windows'))/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    if not compiler.is_file():pytest.skip('Windows Framework compiler required')
    target=tmp_path_factory.mktemp('browser-link-host')/'BrowserLinkHost.exe'
    run=subprocess.run([str(compiler),'/nologo','/target:winexe','/reference:System.Web.Extensions.dll','/out:'+str(target),
        str(ROOT/'browser_link/host/BrowserLinkHost.cs')],capture_output=True,timeout=30)
    assert run.returncode==0,run.stderr.decode(errors='replace')+run.stdout.decode(errors='replace')
    return target


class Peer:
    def __init__(self,executable,path,*,scope=None,profile=None):
        self.path=path;self.scope=scope or uuid.uuid4().hex;self.profile=profile or str(uuid.uuid4());self.messages=queue.Queue()
        env={**os.environ,'KR_BROWSER_LINK_STATE_FILE':str(path),'KR_BROWSER_LINK_TEST_SCOPE':self.scope}
        self.process=subprocess.Popen([str(executable)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        def read():
            try:
                while True:
                    header=self.process.stdout.read(4)
                    if not header:return
                    body=self.process.stdout.read(struct.unpack('<I',header)[0]);self.messages.put(json.loads(body))
            finally:self.messages.put(None)
        threading.Thread(target=read,daemon=True).start()
    def send(self,value):
        body=json.dumps(value,ensure_ascii=False).encode();self.process.stdin.write(struct.pack('<I',len(body))+body);self.process.stdin.flush()
    def next(self):return self.messages.get(timeout=5)
    def hello(self):
        self.send({'type':'hello','protocol':1,'profile_id':self.profile,'extension_version':'0.2.0'})
        return self.next()
    def state(self):return json.loads(self.path.read_text(encoding='utf-8-sig'))
    def http(self,path,body=None):
        state=self.state()
        with httpx.Client(trust_env=False,timeout=8) as client:
            kw={'headers':{'X-KR-Native-Token':state['token']}}
            return client.get(f"http://127.0.0.1:{state['port']}/{path}",**kw) if body is None else client.post(f"http://127.0.0.1:{state['port']}/{path}",json=body,**kw)
    def command(self,id='one',platform='boss',**extra):
        return self.http('command',{'request_id':id,'platform':platform,'generation':self.state()['generation'],
            'operation':'detail','payload':{'url':'https://www.zhipin.com/job_detail/fixture.html'},'timeout_ms':3000,**extra})
    def result(self,command,**extra):
        self.send({'type':'result','request_id':command['request_id'],'platform':command['platform'],
            'generation':command['generation'],'ok':True,'data':{'content':'fixture'},**extra})
    def close(self):
        if self.process.poll() is None:
            try:self.process.stdin.close();self.process.wait(timeout=4)
            except (OSError,subprocess.TimeoutExpired):self.process.terminate();self.process.wait(timeout=3)
        for pipe in [self.process.stdout,self.process.stderr]:pipe.close()


@pytest.fixture
def peer(executable,tmp_path):
    item=Peer(executable,tmp_path/'host.json')
    assert item.hello()['type']=='hello_ack'
    try:yield item
    finally:item.close()


def test_host_negotiates_protocol_and_does_not_leak_token_in_health(peer):
    health=peer.http('health').json()
    assert health['connected'] and health['protocol']==1 and health['extension_version']=='0.2.0'
    assert 'token' not in health


def test_live_host_exposes_build_and_actual_extension_operations(peer, executable):
    import hashlib
    health = peer.http('health').json()
    assert health['host_build'] == hashlib.sha256(executable.read_bytes()).hexdigest()
    assert 'transcript' in health['host_operations']
    assert health['extension_contract'] == 0  # 旧扩展未声明能力，不能伪装已支持。
    assert health['extension_operations'] == []


def test_new_extension_missing_operation_is_rejected_before_dispatch(executable, tmp_path):
    item = Peer(executable, tmp_path/'host.json')
    try:
        item.send({'type':'hello','protocol':1,'profile_id':item.profile,'extension_version':'0.3.20',
                   'contract_version':1,'build_id':'fixture-build','operations':['detail']})
        assert item.next()['type'] == 'hello_ack'
        reply = item.command('missing-transcript','youtube',operation='transcript').json()
        assert reply['code'] == 'extension_operation_unavailable'
        assert reply['request_executed'] is False and item.messages.empty()
    finally:
        item.close()


def test_original_operation_receipt_is_read_only_and_does_not_replay(peer):
    with ThreadPoolExecutor() as pool:
        future=pool.submit(peer.command,'receipt-source','zhihu')
        command=peer.next()
        pending=peer.http('receipt',{'request_id':'receipt-source'}).json()
        assert pending['status']=='pending'
        peer.result(command)
        response=future.result().json()
    for _ in range(2):
        stored=peer.http('receipt',{'request_id':'receipt-source'}).json()
        assert stored['status']=='recorded' and stored['result']==response
    assert peer.messages.empty()
    assert peer.http('receipt',{'request_id':'never-sent'}).json()['status']=='unknown'


def test_receipt_survives_host_restart_without_replaying(executable,tmp_path):
    first=Peer(executable,tmp_path/'host.json')
    assert first.hello()['type']=='hello_ack'
    try:
        with ThreadPoolExecutor() as pool:
            future=pool.submit(first.command,'durable-original','zhihu')
            command=first.next();first.result(command)
            result=future.result().json()
        first.close()
        second=Peer(executable,first.path,scope=first.scope,profile=first.profile)
        try:
            assert second.hello()['type']=='hello_ack'
            stored=second.http('receipt',{'request_id':'durable-original'}).json()
            assert stored['status']=='recorded' and stored['result']==result
            assert stored['result']['generation'] != second.state()['generation']
            assert second.messages.empty()
            duplicate=second.command('durable-original','zhihu').json()
            assert duplicate['code']=='duplicate_request'
        finally:second.close()
    finally:first.close()


def test_receipt_transport_failure_is_not_cancel_failure(monkeypatch,tmp_path):
    from runtime.browser_link import BrowserLink
    state=tmp_path/'host.json';state.write_text(json.dumps({'port':12345,'token':'test','generation':'new'}))
    def respond(request):raise httpx.ReadTimeout('fixture')
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result=BrowserLink(state_file=state,client=client).receipt('old-operation')
    assert result['status']=='unknown'
    assert result['reason']=='receipt_transport_timeout'
    assert result['generation']=='new'


def test_receipt_disk_failure_after_execution_never_claims_not_executed(peer):
    with ThreadPoolExecutor() as pool:
        future=pool.submit(peer.command,'disk-failure','zhihu')
        command=peer.next()
        folder=peer.path.parent/'receipts'
        folder.rename(peer.path.parent/'saved-receipts')
        folder.write_text('fixture blocks receipt directory',encoding='utf-8')
        peer.result(command)
        result=future.result().json()
    assert result['ok'] is True
    assert result['receipt_persistence']=='failed'
    assert result.get('request_executed') is not False
    assert peer.http('receipt',{'request_id':'disk-failure'}).json()['result']==result
    assert peer.messages.empty()


def test_read_only_diagnostic_probe_uses_existing_host_protocol(peer):
    with ThreadPoolExecutor() as pool:
        future=pool.submit(peer.command,'diagnose','zhihu',operation='probe',payload={'diagnostics_only':True})
        command=peer.next()
        assert command['operation']=='probe' and command['payload']=={'diagnostics_only':True}
        peer.result(command,data={'schema':'kr-browser-diagnostics/v1','events':[],'tabs':[]},request_executed=False)
        response=future.result().json()
        assert response['data']['schema']=='kr-browser-diagnostics/v1' and response['request_executed'] is False


def test_single_owner_loser_cannot_overwrite_state(peer,executable):
    before=peer.path.read_bytes();loser=Peer(executable,peer.path,scope=peer.scope)
    try:
        assert loser.next()['code']=='channel_already_owned'
        loser.process.wait(timeout=3)
        assert peer.path.read_bytes()==before
        assert peer.http('health').json()['connected']
    finally:loser.close()


def test_second_profile_is_rejected_without_rebinding(executable,tmp_path):
    path=tmp_path/'host.json';first=Peer(executable,path)
    assert first.hello()['type']=='hello_ack';profile=first.profile;first.close()
    before=path.read_bytes();other=Peer(executable,path)
    try:
        assert other.hello()['code']=='paired_profile_mismatch';other.process.wait(timeout=3)
        assert path.read_bytes()==before
        assert json.loads(path.with_name('paired-profile.json').read_text())['profile_id']==profile
    finally:other.close()


def test_two_sites_multiplex_but_same_site_never_runs_twice(peer):
    with ThreadPoolExecutor(3) as pool:
        a=pool.submit(peer.command,'a','boss');ca=peer.next()
        b=pool.submit(peer.command,'b','maimai');cb=peer.next()
        assert {ca['platform'],cb['platform']}=={'boss','maimai'}
        blocked=peer.command('c','boss').json()
        assert blocked['code']=='browser_busy' and blocked['request_executed'] is False
        peer.result(cb);peer.result(ca)
        assert a.result().json()['ok'] and b.result().json()['ok']


def test_cancel_task_wins_over_late_result_and_prevents_replay(peer):
    with ThreadPoolExecutor() as pool:
        future=pool.submit(peer.command,'cancel-me','boss',payload={'task_id':'task'})
        command=peer.next();assert command['payload']['task_id']=='task'
        assert peer.http('cancel_task',{'task_id':'task'}).json()['cancelled']==1
        assert peer.next()['type']=='cancel'
        peer.result(command)
        assert future.result().json()['code']=='cancelled'
        assert peer.command('cancel-me').json()['code']=='duplicate_request'
        assert peer.command('new','boss',payload={'task_id':'task'}).json()['request_executed'] is False


def test_generation_and_platform_mismatch_cannot_complete_request(peer):
    with ThreadPoolExecutor() as pool:
        future=pool.submit(peer.command)
        command=peer.next();peer.result(command,generation='old');peer.result(command,platform='maimai')
        assert peer.http('health').json()['pending']==1
        peer.result(command);assert future.result().json()['ok']
    assert peer.command('stale',generation='stale').json()['code']=='stale_generation'


def test_eof_fails_pending_without_hanging(peer):
    with ThreadPoolExecutor() as pool:
        future=pool.submit(peer.command);peer.next();peer.process.stdin.close()
        assert future.result().json()['code']=='browser_disconnected'
        peer.process.wait(timeout=4)


def test_deadline_cancels_content_once(peer):
    with ThreadPoolExecutor() as pool:
        future=pool.submit(peer.command,timeout_ms=80)
        peer.next();assert future.result().json()['code']=='native_command_timeout'
        assert peer.next()['type']=='cancel'
        assert peer.http('health').json()['pending']==0


def test_update_drain_rejects_new_work_finishes_old_then_exits(peer):
    with ThreadPoolExecutor() as pool:
        # 此例验证排空，不验证超时；Windows新建HTTP/TLS上下文可能耗尽默认3秒。
        # 超时与cancel另由test_deadline_cancels_content_once覆盖。
        future=pool.submit(peer.command,timeout_ms=10000);command=peer.next()
        assert peer.http('drain',{}).json()['pending']==1
        assert peer.next()['type']=='drain'
        assert peer.command('new').json()['code']=='channel_draining'
        assert peer.http('shutdown',{}).status_code==409
        peer.result(command);assert future.result().json()['ok']
        assert peer.http('shutdown',{}).json()['ok'];peer.process.wait(timeout=4)


def test_cancel_before_send_and_operation_allowlist(peer):
    peer.http('cancel',{'request_id':'never-send'})
    assert peer.command('never-send').json()['request_executed'] is False
    assert peer.command('script',operation='evaluate').json()['code']=='unsupported_operation'


def test_protocol_rejected_before_state_publication(executable,tmp_path):
    item=Peer(executable,tmp_path/'host.json')
    try:
        item.send({'type':'hello','protocol':9,'profile_id':item.profile})
        assert item.next()['code']=='protocol_mismatch';item.process.wait(timeout=3)
        assert not item.path.exists()
    finally:item.close()
