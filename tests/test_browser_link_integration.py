"""Branch composition, HTTP-first admission, archive and installer contracts."""
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import time

import httpx
import pytest
from conftest import patch_native

ROOT=Path(__file__).resolve().parents[1]


def test_borrowed_browser_launches_once_and_reuses_shared_connection(monkeypatch, tmp_path):
    from runtime import browser_channel as channel
    calls = []
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path))
    monkeypatch.setattr(channel, '_daily_chrome_running', lambda: False)
    monkeypatch.setattr(channel, '_launch_daily_chrome', lambda: calls.append('launch'))
    connected = lambda: bool(calls)
    assert channel.ensure_borrowed_browser(connected)['ok']
    assert channel.ensure_borrowed_browser(connected)['ok']
    assert calls == ['launch']


def test_borrowed_browser_does_not_relaunch_running_or_just_closed_browser(monkeypatch, tmp_path):
    from runtime import browser_channel as channel
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path))
    calls = []
    monkeypatch.setattr(channel, '_daily_chrome_running', lambda: True)
    monkeypatch.setattr(channel, '_launch_daily_chrome', lambda: calls.append('launch'))
    assert not channel.ensure_borrowed_browser(lambda: False, timeout_s=0)['ok']
    assert not calls
    monkeypatch.setattr(channel, '_daily_chrome_running', lambda: False)
    assert not channel.ensure_borrowed_browser(lambda: False, timeout_s=0)['ok']
    assert not channel.ensure_borrowed_browser(lambda: False, timeout_s=0)['ok']
    assert calls == ['launch']


def test_borrowed_browser_cancelled_work_cannot_launch(monkeypatch, tmp_path):
    from runtime import browser_channel as channel, execution_context
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path))
    monkeypatch.setattr(channel, '_launch_daily_chrome', lambda: pytest.fail('cancelled work launched browser'))
    def cancelled():
        raise execution_context.ExecutionStopped('execution_superseded_or_cancelled')
    monkeypatch.setattr(execution_context, 'check_execution', cancelled)
    with pytest.raises(execution_context.ExecutionStopped):
        channel.ensure_borrowed_browser(lambda: False)


def test_successful_cold_task_does_not_block_the_next_explicit_task(monkeypatch, tmp_path):
    from runtime import browser_channel as channel
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path))
    monkeypatch.setattr(channel, '_daily_chrome_running', lambda: False)
    state = {'connected': False, 'launches': 0}
    def launch():
        state['launches'] += 1
        state['connected'] = True
    monkeypatch.setattr(channel, '_launch_daily_chrome', launch)
    assert channel.ensure_borrowed_browser(lambda: state['connected'])['ok']
    state['connected'] = False  # Previous task closed its last owned work window.
    assert channel.ensure_borrowed_browser(lambda: state['connected'])['ok']
    assert state['launches'] == 2


def test_daily_chrome_launch_preserves_default_profile_and_has_no_debugger(monkeypatch):
    from runtime import browser_channel as channel, executables, browser_bootstrap
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr(executables, 'resolve_managed_chrome', lambda: SimpleNamespace(path='chrome.exe'))
    monkeypatch.setattr(browser_bootstrap, 'start', lambda executable: calls.append(executable))
    channel._launch_daily_chrome()
    assert calls == ['chrome.exe']
    args=browser_bootstrap.chrome_command('chrome.exe','KR-startup-'+'a'*32)
    assert not any('user-data-dir' in arg or 'remote-debugging' in arg for arg in args)
    assert '--no-startup-window' not in args


