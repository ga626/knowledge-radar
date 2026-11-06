"""执行真实KR Node runner，替换边界模块；不访问账号或网站。"""
import json
from pathlib import Path
import shutil
import subprocess
import pytest

NOTE="0123456789abcdef01234567"
URL="https://www.xiaohongshu.com/explore/"+NOTE+"?xsec_token=fixture-private"
RUNNER=Path(__file__).resolve().parents[1]/"src/runtime/xhs_opencli_runner.mjs"


@pytest.fixture
def fixture_root(tmp_path):
    (tmp_path/"package.json").write_text('{"type":"module"}',encoding="utf-8")
    modules={
        "dist/src/browser/daemon-transport.js": "export async function fetchDaemonStatus(){return {extensionConnected:true,daemonVersion:'2.0.2',extensionVersion:'1.0.26'};} export async function getDaemonHealth(){return {state:'ready',status:await fetchDaemonStatus()};}",
        "dist/src/browser/bridge-readiness.js": "export async function waitForBridgeReady(fetch,opts){return fetch(opts);}",
        "dist/src/browser/daemon-client.js": """
import {appendFileSync} from 'node:fs';
const log=x=>appendFileSync(process.env.KR_RUNNER_FIXTURE_LOG,JSON.stringify(x)+'\\n');
export const generateRunId=()=> 'fixture-run';
export const setDaemonRunContext=x=>log({kind:'context',...x});
export const releaseSiteSessionLease=async x=>log({kind:'release',...x});
export const sendCommand=async(action,opts)=>opts?.chromeMethod==='tabs.query'
 ? [{id:10,title:globalThis.fixtureTitle||'Research',url:'https://www.xiaohongshu.com/explore',windowId:7}]
 : opts?.chromeMethod==='windows.get'?{state:'normal',focused:true}:[];
export const sendCommandFull=async()=>({page:'fixture-target',data:{visible:true,focused:true}});
""",
        "dist/src/browser/bridge.js": """
import {appendFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
const log=x=>appendFileSync(process.env.KR_RUNNER_FIXTURE_LOG,JSON.stringify(x)+'\\n');
const mode=process.env.KR_RUNNER_FIXTURE_MODE;
const page={
  session:'kr:xiaohongshu',surface:'adapter',contextId:'daily-fixture',
  getActivePage:()=> 'fixture-target',setActivePage:()=>{},
  tabs:async()=>[{page:'fixture-target',url:'https://www.xiaohongshu.com/explore'}],
  goto:async url=>log({kind:'goto',url}),
  cdp:async(method,args)=>{log({kind:'cdp',method,args});viewportReady=method==='Emulation.setDeviceMetricsOverride';return {};},
  evaluate:async js=>{
    if(js.includes('const title=document.title')){const old=globalThis.fixtureTitle||'Research';globalThis.fixtureTitle=JSON.parse(js.match(/document.title=(".*?");/)[1]);return old;}
    if(js.startsWith('document.title=')){globalThis.fixtureTitle=JSON.parse(js.split('document.title=')[1]);return;}
    if(js==='location.href')return 'https://www.xiaohongshu.com/explore';
    if(js.includes('kr:viewport')) return {width:mode==='zero-viewport'&&!viewportReady?0:1280,height:mode==='zero-viewport'&&!viewportReady?0:800};
    if(js.includes('const body = document.body')&&mode==='sms-login') {
      const dialog={innerText:'手机号登录 获取验证码',getBoundingClientRect:()=>({width:400})};
      const document={body:{innerText:dialog.innerText},querySelector:s=>s==='#login-btn'?{}:null,
        querySelectorAll:s=>s.includes('[role=')?[dialog]:[]};
      return runInNewContext(js,{document,location:{href:'https://www.xiaohongshu.com/explore',pathname:'/explore'}});
    }
    if(js.includes('const body = document.body')) return {authenticated_ui:mode!=='login',login:mode==='login',risk:mode==='risk',path:'/explore/0123456789abcdef01234567'};
    if(js.includes('const previous = JSON.parse')) return true;
    if(js==='fixture-note') return {title:'笔记',author:'作者',desc:'有用正文',comments:mode==='zero'?'0':'2'};
    if(js==='fixture-comments') {
      if(mode==='partial')throw Object.assign(Error('comments'),{code:'SECURITY_BLOCK'});
      return {results:[{text:'评论',is_reply:false},{text:'回复',is_reply:true,reply_to:'作者'}]};
    }
    if(js==='fixture-media'||js.includes('const result = fixture-media')) return {media:[{type:'image',url:'https://sns-img.xhscdn.com/fixture.jpg'}]};
    return true;
  },
  closeWindow:async()=>log({kind:'close-owned',enabled:process.env.OPENCLI_CLOSE_OWNED_TAB}),
};
let viewportReady=false;
export class BrowserBridge {
 async connect(opts){log({kind:'connect',...opts});return page;}
 async close(){log({kind:'bridge-close'});}
}
""",
        "clis/xiaohongshu/risk-control.js": """
import {appendFileSync} from 'node:fs';
export async function readXhsDetailPage(page,{url,extractJs,retryOnBlock=false}){if(retryOnBlock)throw Error('retry prohibited');appendFileSync(process.env.KR_RUNNER_FIXTURE_LOG,JSON.stringify({kind:'detail-read',extractJs})+'\\n');await page.goto(url);return page.evaluate(extractJs);}
""",
        "clis/xiaohongshu/note.js":"export const NOTE_EXTRACT_JS='fixture-note';",
        "clis/xiaohongshu/download.js":"export const buildDownloadExtractJs=()=> 'fixture-media';",
        "clis/xiaohongshu/search.js":"""
export const command={func:async(page,args)=>{await page.goto('https://www.xiaohongshu.com/explore');if(process.env.KR_RUNNER_FIXTURE_MODE==='diagnostic')throw Object.assign(Error('search_input_not_available https://example.org/?xsec_token=url-secret Bearer bearer-secret "authorization": "header-secret" cookie=session-secret'),{code:'COMMAND_EXEC'});if(process.env.KR_RUNNER_FIXTURE_MODE==='zero-viewport'){const v=await page.evaluate('kr:viewport');if(v.width<=0||v.height<=0)throw Object.assign(Error('Xiaohongshu in-site search failed: search_input_not_available'),{code:'COMMAND_EXEC'});}return [{title:args.query,url:'https://www.xiaohongshu.com/explore/0123456789abcdef01234567?xsec_token=fixture-private'}];}};
""",
        "clis/xiaohongshu/comments.js":"""
export const buildCommentsExtractJs=()=> 'fixture-comments';
export const assertCommentsLoaded=()=>{};
export const command={func:async(page,args)=>{
  if(process.env.KR_RUNNER_FIXTURE_MODE==='partial')throw Object.assign(Error('comments'),{code:'SECURITY_BLOCK'});
  await page.goto(args['note-id']);return [{text:'评论',is_reply:false},{text:'回复',is_reply:true,reply_to:'作者'}];
}};
""",
    }
    for rel,text in modules.items():
        target=tmp_path/rel
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(text,encoding="utf-8")
    (tmp_path/'window-helper-fixture.mjs').write_text("import cp from 'node:child_process';\nimport {syncBuiltinESMExports} from 'node:module';\nimport {PassThrough} from 'node:stream';\nimport {appendFileSync} from 'node:fs';\ncp.execFile=(exe,args,options,callback)=>{\n const stdin=new PassThrough();let input='';\n stdin.on('data',chunk=>input+=chunk);\n stdin.on('end',()=>{\n  const r=JSON.parse(input);\n  if(r.action==='present')globalThis.fixtureWindowShown=r.foreground;\n  appendFileSync(process.env.KR_RUNNER_FIXTURE_LOG,JSON.stringify({kind:'window-helper',...r})+'\\n');\n  callback(null,JSON.stringify(r.action==='bind'?{handle:1,pid:2}:{}));\n });return {stdin};\n};\nsyncBuiltinESMExports();\n",encoding='utf-8')
    return tmp_path


