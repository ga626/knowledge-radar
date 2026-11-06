"""共享连接状态合同；不操作用户Chrome或重启daemon。"""
import json
from pathlib import Path
import shutil
import subprocess
import pytest


@pytest.mark.parametrize('operation',['search','detail','comments','auth_verify'])
def test_python_bridge_checks_bound_browser_before_dispatch(monkeypatch,tmp_path,operation):
    from runtime import opencli_bridge, browser_channel, process
    (tmp_path/'package.json').write_text('{"version":"2.0.2"}',encoding='utf-8')
    monkeypatch.setattr(opencli_bridge,'connection_config',lambda:{'root':str(tmp_path),'context_id':'approved-context','state_root':tmp_path})
    monkeypatch.setattr(opencli_bridge,'ensure_daemon',lambda *a,**k:'')
    monkeypatch.setenv('KR_NODE_EXE','unused-node')
    monkeypatch.setattr(process,'silent_subprocess_run',lambda *a,**k:pytest.fail('disconnected browser must not dispatch'))
    observed=[]
    class Status:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def get(self,url,**kw):
            observed.append(kw)
            return type('Response',(),{'json':lambda self:{'daemonVersion':'2.0.2','extensionConnected':False}})()
    monkeypatch.setattr('httpx.Client',lambda **k:Status())
    def ready(connected,**kwargs):
        assert kwargs['timeout_s']==18 and connected() is False
        return {'ok':False,'code':'browser_channel_not_connected','request_executed':False}
    monkeypatch.setattr(browser_channel,'ensure_borrowed_browser',ready)
    result=opencli_bridge.invoke_page(session='wechat',operation=operation)
    assert result['code']=='browser_channel_not_connected'
    assert observed[0]['params']=={'contextId':'approved-context'}

@pytest.mark.parametrize('mode,code',[('reconnect',''),('offline','browser_disconnected'),('wrong-version','opencli_daemon_identity_mismatch')])
def test_native_wait_handles_reconnect_without_dispatch(mode,code):
    node=shutil.which('node')
    if not node:pytest.skip('node unavailable')
    helper=(Path(__file__).parents[1]/'src/runtime/opencli_readiness.mjs').as_uri()
    program=r'''
const {waitForBoundBrowser}=await import(process.argv[1]);
const mode=process.argv[2],calls=[];
let count=0;
const load=async name=>name.endsWith('daemon-transport.js')?{
 getDaemonHealth:async options=>{calls.push({kind:'health',...options});return {
  state:mode==='offline'||(mode==='reconnect'&&count++===0)?'no-extension':'ready',
  status:{daemonVersion:mode==='wrong-version'?'old':'2.0.2',extensionVersion:'1.0.26'}};}
}:{waitForBridgeReady:async(fetch,options)=>{
 calls.push({kind:'native-wait',...options});
 const first=await fetch(options);return first.state==='ready'?first:await fetch(options);
}};
try{await waitForBoundBrowser(load,'original-binding');console.log(JSON.stringify({code:'',calls}));}
catch(error){console.log(JSON.stringify({code:error.code,calls}));}
'''
    completed=subprocess.run([node,'--input-type=module','-e',program,helper,mode],capture_output=True,text=True,encoding='utf-8',timeout=10,check=True)
    result=json.loads(completed.stdout)
    assert result['code']==code
    assert result['calls'][0]=={'kind':'native-wait','contextId':'original-binding','timeoutMs':18000}
    assert all(call['contextId']=='original-binding' for call in result['calls'])
    assert len([call for call in result['calls'] if call['kind']=='health'])==(1 if mode=='wrong-version' else 2)


def test_durable_identity_includes_browser_runner_and_shared_readiness(tmp_path):
    from runtime.task_service import source_identity
    source=tmp_path/'src';source.mkdir()
    (source/'adapter.py').write_text('value=1',encoding='utf-8')
    (source/'driver.mjs').write_text('export const value=1',encoding='utf-8')
    first=source_identity(tmp_path)
    (source/'driver.mjs').write_text('export const value=2',encoding='utf-8')
    assert source_identity(tmp_path)!=first


@pytest.mark.parametrize('mode',['hidden','already-focused','select-failed'])
def test_attention_preserves_login_page_and_cleans_only_its_blank_tab(mode):
    node=shutil.which('node')
    if not node:pytest.skip('node unavailable')
    helper=(Path(__file__).parents[1]/'src/runtime/opencli_readiness.mjs').as_uri()
    program=r'''
const {presentOwnedPage}=await import(process.argv[1]);
const mode=process.argv[2],calls=[];
let shown=mode==='already-focused';
const page={session:'kr:boss',surface:'adapter',contextId:'daily',
 getActivePage:()=>undefined,setActivePage:id=>calls.push(['identity',id]),
 tabs:async()=>{calls.push(['tabs-list']);return [{page:'login-target',url:'https://example.test/login'}];},
 selectTab:async id=>{calls.push(['select',id]);if(mode==='select-failed')throw new Error('select failed');},
 evaluate:async()=>'https://example.test/login'};
const load=async name=>name.endsWith('daemon-client.js')?{
 sendCommandFull:async(action,opts)=>{calls.push(['read',action,opts]);return {page:'login-target',data:{visible:true,focused:mode==='already-focused'}};},
 sendCommand:async(action,opts)=>opts.chromeMethod==='tabs.query'?[{url:'https://example.test/login',windowId:7}]:{state:'normal',focused:shown},
 releaseSiteSessionLease:async opts=>calls.push(['release',opts.session])
}:{BrowserBridge:class {
 async connect(opts){calls.push(['connect',opts]);return {
  newTab:async(...args)=>{calls.push(['blank',args]);shown=true;},
  closeWindow:async()=>calls.push(['close-owned',process.env.OPENCLI_CLOSE_OWNED_TAB])};}
 async close(){calls.push(['disconnect']);}
}};
try{console.log(JSON.stringify({result:await presentOwnedPage(load,page,'run-unique'),calls}));}
catch(error){console.log(JSON.stringify({error:error.message,calls}));}
'''
    completed=subprocess.run([node,'--input-type=module','-e',program,helper,mode],capture_output=True,text=True,encoding='utf-8',timeout=10,check=True)
    result=json.loads(completed.stdout)
    calls=result['calls']
    assert calls[0][0]=='tabs-list'
    assert not any(c[0]=='read' for c in calls)
    assert ['identity','login-target'] in calls
    if mode=='already-focused':
        assert len(calls)==2
        return
    connection=next(c[1] for c in calls if c[0]=='connect')
    assert connection['session']=='kr:boss:attention:run-unique'
    assert connection['surface']=='adapter' and connection['windowMode']=='foreground'
    assert ['blank',[]] in calls
    assert ['select','login-target'] in calls
    assert ['close-owned','1'] in calls
    assert ['release','kr:boss:attention:run-unique'] in calls
    assert ['disconnect'] in calls
    assert bool(result.get('error'))==(mode=='select-failed')
    if mode=='hidden':assert result['result']=={'visible':True,'focused':True,'source':'chrome_native'}