def test_quiet_bootstrap_minimizes_without_activation_and_closes_only_its_window(monkeypatch,tmp_path):
    from runtime import browser_bootstrap as boot
    from types import SimpleNamespace
    marker='KR-startup-'+'a'*32
    own={'handle':11,'pid':77,'title':marker,'minimized':True}
    user={'handle':12,'pid':77,'title':'private user page','minimized':False}
    calls=[]
    class Windows:
        closed=False
        def list(self):return [user] if self.closed else [own,user]
        def foreground(self):return user['handle']
        def close_if_owned(self,identity,token):
            calls.append((identity,token));self.closed=True;return True
    launches=[]
    monkeypatch.setattr(boot.subprocess,'Popen',lambda args,**kw: launches.append((args,kw)) or SimpleNamespace(pid=77))
    result=boot.supervise('chrome.exe',marker,tmp_path/'receipt.json',windows=Windows())
    assert result['bootstrap_window_closed'] and result['stop_reason']=='work_window_available'
    assert calls==[(own,marker)]
    assert launches[0][1]['startupinfo'].wShowWindow==7
    assert not result['may_close_browser_process']


def test_quiet_bootstrap_relinquishes_when_user_takes_window(monkeypatch,tmp_path):
    from runtime import browser_bootstrap as boot
    from types import SimpleNamespace
    marker='KR-startup-'+'b'*32
    own={'handle':11,'pid':77,'title':marker,'minimized':True}
    class Windows:
        def list(self):return [own]
        def foreground(self):return 11
        def close_if_owned(self,*args):pytest.fail('must not close user-taken window')
    monkeypatch.setattr(boot.subprocess,'Popen',lambda *a,**kw:SimpleNamespace(pid=77))
    result=boot.supervise('chrome.exe',marker,tmp_path/'receipt.json',windows=Windows(),lifetime_s=0)
    assert result['owned_window_ever_foreground'] and not result['bootstrap_window_closed']
    assert result['stop_reason']=='foreground_at_cleanup_deadline'


def test_native_health_and_cancel_do_not_launch_browser(monkeypatch, tmp_path):
    from runtime import browser_channel as channel
    from runtime.browser_link import BrowserLink
    monkeypatch.setattr(channel, 'ensure_borrowed_browser', lambda *a, **kw: pytest.fail('read/control launched browser'))
    bridge = BrowserLink(state_file=tmp_path / 'absent-host.json')
    assert not bridge.health()['ok']
    assert not bridge.cancel('absent')['ok']


def test_native_command_prepares_browser_without_replaying_business(monkeypatch, tmp_path):
    from runtime import browser_channel as channel, browser_link
    state = tmp_path / 'host.json'
    state.write_text(json.dumps({'port': 12345, 'token': 'fixture', 'generation': 'g'}))
    calls = []
    monkeypatch.setattr(browser_link.BrowserLink, 'health', lambda self: {'ok': False})
    monkeypatch.setattr(channel, 'ensure_borrowed_browser', lambda check, **kw: calls.append('prepare') or {'ok': True})
    def response(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={'ok': True, 'generation': 'g', 'request_id': 'r', 'data': {}})
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        result = browser_link.BrowserLink(state_file=state, client=client).request('search', {'request_id': 'r'}, platform='boss')
    assert result['ok'] and calls == ['prepare', '/command']