def run(root,monkeypatch,operation="detail",mode="normal",**extra):
    if not shutil.which("node"):pytest.skip("node unavailable")
    log=root/"calls.jsonl"
    monkeypatch.setenv("KR_RUNNER_FIXTURE_LOG",str(log))
    monkeypatch.setenv("KR_RUNNER_FIXTURE_MODE",mode)
    completed=subprocess.run(["node","--import",(root/"window-helper-fixture.mjs").as_uri(),str(RUNNER)],input=json.dumps({"pythonExecutable":"fixture-python","root":str(root),
        "operation":operation,"contextId":"daily-chrome","url":URL,"keyword":"包",
        "include_comments":True,"comment_limit":10,**extra}),capture_output=True,text=True,encoding="utf-8",timeout=10)
    result=json.loads(completed.stdout)
    assert all(isinstance(v,(float,int)) and v >= 0 for v in result.get('metadata',{}).get('browser_timings',{}).values())
    calls=[json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    return result,calls


def test_media_reuses_ready_note_without_another_detail_settle(fixture_root,monkeypatch):
    result,calls=run(fixture_root,monkeypatch)
    assert result['ok'] and len(result['data']['media_manifest'])==1
    reads=[c for c in calls if c['kind']=='detail-read']
    assert len([c for c in reads if c['extractJs']=='fixture-note'])==1
    assert len([c for c in reads if c['extractJs']=='fixture-comments'])==1
    assert len([c for c in calls if c['kind']=='goto'])==1


def test_unsigned_detail_on_opaque_work_page_requests_fresh_link_without_navigation(fixture_root,monkeypatch):
    bridge=fixture_root/'dist/src/browser/bridge.js'
    bridge.write_text(bridge.read_text(encoding='utf-8').replace(
        "if(js==='location.href')", """if(js.includes('let cache;')) {
      const document={querySelectorAll:()=>[]};
      return runInNewContext(js,{document,Date,get sessionStorage(){throw new DOMException('Access is denied','SecurityError');},DOMException});
    }
    if(js==='location.href')"""),encoding='utf-8')
    result,calls=run(fixture_root,monkeypatch,url='https://www.xiaohongshu.com/explore/'+NOTE)
    assert not result['ok'] and result['code']=='signed_link_expired_search_again'
    assert result['diagnostic']['stage']=='signed_link_resolution'
    assert not result['request_executed']
    assert not any(c['kind']=='goto' for c in calls)
    assert result['metadata']['work_page_closed']


def test_media_resume_without_a_loaded_body_still_uses_native_ready_path(fixture_root,monkeypatch):
    result,calls=run(fixture_root,monkeypatch,include_comments=False,checkpoint={
        'note_id':NOTE,'content':'已有正文','completed_stages':['body']})
    assert result['ok'] and len(result['data']['media_manifest'])==1
    reads=[c for c in calls if c['kind']=='detail-read']
    assert len(reads)==1 and 'fixture-media' in reads[0]['extractJs']


def test_full_runner_uses_one_navigation_and_native_session_cleanup(fixture_root,monkeypatch):
    result,calls=run(fixture_root,monkeypatch)
    assert result["ok"] and result["data"]["comments"][1]["is_reply"]
    assert len([c for c in calls if c["kind"]=="goto"])==1
    connect=next(c for c in calls if c["kind"]=="connect")
    assert connect["windowMode"]=="background" and connect["siteSession"]=="ephemeral"
    assert connect["session"]=="kr:xiaohongshu" and connect["idleTimeout"]==120
    assert calls[-2]["kind"]=="release" and calls[-1]["kind"]=="bridge-close"
    assert len([c for c in calls if c["kind"]=="close-owned"])==1


def test_kr_chain_from_search_to_models_and_immutable_archive(fixture_root,monkeypatch):
    """真实KR/Node边界、实际下载校验/落库；站点与模型用替身，不冒充真人验收。"""
    import hashlib
    import io
    from PIL import Image
    from runtime import xhs_opencli as oc, optional_media
    from runtime.platform_execution import ensure_bindings, worker_detail
    from kr_core import DetailRequest
    from kr_core.models import SearchRequest
    from kr_core.registry import registry
    from generic_web import url_policy
    if not shutil.which("node"): pytest.skip("node unavailable")
    (fixture_root/"package.json").write_text('{"type":"module","version":"2.0.2"}',encoding="utf-8")
    (fixture_root/"clis/xiaohongshu/note-helpers.js").write_text('',encoding="utf-8")
    bridge=fixture_root/"dist/src/browser/bridge.js"
    bridge.write_text(bridge.read_text(encoding="utf-8").replace(
        "url:'https://sns-img.xhscdn.com/fixture.jpg'}]",
        "url:'https://sns-img.xhscdn.com/fixture.jpg'},{type:'video',url:'https://sns-video.xhscdn.com/fixture.mp4'}]"),encoding="utf-8")
    digest=hashlib.sha256()
    for name in ("search","note","comments","download","risk-control","note-helpers"):
        rel='clis/xiaohongshu/'+name+'.js';digest.update(rel.encode());digest.update((fixture_root/rel).read_bytes())
    (fixture_root/"disabled-policy.json").write_text('{"schema":"knowledgeradar-paid-policy/v2","providers":{"tikhub":{"enabled":false}}}',encoding="utf-8")
    for key,value in {"KR_XHS_OPENCLI_ROOT":str(fixture_root),"KR_XHS_OPENCLI_ADAPTER_SHA256":digest.hexdigest(),
        "KR_XHS_OPENCLI_CONTEXT_ID":"daily-fixture","KR_XHS_OPENCLI_STATE_DIR":str(fixture_root/"state"),
        "KR_STATE_DIR":str(fixture_root/"state"),"KR_LOG_DIR":str(fixture_root/"logs"),
        "KR_MEDIA_CACHE_DIR":str(fixture_root/"cache"),"KR_EVIDENCE_ROOT":str(fixture_root/"assets"),
        "KR_RUNTIME_LEASE_DB_PATH":str(fixture_root/"leases.sqlite3"),
        "KR_RUNNER_FIXTURE_LOG":str(fixture_root/"calls.jsonl"),"KR_RUNNER_FIXTURE_MODE":"normal",
        "KR_PAID_POLICY_PATH":str(fixture_root/"disabled-policy.json")}.items(): monkeypatch.setenv(key,value)
    monkeypatch.setattr(oc,"_ensure_daemon",lambda *a:"")
    monkeypatch.setattr(oc,"connection_status",lambda:{"connected":True})
    original_run=oc.silent_subprocess_run
    def run_with_window_fixture(command, **kwargs):
        return original_run([command[0], '--import', (fixture_root/'window-helper-fixture.mjs').as_uri(), *command[1:]], **kwargs)
    monkeypatch.setattr(oc,'silent_subprocess_run',run_with_window_fixture)

    image=io.BytesIO();Image.new('RGB',(8,8)).save(image,format='PNG')
    import av
    video=io.BytesIO()
    with av.open(video,'w',format='mp4') as container:
        stream=container.add_stream('mpeg4',rate=1);stream.width=16;stream.height=16;stream.pix_fmt='yuv420p'
        frame=av.VideoFrame(16,16,'yuv420p')
        for plane in frame.planes: plane.update(bytes(plane.buffer_size))
        for packet in stream.encode(frame): container.mux(packet)
        for packet in stream.encode(None): container.mux(packet)
    fetched=[];models=[]
    class Response:
        def __init__(self,url): self.content=image.getvalue() if url.endswith('.jpg') else video.getvalue()
        def raise_for_status(self): pass
    monkeypatch.setattr(url_policy,"guarded_fetch",lambda url,**kwargs:fetched.append(url) or Response(url))
    monkeypatch.setattr(optional_media,"run_optional",lambda payload:models.append(payload['component']) or
        {"status":"acquired","component":payload['component'],"text":"可归档的模型结果"})
    ensure_bindings()
    found=registry.get("小红书").search(SearchRequest(platform="小红书",keyword="测试资料",limit=1)).to_mcp_dict()
    assert found['items'] and 'fixture-private' not in json.dumps(found)
    request=registry.prepare_detail(DetailRequest(platform="小红书",url=found['items'][0]['url'],
        auto_multimodal=True,options={"include_comments":True,"include_ocr":True,"include_transcript":True,"research_task_id":"fixture-research"}))
    result=worker_detail(request)
    assert result['content']=='有用正文' and result['comments'][1]['is_reply']
    assert len(fetched)==2 and len(models)==2 and result['ocr']['status']=='ok'
    assert {'content','comments','media_manifest','ocr_text','transcript'} <= {ref['kind'] for ref in result['evidence_assets']}
    assert len(result['downloaded_media'])==2
    calls_before=len((fixture_root/'calls.jsonl').read_text(encoding='utf-8').splitlines())
    from dataclasses import replace
    resumed=worker_detail(replace(request,options={**request.options,'resume_checkpoint':True}))
    assert resumed['content']==result['content'] and len(fetched)==2 and len(models)==2
    assert len((fixture_root/'calls.jsonl').read_text(encoding='utf-8').splitlines())==calls_before
    assert all(asset['reused'] for asset in resumed['downloaded_media'])


def test_partial_body_survives_native_comment_challenge(fixture_root,monkeypatch):
    result,calls=run(fixture_root,monkeypatch,mode="partial")
    assert not result["ok"] and result["code"]=="SECURITY_BLOCK"
    assert result["data"]["content"]=="有用正文"
    assert result["data"]["completed_stages"]==["body"]
    assert calls[-2]["kind"]=="release"


def test_zero_comments_is_observed_success_without_empty_api_retry(fixture_root,monkeypatch):
    result,_=run(fixture_root,monkeypatch,mode="zero")
    assert result["ok"] and result["data"]["comments"]==[]
    assert result["data"]["comments_coverage"]=="observed_zero_comments"


def test_login_wall_stops_search_after_first_navigation(fixture_root,monkeypatch):
    result,calls=run(fixture_root,monkeypatch,operation="search",mode="login")
    assert result["code"]=="AUTH_REQUIRED"
    assert len([c for c in calls if c["kind"]=="goto"])==1


def test_sms_login_code_is_login_wall_not_security_challenge(fixture_root,monkeypatch):
    result,calls=run(fixture_root,monkeypatch,operation="search",mode="sms-login")
    assert result["code"]=="AUTH_REQUIRED"
    assert len([c for c in calls if c["kind"]=="goto"])==1


def test_execution_diagnostic_retains_reason_without_credentials(fixture_root,monkeypatch):
    result,_=run(fixture_root,monkeypatch,operation="search",mode="diagnostic")
    assert result['diagnostic']['reason']=='search_input_not_available'
    assert 'secret' not in json.dumps(result)


def test_risk_never_triggers_retry_or_account_switch(fixture_root,monkeypatch):
    result,calls=run(fixture_root,monkeypatch,operation="search",mode="risk")
    assert result["code"]=="SECURITY_BLOCK"
    assert len([c for c in calls if c["kind"]=="goto"])==1
    assert result['metadata']['retained_for_interaction']
    assert result['metadata']['retention_max_s']==120
    assert not any(c['kind']=='close-owned' for c in calls)


def test_zero_viewport_is_repaired_on_owned_page_before_search(fixture_root,monkeypatch):
    result,calls=run(fixture_root,monkeypatch,operation="search",mode="zero-viewport")
    assert result['ok']
    changed=[c for c in calls if c['kind']=='cdp']
    assert changed[0]['method']=='Emulation.setDeviceMetricsOverride'
    assert changed[0]['args']['width']==1280 and changed[0]['args']['height']==800
    assert changed[-1]['method']=='Emulation.clearDeviceMetricsOverride'
    assert len([c for c in calls if c['kind']=='goto'])==1


def test_explicit_release_can_only_close_owned_tab(fixture_root,monkeypatch):
    result,calls=run(fixture_root,monkeypatch,operation="release")
    assert result["ok"] and result["data"]["browser_closed"] is False
    assert next(c for c in calls if c["kind"]=="close-owned")["enabled"]=="1"


def test_login_has_foreground_and_finite_native_idle_timeout(fixture_root,monkeypatch):
    result,calls=run(fixture_root,monkeypatch,operation="login",mode="login")
    assert result["ok"] and result["data"]["login_opened"]
    connect=next(c for c in calls if c["kind"]=="connect")
    assert connect["windowMode"]=="foreground" and connect["idleTimeout"]==300
    assert connect["siteSession"]=="ephemeral"


@pytest.mark.parametrize('operation',['session_probe','resume'])
def test_login_probe_preserves_window_mode_and_remaining_grace(fixture_root,monkeypatch,operation):
    result,calls=run(fixture_root,monkeypatch,operation=operation,idleTimeoutS=247)
    assert result['ok']
    connect=next(c for c in calls if c['kind']=='connect')
    assert connect['windowMode']=='background' and connect['idleTimeout']==247
    assert not any(c['kind']=='goto' for c in calls)


def test_checkpoint_skips_body_and_reads_only_missing_comments_and_media(fixture_root,monkeypatch):
    result,calls=run(fixture_root,monkeypatch,checkpoint={"note_id":NOTE,"content":"已有正文","completed_stages":["body"]})
    assert result["ok"] and result["data"]["content"]=="已有正文"
    assert len([c for c in calls if c["kind"]=="goto"])==1
