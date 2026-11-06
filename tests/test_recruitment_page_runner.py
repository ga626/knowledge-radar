"""执行实际公共runner；用固定桥接替身验证不会重复导航或越过风险。"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[1] / 'src/runtime/opencli_page_runner.mjs'


@pytest.fixture
def bridge_fixture(tmp_path, monkeypatch):
    if not shutil.which('node'):
        pytest.skip('node unavailable')
    log = tmp_path / 'calls.jsonl'
    monkeypatch.setenv('KR_RUNNER_FIXTURE_LOG', str(log))
    (tmp_path / 'package.json').write_text('{"type":"module"}', encoding='utf8')
    sources = {
        'daemon-transport.js': "export async function getDaemonHealth(){return {state:'ready',status:{daemonVersion:'2.0.2',extensionVersion:'1.0.26'}};}",
        'bridge-readiness.js': 'export async function waitForBridgeReady(fetch,opts){return fetch(opts);}',
        'daemon-client.js': """
import {appendFileSync} from 'node:fs';
export async function sendCommand(action,opts){
 appendFileSync(process.env.KR_RUNNER_FIXTURE_LOG,JSON.stringify({kind:'native',action,...opts})+'\\n');
 return opts.chromeMethod==='tabs.query'?[{id:10,title:globalThis.fixtureTitle||'Research',url:'https://example.test/login',windowId:1}]:{state:'normal',focused:true};
}
export async function sendCommandFull(action,opts){
 appendFileSync(process.env.KR_RUNNER_FIXTURE_LOG,JSON.stringify({kind:action==='navigate'?'goto':'native',action,...opts})+'\\n');
 if(process.env.KR_RUNNER_FIXTURE_MODE==='stale-navigation')throw new Error('Page not found: gone — stale page identity');
 if(process.env.KR_RUNNER_FIXTURE_MODE==='risk-navigation')throw Object.assign(new Error('blocked'),{code:'SECURITY_BLOCK'});
 globalThis.fixtureNavigate?.();
 return {ok:true,page:opts.page,data:{url:opts.url}};
}
export const generateRunId=()=> crypto.randomUUID();
export const setDaemonRunContext=()=> {};
export async function releaseSiteSessionLease(){appendFileSync(process.env.KR_RUNNER_FIXTURE_LOG,JSON.stringify({kind:'release'})+'\\n');}
""",
        'bridge.js': """