def test_browser_start_time_counts_toward_command_deadline(monkeypatch, tmp_path):
    from runtime import browser_channel as channel, browser_link
    monkeypatch.setattr(browser_link.BrowserLink, 'health', lambda self: {'ok': False})
    clock = iter([10.0, 12.0])
    monkeypatch.setattr(browser_link.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(channel, 'ensure_borrowed_browser', lambda *a, **kw: {'ok': True})
    bridge = browser_link.BrowserLink(state_file=tmp_path / 'absent-host.json')
    result = bridge.request('detail', {}, platform='boss', timeout_s=1)
    assert result['failure_type'] == 'native_command_timeout' and result['request_executed'] is False


@pytest.fixture(autouse=True)
def isolated(monkeypatch,tmp_path):
    for key,suffix in [('KR_STATE_DIR','state'),('KR_TASK_DB_PATH','tasks.sqlite3'),('KR_RUNTIME_LEASE_DB_PATH','leases.sqlite3')]:
        monkeypatch.setenv(key,str(tmp_path/suffix))
    monkeypatch.delenv('KR_DURABLE_TASK_ID',raising=False)
    monkeypatch.delenv('KR_V2EX_TOKEN',raising=False)


def test_native_collection_releases_browser_before_evidence_and_preserves_raw(monkeypatch,tmp_path):
    from runtime import recruitment_governance as g
    from runtime.platform_execution import ensure_bindings,worker_detail
    from kr_core import DetailRequest,SearchRequest
    from kr_core.registry import registry
    from kr_core.evidence_store import EvidenceStore
    from generic_web import url_policy
    from runtime import opencli_bridge
    url='https://www.zhipin.com/job_detail/fixture.html?securityId=context'
    events=[]
    def native(**kw):
        assert g.get_runtime_lease_coordinator().resource_leases('recruitment_platform','boss')
        events.append(kw['operation'])
        return {'ok':True,'data':{'url':url,'title':'Python','company':'fixture','salary':'20-30K',
            'content':'岗位职责：负责后端开发，熟悉Python，具有开发经验。'*12,'content_source':'job_intro_primary'}} if kw['operation']=='detail' else {
                'ok':True,'data':{'query':'Python','items':[{'url':url,'title':'Python','salary':'20-30K'}]}}
    patch_native(monkeypatch,native)
    monkeypatch.setattr(url_policy,'validate_detail_target',lambda *a:None)
    monkeypatch.setattr(opencli_bridge,'invoke_page',lambda **kw:pytest.fail('retired BOSS wiring'))
    ensure_bindings()
    original=EvidenceStore.append_detail
    def archive(self,**kwargs):
        assert not g.get_runtime_lease_coordinator().resource_leases('recruitment_platform','boss')
        events.append('archive');return original(self,**kwargs)
    monkeypatch.setattr(EvidenceStore,'append_detail',archive)
    result=registry.get('BOSS直聘').search(SearchRequest(keyword='Python',platform='BOSS直聘',limit=1))
    assert result.items[0].url==url
    result=worker_detail(DetailRequest(url=url,platform='BOSS直聘'))
    assert result['content'] and result['evidence_assets'] and result['salary']=='20-30K'
    assert events==['search','detail','archive']


def test_liepin_typed_http_detail_without_any_browser(monkeypatch):
    from collectors.platform import liepin
    from generic_web import url_policy
    from runtime.browser_link import BrowserLink
    job={'@type':'JobPosting','title':'Python','description':'负责后端开发，熟悉Python设计，三年开发经验。'*8,
        'baseSalary':{'currency':'CNY','value':{'minValue':20000,'maxValue':30000,'unitText':'MONTH'}},'hiringOrganization':{'name':'fixture'}}
    html='<script type="application/ld+json">'+json.dumps({'@graph':[job]},ensure_ascii=False)+'</script>'
    monkeypatch.setattr(url_policy,'guarded_fetch',lambda url,**kw:httpx.Response(200,text=html,request=httpx.Request('GET',url)))
    monkeypatch.setattr(BrowserLink,'request',lambda *a,**kw:pytest.fail('HTTP must not need a browser'))
    result=liepin.detail('https://www.liepin.com/job/123.shtml')
    assert result['status']=='ok' and result['salary']=='20000-30000 CNY MONTH' and result['company']=='fixture'
    assert result['metadata']['external_reader'] is False


def test_liepin_http_429_stops_before_dynamic_fallback(monkeypatch):
    from collectors.platform import liepin
    from generic_web import url_policy
    from runtime.browser_link import BrowserLink
    monkeypatch.setattr(url_policy,'guarded_fetch',lambda url,**kw:httpx.Response(429,request=httpx.Request('GET',url)))
    monkeypatch.setattr(BrowserLink,'request',lambda *a,**kw:pytest.fail('no fallback past rate wall'))
    result=liepin.detail('https://www.liepin.com/job/123.shtml')
    assert result['failure_type']=='rate_limited'
    assert not liepin.admission()['allowed']


def test_http_is_not_held_by_unrelated_manual_login_wait(monkeypatch):
    from collectors.platform import liepin
    from runtime.state_file import write_state
    write_state(liepin._site().state_path(),{'expires_at':time.time()+300})
    monkeypatch.setattr(liepin,'_static_liepin_detail',lambda url:{'status':'ok','content':'HTTP body'})
    assert liepin.detail('https://www.liepin.com/job/123.shtml')['content']=='HTTP body'


def test_v2ex_429_is_shared_across_calls_and_never_retried(monkeypatch):
    from collectors.platform import v2ex
    calls=[]
    def response(request):calls.append(request.url);return httpx.Response(429,headers={'Retry-After':'120'})
    monkeypatch.setattr(v2ex,'_client',lambda:httpx.Client(transport=httpx.MockTransport(response)))
    first=v2ex.discover();second=v2ex.detail('https://www.v2ex.com/t/123')
    assert first['failure_type']==second['failure_type']=='rate_limited'
    assert second['request_executed'] is False and len(calls)==1 and second['retry_after_s']>=119


def test_v2ex_search_continuation_does_not_drop_rest_of_page(monkeypatch):
    from collectors.platform import v2ex
    html='<div id="TopicsNode">'+''.join(f'<div class="cell"><span class="item_title"><a href="/t/{i}">Python {i}</a></span></div>' for i in range(1,6))+'</div>'
    monkeypatch.setattr(v2ex,'_client',lambda:httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,text=html))))
    result=v2ex.discover(limit=2,pages=1)
    cursor=result['metadata']['pagination']['continuation']
    assert cursor=={'page':1,'offset':2}
    next_result=v2ex.discover(limit=3,pages=1,**cursor)
    assert [x['url'] for x in next_result['items']]==['https://www.v2ex.com/t/3','https://www.v2ex.com/t/4','https://www.v2ex.com/t/5']
    assert next_result['metadata']['pagination']['complete']


