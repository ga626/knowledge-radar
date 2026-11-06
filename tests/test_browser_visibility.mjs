import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {visibilityPolicy as policy,attentionController} from '../src/runtime/opencli_readiness.mjs';

for(const state of [{}, {risk:true}, {login:true}, {http_status:403}, {http_status:429},
  {failure_type:'native_command_timeout'}, {content:''}, {visibility:{hidden:true}},
  {interaction:{visible:false,kind:'captcha'}}, {interaction:{visible:true,kind:'rate_limit'}},
  {visibility:{hidden:true,dom_ready:true,stalled:true,reason:'empty_content'}},
  {visibility:{hidden:true,dom_ready:true,stalled:true,reason:'collapsed_layout_after_repair'}}]) {
  test('no speculative foreground: '+JSON.stringify(state),()=>assert.equal(policy.decide(state).action,'none'));
}
for(const kind of ['login','qr','captcha','slider']) {
  test('a concrete interaction presents once: '+kind,()=>{
    const state={interaction:{visible:true,kind}};
    assert.deepEqual(policy.decide(state),{action:'foreground',reason:'interaction_'+kind,manual:true});
    assert.equal(policy.decide(state,{presented:true}).action,'wait');
  });
}
test('renderer failure requires hidden, loaded and stalled signals together',()=>{
  const visibility={hidden:true,dom_ready:true,stalled:true,reason:'zero_viewport'};
  assert.equal(policy.decide({visibility}).action,'foreground');
  for(const field of ['hidden','dom_ready','stalled'])assert.equal(policy.decide({visibility:{...visibility,[field]:false}}).action,'none');
});

function pageFixture(initial) {
  let state=initial;
  const actions=[];
  const page={evaluate:async()=>state,wait:async()=>{},cdp:async(method,args)=>actions.push({method,...args})};
  return {page,actions,set:value=>state=value};
}
test('visibility recovery never navigates and always returns to background at completion',async()=>{
  const p=pageFixture({visibility:{hidden:true,dom_ready:true,reason:'zero_viewport'}});
  const c=attentionController(p.page,(foreground,reason)=>p.page.cdp("test-window-control",{foreground,reason}));
  await c.observe();await c.observe();await c.finish();
  assert.equal(p.actions.filter(a=>a.foreground).length,1);
  assert.equal(p.actions.at(-1).foreground,false);
  assert.ok(p.actions.every(a=>a.method==='test-window-control'));
});
test('manual handoff survives call completion and ends when the actual control disappears',async()=>{
  const p=pageFixture({interaction:{visible:true,kind:'captcha'},visibility:{}});
  const c=attentionController(p.page,(foreground,reason)=>p.page.cdp("test-window-control",{foreground,reason}));
  await c.observe();await c.finish();assert.equal(p.actions.length,1);
  p.set({interaction:{visible:false},visibility:{}});
  await c.observe();assert.equal(p.actions.at(-1).foreground,false);
});
test('timeouts and broad risk classification do not cause a foreground fallback',async()=>{
  const p=pageFixture({risk:true,visibility:{hidden:true,dom_ready:true,reason:''}});
  const c=attentionController(p.page,(foreground,reason)=>p.page.cdp("test-window-control",{foreground,reason}));await c.observe();await c.finish();assert.deepEqual(p.actions,[]);
});