import {appendFileSync} from 'node:fs';
const log=x=>appendFileSync(process.env.KR_RUNNER_FIXTURE_LOG,JSON.stringify(x)+'\\n');
const mode=process.env.KR_RUNNER_FIXTURE_MODE;
let acted=false,active='login-target',viewportFixed=false,created=false,listed=0,visibilityPresented=false;
globalThis.fixtureNavigate=()=>{viewportFixed=false;};
const page={
 evaluate:async js=>{if(js.includes('const title=document.title')){const old=globalThis.fixtureTitle||'Research';globalThis.fixtureTitle=JSON.parse(js.match(/document.title=(".*?");/)[1]);return old;}if(js.startsWith('document.title=')){globalThis.fixtureTitle=JSON.parse(js.split('document.title=')[1]);return;}log({kind:'evaluate',business:js==='fixture-state'});if(js.includes('kr:visibility')&&mode==='visibility')return {visibility:{hidden:!globalThis.fixtureWindowShown,dom_ready:true,reason:globalThis.fixtureWindowShown?'':'explicit_visibility_notice'}};return js.includes('kr:viewport')?{width:viewportFixed?1280:0,height:viewportFixed?800:0}:{risk:mode==='risk'||(mode==='late-risk'&&acted),login:false};},
 getActivePage:()=>active, setActivePage:id=>{active=id;log({kind:'bind',page:id});},
 tabs:async()=>{listed++;log({kind:'tabs-list'});return mode==='closed'||(mode==='gone-before-nav'&&listed>1)||(mode==='empty-work'&&!created)?[]:mode==='ambiguous'?[{page:'one'},{page:'two'}]:[{page:'login-target',url:'https://example.test/login'}];},
 newTab:async url=>{created=true;log({kind:'new-tab',url});return 'login-target';},
 evaluateOnce:async js=>{log({kind:'action'});acted=true;return mode==='ready'?{ready:true}:mode==='missing'?{clicked:false}:{clicked:true};},
 cdp:async(method,args)=>{if(method==='KR.windowPresentation')visibilityPresented=args.foreground;else viewportFixed=method==='Emulation.setDeviceMetricsOverride';log({kind:'cdp',method,...args});if(mode==='cleanup-gone'&&method==='Emulation.clearDeviceMetricsOverride')throw new Error('Page not found: gone — stale page identity');},
 goto:async url=>{viewportFixed=false;log({kind:'goto',url});},
 wait:async opts=>log({kind:'wait',opts}),
 closeWindow:async()=>{log({kind:'close-owned',permission:process.env.OPENCLI_CLOSE_OWNED_TAB});if(mode==='close-failed')throw new Error('close failed');},
};
export class BrowserBridge {
 async connect(opts){log({kind:'connect',...opts});return page;}
 async close(){log({kind:'bridge-close'});}
}
""",
    }
    for filename, content in sources.items():
        target = tmp_path / 'dist/src/browser' / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf8')
    (tmp_path/'window-helper-fixture.mjs').write_text("import cp from 'node:child_process';\nimport {syncBuiltinESMExports} from 'node:module';\nimport {PassThrough} from 'node:stream';\nimport {appendFileSync} from 'node:fs';\ncp.execFile=(exe,args,options,callback)=>{\n const stdin=new PassThrough();let input='';\n stdin.on('data',chunk=>input+=chunk);\n stdin.on('end',()=>{\n  const r=JSON.parse(input);\n  if(r.action==='present')globalThis.fixtureWindowShown=r.foreground;\n  appendFileSync(process.env.KR_RUNNER_FIXTURE_LOG,JSON.stringify({kind:'window-helper',...r})+'\\n');\n  callback(null,JSON.stringify(r.action==='bind'?{handle:1,pid:2}:{}));\n });return {stdin};\n};\nsyncBuiltinESMExports();\n",encoding='utf-8')
    return tmp_path, log


def run(fixture, monkeypatch, mode, **extra):
    root, log = fixture
    monkeypatch.setenv('KR_RUNNER_FIXTURE_MODE', mode)
    request = {'pythonExecutable':'fixture-python','root': str(root), 'contextId': 'fixture', 'session': 'kr:boss',
        'operation': 'detail', 'expression': 'fixture-state', 'steps': [
            {'expression': 'fixture-click', 'fallbackUrl': 'https://www.zhipin.com/job_detail/one.html'}], **extra}
    process = subprocess.run(['node', '--import', (root/'window-helper-fixture.mjs').as_uri(), str(RUNNER)], input=json.dumps(request),
        capture_output=True, text=True, encoding='utf8', timeout=10)
    return json.loads(process.stdout), [json.loads(line) for line in log.read_text(encoding='utf8').splitlines()]


@pytest.mark.parametrize('mode,expected_navigation', [('ready', 0), ('clicked', 0), ('missing', 1)])
def test_reuse_selected_job_or_single_fallback_only(bridge_fixture, monkeypatch, mode, expected_navigation):
    result, calls = run(bridge_fixture, monkeypatch, mode)
    assert result['ok']
    assert sum(c['kind'] == 'goto' for c in calls) == expected_navigation
    assert sum(c['kind'] == 'action' for c in calls) == 1
    assert [c['method'] for c in calls if c['kind'] == 'cdp'] == [
        'Emulation.setDeviceMetricsOverride'] * (1+expected_navigation) + ['Emulation.clearDeviceMetricsOverride']
    assert [c['kind'] for c in calls[-2:]] == ['release', 'bridge-close']


@pytest.mark.parametrize('mode,expected_actions', [('risk', 0), ('late-risk', 1)])
def test_risk_before_or_during_expansion_stops_without_navigation(bridge_fixture, monkeypatch, mode, expected_actions):
    result, calls = run(bridge_fixture, monkeypatch, mode, steps=[{'expression': 'expand'}] * 3)
    assert not result['ok'] and result['code'] == 'SECURITY_BLOCK'
    assert sum(c['kind'] == 'action' for c in calls) == expected_actions
    assert not any(c['kind'] == 'goto' for c in calls)
    assert [c['kind'] for c in calls[-2:]] == ['release', 'bridge-close']


def test_manual_wait_uses_upstream_persistent_session(bridge_fixture, monkeypatch):
    result, calls = run(bridge_fixture, monkeypatch, 'ready', operation='probe', manualWait=True, steps=[])
    assert result['ok']
    assert calls[0]['siteSession'] == 'persistent'
    assert not any(c['kind'] in {'action', 'goto'} for c in calls)


@pytest.mark.parametrize('operation', ['login', 'attention'])
def test_manual_login_never_attaches_debugger_or_reloads(bridge_fixture, monkeypatch, operation):
    result, calls = run(bridge_fixture, monkeypatch, 'ready', operation=operation, manualWait=True,
        passiveManualLogin=True, url='https://example.test/login' if operation=='login' else '', steps=[])
    assert result['ok'] and result['metadata']['debugger_attached'] is False
    assert not any(c['kind'] in {'action','goto','evaluate','cdp','wait'} for c in calls)
    assert sum(c['kind']=='new-tab' for c in calls) == (1 if operation=='login' else 0)
    assert calls[0]['session']=='kr:boss:login'
    assert [c['kind'] for c in calls[-2:]] == ['release','bridge-close']


def test_closed_manual_page_is_not_automatically_recreated(bridge_fixture, monkeypatch):
    result,calls=run(bridge_fixture,monkeypatch,'closed',operation='attention',manualWait=True,
        passiveManualLogin=True,steps=[])
    assert not result['ok'] and result['code']=='manual_login_page_closed'
    assert not any(c['kind'] in {'new-tab','goto','evaluate','cdp'} for c in calls)


@pytest.mark.parametrize('mode',['ready','risk'])
def test_auth_verification_uses_disposable_page_not_manual_session(bridge_fixture,monkeypatch,mode):
    result,calls=run(bridge_fixture,monkeypatch,mode,operation='auth_verify',steps=[])
    assert calls[0]['session']=='kr:boss:verify'
    assert [c for c in calls if c['kind']=='close-owned']==[{'kind':'close-owned','permission':'1'}]
    assert [c['method'] for c in calls if c['kind']=='cdp']==[
        'Emulation.setDeviceMetricsOverride','Emulation.clearDeviceMetricsOverride']
    cleared=next(i for i,c in enumerate(calls) if c.get('method')=='Emulation.clearDeviceMetricsOverride')
    closed=next(i for i,c in enumerate(calls) if c['kind']=='close-owned')
    assert cleared < closed
    assert not any(c['kind'] in {'evaluate','cdp','goto'} for c in calls[closed+1:])
    assert bool(result['ok'])==(mode=='ready')


@pytest.mark.parametrize('mode,new_tabs',[('ready',0),('empty-work',1)])
def test_work_page_bound_before_layout_and_navigation(bridge_fixture,monkeypatch,mode,new_tabs):
    result,calls=run(bridge_fixture,monkeypatch,mode,operation='search',
        url='https://example.test/search',steps=[])
    assert result['ok']
    bound=next(i for i,c in enumerate(calls) if c['kind']=='bind')
    layout=next(i for i,c in enumerate(calls) if c['kind']=='evaluate')
    navigated=next(i for i,c in enumerate(calls) if c['kind']=='goto')
    assert bound < layout < navigated
    assert sum(c['kind']=='new-tab' for c in calls)==new_tabs


def test_ambiguous_owned_work_page_stops_before_dom_or_navigation(bridge_fixture,monkeypatch):
    result,calls=run(bridge_fixture,monkeypatch,'ambiguous',steps=[])
    assert not result['ok'] and result['code']=='opencli_session_page_ambiguous'
    assert not any(c['kind'] in {'evaluate','cdp','goto','action','new-tab'} for c in calls)


def test_navigation_can_reset_viewport_and_wait_needs_restored_layout(bridge_fixture,monkeypatch):
    result,calls=run(bridge_fixture,monkeypatch,'ready',operation='search',
        url='https://example.test/search',waitSelector='.result',steps=[])
    assert result['ok']
    navigated=next(i for i,c in enumerate(calls) if c['kind']=='goto')
    waited=next(i for i,c in enumerate(calls) if c['kind']=='wait')
    assert any(c.get('method')=='Emulation.setDeviceMetricsOverride' for c in calls[navigated+1:waited])


def test_navigation_uses_native_command_without_page_global_patches(bridge_fixture,monkeypatch):
    result,calls=run(bridge_fixture,monkeypatch,'ready',operation='search',
        url='https://example.test/search',steps=[])
    assert result['ok']
    navigation=[c for c in calls if c['kind']=='goto']
    assert len(navigation)==1 and navigation[0].get('action')=='navigate'
    assert navigation[0]['page']=='login-target'
    assert navigation[0]['siteSession']=='ephemeral'


def test_disappearing_target_during_cleanup_does_not_mask_result(bridge_fixture,monkeypatch):
    result,calls=run(bridge_fixture,monkeypatch,'cleanup-gone',steps=[])
    assert result['ok']
    assert result['metadata']['cleanup_warnings']
    assert [c['kind'] for c in calls[-2:]]==['release','bridge-close']


def test_closed_target_is_not_reopened_by_navigation_or_cleanup(bridge_fixture,monkeypatch):
    result,calls=run(bridge_fixture,monkeypatch,'gone-before-nav',operation='search',
        url='https://example.test/search',steps=[])
    assert not result['ok'] and result['code']=='browser_navigation_interrupted'
    assert result['request_executed'] is False
    assert not any(c['kind'] in {'goto','new-tab'} for c in calls)
    assert not any(c.get('method')=='Emulation.clearDeviceMetricsOverride' for c in calls)
    assert result['metadata']['cleanup_warnings']==['viewport_target_gone']


@pytest.mark.parametrize('mode,code',[('stale-navigation','browser_navigation_interrupted'),
    ('risk-navigation','SECURITY_BLOCK')])
def test_navigation_error_preserves_risk_and_classifies_stale_target(bridge_fixture,monkeypatch,mode,code):
    result,calls=run(bridge_fixture,monkeypatch,mode,operation='search',
        url='https://example.test/search',steps=[])
    assert not result['ok'] and result['code']==code
    assert sum(c['kind']=='goto' for c in calls)==1


def test_work_calls_use_new_lease_generations_and_close_before_daemon_release(bridge_fixture,monkeypatch):
    result,first=run(bridge_fixture,monkeypatch,'ready',steps=[],isolatedWork=True)
    assert result['ok']
    session=first[0]['session']
    assert session.startswith('kr:boss:work:')
    assert first[0]['siteSession']=='ephemeral'
    assert first[0]['idleTimeout']==120
    assert [c['kind'] for c in first[-3:]]==['close-owned','release','bridge-close']
    assert first[-3]['permission']=='1'
    second_result,all_calls=run(bridge_fixture,monkeypatch,'ready',steps=[],isolatedWork=True)
    second=all_calls[len(first):]
    assert second_result['ok'] and second[0]['session']!=session


@pytest.mark.parametrize('mode',['stale-navigation','cleanup-gone','close-failed'])
def test_failed_or_successful_work_always_settles_its_owned_tab(bridge_fixture,monkeypatch,mode):
    result,calls=run(bridge_fixture,monkeypatch,mode,url='https://example.test/search',steps=[],isolatedWork=True)
    assert sum(c['kind']=='close-owned' for c in calls)==1
    assert [c['kind'] for c in calls[-2:]]==['release','bridge-close']
    if mode=='close-failed':
        assert result['ok'] and 'work_page_cleanup_failed' in result['metadata']['cleanup_warnings']


def test_isolated_risk_page_is_retained_without_focus_or_replay(bridge_fixture,monkeypatch):
    result,calls=run(bridge_fixture,monkeypatch,'risk',url='https://example.test/search',steps=[],isolatedWork=True)
    assert result['code']=='SECURITY_BLOCK'
    assert result['metadata']['retained_for_interaction'] and result['metadata']['retention_max_s']==120
    assert not result['metadata']['work_page_closed'] and not result['metadata']['automatic_retry']
    assert not any(c['kind'] in {'close-owned','action','new-tab'} for c in calls)
    assert [c['kind'] for c in calls[-2:]]==['release','bridge-close']


def test_manual_login_keeps_stable_handoff_lease(bridge_fixture,monkeypatch):
    result,calls=run(bridge_fixture,monkeypatch,'ready',operation='attention',manualWait=True,
        passiveManualLogin=True,steps=[],isolatedWork=True)
    assert result['ok'] and calls[0]['session']=='kr:boss:login'
    assert not any(c['kind']=='close-owned' for c in calls)


def test_isolated_work_never_inherits_manual_persistent_hold(bridge_fixture,monkeypatch):
    result,calls=run(bridge_fixture,monkeypatch,'ready',operation='search',manualWait=True,
        isolatedWork=True,idleTimeoutS=1,url='https://example.test/search',steps=[])
    assert result['ok']
    assert calls[0]['siteSession']=='ephemeral'
    assert calls[0]['idleTimeout']==120
    assert next(c for c in calls if c['kind']=='goto')['siteSession']=='ephemeral'


def test_stateful_adapters_keep_their_shared_page_contract(bridge_fixture,monkeypatch):
    result,calls=run(bridge_fixture,monkeypatch,'ready',operation='comments',steps=[])
    assert result['ok'] and calls[0]['session']=='kr:boss'
    assert not any(c['kind']=='close-owned' for c in calls)


def test_derivative_extension_rejected_before_any_window_or_page(bridge_fixture, monkeypatch):
    root, log = bridge_fixture
    log.write_text('',encoding='utf-8')
    transport = root / 'dist/src/browser/daemon-transport.js'
    transport.write_text(transport.read_text(encoding='utf-8').replace('1.0.26','1.0.26.2'),encoding='utf-8')
    result, calls = run(bridge_fixture, monkeypatch, 'ready', operation='detail', steps=[])
    assert result['code']=='opencli_identity_mismatch'
    assert result['request_executed'] is False
    assert calls == []


def test_navigation_explicitly_keeps_background_mode(bridge_fixture, monkeypatch):
    result, calls = run(bridge_fixture, monkeypatch, 'ready', operation='detail', url='https://example.test/', steps=[])
    assert result['ok']
    assert all(c['windowMode']=='background' for c in calls if c['kind'] in {'connect','goto'})

def test_visibility_is_resolved_before_one_business_read_without_replay(bridge_fixture, monkeypatch):
    result, calls = run(bridge_fixture, monkeypatch, 'visibility', steps=[])
    assert result['ok']
    reads = [i for i, c in enumerate(calls) if c.get('business')]
    assert len(reads) == 1
    foreground = next(i for i, c in enumerate(calls) if c.get('kind') == 'window-helper' and c.get('foreground'))
    background = max(i for i, c in enumerate(calls) if c.get('kind') == 'window-helper' and c.get('foreground') is False)
    assert foreground < reads[0] < background
    assert not any(c['kind'] in {'goto', 'action'} for c in calls)


def test_packaged_boss_dom_and_native_extension_routing():
    node=shutil.which('node')
    if not node:pytest.skip('node unavailable')
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run([node,'--test','tests/test_boss_dom.mjs','tests/test_browser_link_extension.mjs'],cwd=root,capture_output=True,text=True,encoding='utf-8',timeout=20)
    assert result.returncode==0, result.stdout+'\n'+result.stderr