def installer():
    spec=spec_from_file_location('isolated_browser_link_install',ROOT/'scripts/browser_link_install.py')
    module=module_from_spec(spec);spec.loader.exec_module(module);return module


def test_install_plan_is_read_only_and_does_not_invent_store_id(tmp_path):
    module=installer();before=list(tmp_path.iterdir())
    plan=module.plan(ROOT,tmp_path/'install',tmp_path/'data')
    assert plan['status']=='ready' and plan['registration']
    assert plan['extension_ids']==['gjpefddhmkinebmgfblaidofllfckhbi']
    assert not plan['store_url'] and list(tmp_path.iterdir())==before


def test_install_registration_update_and_rollback_isolated(monkeypatch,tmp_path):
    module=installer();program=tmp_path/'package';program.mkdir();(program/'config').mkdir();(program/'browser_link/host').mkdir(parents=True)
    (program/'config/browser-link.json').write_text(json.dumps({'extension_ids':['a'*32],'protocol':1}))
    (program/'browser_link/host/BrowserLinkHost.cs').write_text('// fixture source')
    install=tmp_path/'install';data=tmp_path/'data';current=[None];writes=[]
    monkeypatch.setattr(module,'read_registration',lambda:current[0])
    monkeypatch.setattr(module,'write_registration',lambda value:(writes.append(value),current.__setitem__(0,value)))
    def compile(command,**kwargs):Path(next(x[5:] for x in command if x.startswith('/out:'))).write_bytes(b'fixture-host')
    monkeypatch.setattr(module.subprocess,'run',compile)
    result=module.apply(program,install,data)
    assert result['status']=='registered' and current[0]==result['manifest']
    manifest=json.loads(Path(result['manifest']).read_text());assert manifest['allowed_origins']==['chrome-extension://'+'a'*32+'/']
    assert Path(manifest['path']).suffix=='.exe'
    module.restore(result,install);assert current[0] is None
    assert not data.exists()