async function contentScenario(manual) {
  const messages=[],listeners=[],changes=[];
  let hidden=true,challenge=manual,extractions=0;
  const dialog={innerText:'请拖动滑块完成验证',matches:()=>false,querySelector:()=>null,
    getBoundingClientRect:()=>({width:300,height:100})};
  const document={readyState:'complete',documentElement:{},body:{innerText:''},
    get hidden(){return hidden;},querySelector:()=>null,
    querySelectorAll:selector=>selector.includes('dialog') && challenge?[dialog]:[]};
  const chrome={runtime:{id:'ext',onMessage:{addListener:fn=>listeners.push(fn)},sendMessage:async message=>{
    messages.push(message);
    if(message.kind==='content-attention'){
      hidden=false;changes.push('foreground');
      if(manual)setTimeout(()=>{challenge=false;},20);
      return {action:'foreground',manual};
    }
  }}};
  const context=vm.createContext({chrome,document,location:{hostname:'www.zhipin.com',href:'https://www.zhipin.com/test'},
    getComputedStyle:()=>({display:'block',visibility:'visible'}),
    get innerWidth(){return hidden?0:1280;},get innerHeight(){return hidden?0:800;},
    KRSiteRoutes:{boss:['www.zhipin.com']},KRSites:{boss:{extract:async()=>{
      extractions++;return challenge?{risk:true}:hidden?{}:{content:'same document'};
    }}},AbortController,TextEncoder,setTimeout,clearTimeout,console,
    MutationObserver:class{observe(){}disconnect(){}}});
  vm.runInContext(readFileSync(new URL('../browser_link/extension/visibility.js',import.meta.url),'utf8'),context);
  vm.runInContext(readFileSync(new URL('../browser_link/extension/content.js',import.meta.url),'utf8'),context);
  for(const listener of listeners)listener({kind:'extract',request_id:'test',generation:'g',platform:'boss',operation:'detail',timeout_ms:3000,payload:{}},{id:'ext'});
  const until=Date.now()+4000;
  while(Date.now()<until && !messages.some(m=>m.kind===(manual?'content-attention-resolved':'content-result')))await new Promise(r=>setTimeout(r,20));
  return {messages,changes,extractions};
}
test('actual content loop continues the same DOM after confirmed hidden rendering failure',async()=>{
  const r=await contentScenario(false);
  assert.equal(r.messages.filter(m=>m.kind==='content-attention').length,1);
  assert.equal(r.messages.find(m=>m.kind==='content-result')?.data?.content,'same document');
  assert.ok(r.extractions>=2);
});
test('actual content loop hands over a slider and reports its disappearance without re-extraction',async()=>{
  const r=await contentScenario(true);
  assert.equal(r.messages.filter(m=>m.kind==='content-attention').length,1);
  assert.equal(r.messages.filter(m=>m.kind==='content-attention-resolved').length,1);
  assert.equal(r.extractions,1);
});

// 官方扩展与KR窗口管理之间的归属边界；不需要私有扩展命令。
import {officialWindowController,requireOfficialExtension} from '../src/runtime/opencli_readiness.mjs';
test('official extension accepted and derivative no longer required',()=>{
  assert.doesNotThrow(()=>requireOfficialExtension({extensionVersion:'1.0.26'},'search'));
  assert.throws(()=>requireOfficialExtension({extensionVersion:'1.0.26.2'},'search'),/official/);
});
test('official controller restores title, minimizes only owned window and relinquishes after user adds tab',async()=>{
  let title='Research',tabs=[{id:2,windowId:5,title}],calls=[];
  const page={contextId:'official',evaluate:async code=>{
    if(code.includes('const title=document.title')){const before=title;title=JSON.parse(code.match(/document.title=(".*?");/)[1]);tabs[0].title=title;return before;}
    title=JSON.parse(code.split('document.title=')[1]);tabs[0].title=title;
  }};
  const load=async()=>({sendCommand:async(action,p)=>{
    assert.equal(action,'chrome-call');assert.equal(p.chromeMethod,'tabs.query');return tabs;
  }});
  const helper=async(p,r)=>{calls.push(r);return r.action==='bind'?{handle:9,pid:10}:{};};
  const control=await officialWindowController(load,page,'python',{helper});
  assert.equal(title,'Research');assert.equal(calls[1].foreground,false);
  await control(true);assert.equal(calls.at(-1).foreground,true);
  tabs.push({id:3,windowId:5,title:'User page'});
  const n=calls.length;assert.equal((await control(false)).skipped,true);assert.equal(calls.length,n);
});
test('shared window is never minimized or focused by official controller',async()=>{
  const calls=[];let title='';
  const page={contextId:'official',evaluate:async code=>{calls.push(code);return title;}};
  let n=0;
  const load=async()=>({sendCommand:async()=>++n===1?[{id:1,windowId:3}]:[{id:1},{id:2}]});
  const control=await officialWindowController(load,page,'python',{helper:async()=>assert.fail('must not touch shared window')});
  assert.equal((await control(false)).reason,'kr_work_window_shared');
  assert.equal(calls.length,2);
});