def test_foreign_registration_cannot_be_overwritten(monkeypatch,tmp_path):
    module=installer()
    monkeypatch.setattr(module,'plan',lambda *a:{'registration':True})
    monkeypatch.setattr(module,'read_registration',lambda:'C:/foreign-install/host.json')
    monkeypatch.setattr(module,'write_registration',lambda *a:pytest.fail('must not overwrite another installation'))
    assert module.apply(ROOT,tmp_path/'install',tmp_path/'data')['status']=='registration_conflict'


@pytest.fixture
def registered_installer(monkeypatch, tmp_path):
    module=installer(); install=tmp_path/'install'; data=tmp_path/'data'
    component=install/'components/browser_link/fixture';component.mkdir(parents=True)
    manifest=component/'host.json';host=component/'BrowserLinkHost.exe';host.write_bytes(b'fixture')
    settings=component/'host-settings.json';settings.write_text(json.dumps({'state_file':str(data/'host.json')}))
    manifest.write_text(json.dumps({'path':str(host),'allowed_origins':['old']}))
    current=[str(manifest)];calls=[]
    monkeypatch.setattr(module,'read_registration',lambda:current[0])
    monkeypatch.setattr(module,'write_registration',lambda value:current.__setitem__(0,value))
    monkeypatch.setattr(module,'_control',lambda state,op:calls.append(op) or {'ok':True,'pending':0,'connected':True})
    monkeypatch.setattr(module,'plan',lambda *args:{'registration':True,'host':str(host),'manifest':str(manifest),
        'extension_ids':['a'*32],'state_file':str(data/'new.json'),'source_sha256':'fixture'})
    return module,install,data,current,calls,manifest,settings


def test_upgrade_busy_resumes_without_registry_or_settings_change(registered_installer,monkeypatch):
    module,install,data,current,calls,manifest,settings=registered_installer
    before=(manifest.read_bytes(),settings.read_bytes())
    monkeypatch.setattr(module,'_control',lambda state,op:calls.append(op) or {'pending':1,'connected':True})
    with pytest.raises(RuntimeError,match='upgrade_wait'):
        module.apply(ROOT,install,data)
    assert calls==['drain','resume'] and current[0]==str(manifest)
    assert before==(manifest.read_bytes(),settings.read_bytes())


def test_failed_upgrade_restores_same_version_settings_and_resumes(registered_installer,monkeypatch):
    module,install,data,current,calls,manifest,settings=registered_installer
    before=(manifest.read_bytes(),settings.read_bytes());writes=[]
    def write(value):
        writes.append(value)
        if len(writes)==1:raise OSError('fixture registry failure')
        current[0]=value
    monkeypatch.setattr(module,'write_registration',write)
    with pytest.raises(OSError):module.apply(ROOT,install,data)
    assert before==(manifest.read_bytes(),settings.read_bytes())
    assert current[0]==str(manifest) and calls==['drain','resume']


def test_registry_unknown_result_is_read_back_before_rollback(registered_installer,monkeypatch,tmp_path):
    module,install,data,current,calls,manifest,settings=registered_installer
    before=settings.read_bytes()
    new_manifest=manifest.with_name('new-host.json')
    original=module.plan
    monkeypatch.setattr(module,'plan',lambda *a:{**original(*a),'manifest':str(new_manifest)})
    writes=[]
    def write(value):
        current[0]=value;writes.append(value)
        if len(writes)==1:raise OSError('committed then connection lost')
    monkeypatch.setattr(module,'write_registration',write)
    with pytest.raises(OSError):module.apply(ROOT,install,data)
    assert current[0]==str(manifest) and settings.read_bytes()==before
    assert not new_manifest.exists() and writes==[str(new_manifest),str(manifest)]


def test_restore_preserves_host_settings_edited_after_apply(registered_installer):
    module,install,data,current,calls,manifest,settings=registered_installer
    receipt=module.apply(ROOT,install,data);calls.clear()
    settings.write_text(json.dumps({'state_file':str(data/'human.json')}))
    with pytest.raises(RuntimeError,match='current_registration_modified_preserved'):module.restore(receipt,install)
    assert 'human.json' in settings.read_text() and not calls


@pytest.mark.parametrize('operation',['apply','restore'])
def test_host_human_edit_during_drain_is_preserved(registered_installer,monkeypatch,operation):
    module,install,data,current,calls,manifest,settings=registered_installer
    receipt=module.apply(ROOT,install,data) if operation=='restore' else None
    calls.clear()
    def control(state,action):
        calls.append(action)
        if action=='drain':settings.write_text(json.dumps({'state_file':str(data/'human-during-drain.json')}))
        return {'ok':True,'pending':0,'connected':True}
    monkeypatch.setattr(module,'_control',control)
    with pytest.raises(RuntimeError,match='changed_before'):
        module.restore(receipt,install) if operation=='restore' else module.apply(ROOT,install,data)
    assert 'human-during-drain.json' in settings.read_text()
    assert calls==['drain','resume'] and current[0]==str(manifest)


def test_external_handoff_rejects_changed_old_assets(registered_installer,monkeypatch,tmp_path):
    module,install,data,current,calls,manifest,settings=registered_installer
    old=tmp_path/'external';old.mkdir()
    old_host=old/'BrowserLinkHost.exe';old_host.write_bytes(b'old host')
    old_settings=old/'host-settings.json';old_settings.write_text(json.dumps({'state_file':str(data/'old.json')}))
    old_manifest=old/'host.json';old_manifest.write_text(json.dumps({'name':module.HOST,'type':'stdio','path':str(old_host)}))
    current[0]=str(old_manifest)
    plan=module.handoff_plan(install)
    old_host.write_bytes(b'updated old host')
    with pytest.raises(RuntimeError,match='handoff_plan_stale'):
        module.apply(ROOT,install,data,handoff_confirmation=plan['confirmation_token'])
    assert current[0]==str(old_manifest) and not calls
    plan=module.handoff_plan(install)
    receipt=module.apply(ROOT,install,data,handoff_confirmation=plan['confirmation_token'])
    old_settings.write_text(json.dumps({'state_file':str(data/'changed.json')}))
    with pytest.raises(RuntimeError,match='previous_registration_modified_preserved'):module.restore(receipt,install)
    assert current[0]==str(manifest) and 'changed.json' in old_settings.read_text()


def test_restore_drains_current_host_before_reverting_files(registered_installer):
    module,install,data,current,calls,manifest,settings=registered_installer
    before=(manifest.read_bytes(),settings.read_bytes())
    receipt=module.apply(ROOT,install,data);calls.clear()
    module.restore(receipt,install)
    assert calls==['drain','shutdown'] and current[0]==str(manifest)
    assert before==(manifest.read_bytes(),settings.read_bytes())


def test_failed_uninstall_resumes_and_keeps_registration(registered_installer,monkeypatch):
    module,install,data,current,calls,manifest,settings=registered_installer
    def write(value):
        if value is None:raise OSError('fixture registry failure')
        current[0]=value
    monkeypatch.setattr(module,'write_registration',write)
    with pytest.raises(OSError):module.uninstall(install)
    assert calls==['drain','resume'] and current[0]==str(manifest)


def test_durable_unsent_native_busy_defers_without_rate_charge(monkeypatch):
    from collectors.platform import liepin
    from runtime import recruitment_governance as g
    from runtime.resource_concurrency import ResourceDeferred
    from platform_adapters.liepin_adapter import LiepinAdapter
    resource_available = LiepinAdapter.resource_ready
    from runtime.browser_link import BrowserLink
    monkeypatch.setenv('KR_DURABLE_WORKER','1')
    patch_native(monkeypatch,lambda **kw:{'ok':False,'code':'browser_busy','request_executed':False})
    with pytest.raises(ResourceDeferred,match='browser_link:liepin'):
        liepin.legacy_search_liepin('Python')
    assert not g.get_runtime_lease_coordinator().resource_leases('recruitment_platform','liepin')
    assert g.check_search_gate('liepin')['searches_this_hour']==0
    monkeypatch.setattr(BrowserLink,'health',lambda self:{'connected':True,'draining':True,'pending':0})
    assert not resource_available('browser_link:liepin')
    monkeypatch.setattr(BrowserLink,'health',lambda self:{'connected':True,'pending':0})
    assert resource_available('browser_link:liepin')


def test_unknown_execution_is_never_automatically_deferred(monkeypatch):
    from platform_adapters.common import defer_browser_busy
    monkeypatch.setenv('KR_DURABLE_WORKER','1')
    assert defer_browser_busy('boss','browser_busy',request_executed=True) is None
    assert defer_browser_busy('boss','native_command_timeout',request_executed=False) is None


def test_rollback_to_package_without_connector_can_restore_on_failure(registered_installer):
    module,install,data,current,calls,manifest,settings=registered_installer
    receipt=module.uninstall(install)
    assert current[0] is None and calls==['drain','shutdown']
    module.restore(receipt,install)
    assert current[0]==str(manifest) and settings.is_file()


def test_busy_optional_comments_do_not_replay_or_discard_collected_body(monkeypatch):
    from collectors.platform import maimai
    from runtime.resource_concurrency import ResourceDeferred
    monkeypatch.setattr(maimai,'_detail',lambda url:{'status':'ok','content':'已采集正文'})
    def busy(*args):raise ResourceDeferred('browser_link:maimai')
    monkeypatch.setattr(maimai,'_comments',busy)
    result=maimai.discussion_detail('https://maimai.cn/community/feed-detail/12?efid=e',include_comments=True)
    assert result['status']=='ok' and result['content']=='已采集正文'
    assert result['comments_status']=='resource_busy' and result['comments_complete'] is False


@pytest.mark.parametrize('code,executed,status',[
    ('browser_busy',False,'queued'),('native_command_timeout',True,'failed')])
def test_native_control_outcome_reaches_durable_task_state(monkeypatch,tmp_path,code,executed,status):
    from runtime.tasks import TaskStore
    from runtime.task_process import execute_job
    from generic_web import url_policy
    monkeypatch.setenv('KR_DURABLE_WORKER','1')
    monkeypatch.delenv('KR_TASK_INPUT_STDIN',raising=False)
    monkeypatch.delenv('KR_TASK_STAGE',raising=False)
    task_id='native-outcome-'+code
    monkeypatch.setenv('KR_DURABLE_TASK_ID',task_id)
    store=TaskStore(str(tmp_path/'tasks.sqlite3'))
    store.upsert_task(task_id,'durable_content_detail','猎聘')
    job={'task_id':task_id,'attempt_id':'fixture','db_path':store.db_path,'operation':'content_detail','identity':{},
        'payload':{'url':'https://www.liepin.com/job/123.shtml','platform':'猎聘'}}
    path=tmp_path/'input.json';path.write_text(json.dumps(job),encoding='utf-8')
    patch_native(monkeypatch,lambda **kw:{'ok':False,'code':code,'request_executed':executed})
    monkeypatch.setattr('collectors.platform.liepin._static_liepin_detail', lambda url: None)
    monkeypatch.setattr(url_policy,'validate_detail_target',lambda *a:None)
    execute_job(path)
    task=store.get_task(task_id)
    assert task['status']==status
    if status=='queued':
        assert task['metadata']['waiting_resource']=='browser_link:liepin'
    else:
        assert task['error_code']=='request_outcome_unknown'


def test_reconnect_wait_covers_pinned_extension_backoff_without_relaunch(monkeypatch,tmp_path):
    from runtime import browser_channel as channel
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path))
    monkeypatch.setattr(channel,'_daily_chrome_running',lambda:True)
    monkeypatch.setattr(channel,'_launch_daily_chrome',lambda:pytest.fail('already running'))
    clock=[0.0]
    monkeypatch.setattr(time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(time,'sleep',lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    result=channel.ensure_borrowed_browser(lambda:clock[0]>=15.5,timeout_s=18)
    assert result['ok'] and not result['launched'] and clock[0]>=15.5


def test_routine_requests_cannot_inherit_visible_diagnostic_mode(monkeypatch, tmp_path):
    from runtime import browser_link
    monkeypatch.setenv('KR_BROWSER_LINK_OBSERVE', '1')
    monkeypatch.setattr(browser_link.BrowserLink, 'health', lambda self: {'ok': True})
    state=tmp_path/'host.json'
    state.write_text(json.dumps({'port':12345,'token':'fixture','generation':'g'}))
    observed=[]
    def respond(request):
        observed.append(json.loads(request.content))
        return httpx.Response(200,json={'ok':True,'generation':'g','request_id':'r','data':{}})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result=browser_link.BrowserLink(state_file=state,client=client).request('detail',{'request_id':'r','observe':True},platform='zhihu')
    assert result['ok']
    assert 'observe' not in observed[0]['payload']


def test_bootstrap_transient_activation_is_not_assumed_user_takeover(monkeypatch,tmp_path):
    from runtime import browser_bootstrap as boot
    from types import SimpleNamespace
    marker='KR-startup-'+'c'*32
    own={'handle':11,'pid':77,'title':marker,'minimized':True}
    other={'handle':12,'pid':77,'title':'other','minimized':True}
    class Windows:
        closed=False
        foreground_calls=0
        def list(self):return [other] if self.closed else [own,other]
        def foreground(self):
            self.foreground_calls+=1
            return 11 if self.foreground_calls==1 else 99
        def close_if_owned(self,identity,token):
            assert identity==own and token==marker
            self.closed=True;return True
    monkeypatch.setattr(boot.subprocess,'Popen',lambda *a,**kw:SimpleNamespace(pid=77))
    result=boot.supervise('chrome.exe',marker,tmp_path/'receipt.json',windows=Windows())
    assert result['owned_window_ever_foreground']
    assert result['bootstrap_window_closed']


@pytest.mark.parametrize('direction', ['', '\u200e', '\u200f'])
def test_official_window_helper_checks_marker_pid_and_title(direction):
    from runtime.browser_bootstrap import window_presentation
    import pytest
    marker = 'KR-work-' + 'a' * 32
    class WindowList:
        rows = [{'handle': 12, 'pid': 34, 'title': direction + marker + ' - Google Chrome'}]
        calls = []
        def list(self): return self.rows
        def present(self, identity, foreground): self.calls.append((identity, foreground))
    windows = WindowList()
    identity = window_presentation({'action': 'bind', 'marker': marker}, windows=windows)
    window_presentation({'action': 'present', 'identity': identity, 'title': marker, 'foreground': False}, windows=windows)
    assert windows.calls == [(identity, False)]
    windows.rows[0]['pid'] = 99
    with pytest.raises(ValueError, match='identity_changed'):
        window_presentation({'action': 'present', 'identity': identity, 'title': marker, 'foreground': False}, windows=windows)
    assert len(windows.calls) == 1


def test_unpublished_connector_fails_before_browser_or_http(monkeypatch):
    from runtime.browser_link import BrowserLink
    from runtime import browser_channel
    monkeypatch.setattr(browser_channel, 'ensure_borrowed_browser', lambda *a, **k: pytest.fail('must not launch browser'))
    bridge = BrowserLink()
    monkeypatch.setattr(bridge,'distribution_status',lambda:'browser_link_distribution_pending')
    monkeypatch.setattr(bridge, '_request', lambda *a, **k: pytest.fail('must not contact old host'))
    assert bridge.health()['code'] == 'browser_link_distribution_pending'
    result = bridge.request('search', {}, platform='zhihu')
    assert result['code'] == 'browser_link_distribution_pending'
    assert result['request_executed'] is False
