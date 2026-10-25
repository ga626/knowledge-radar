import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

// Execute the actual connection controller with deterministic network outcomes.
const source=readFileSync(new URL('../src/onboarding/static/console.ts',import.meta.url),'utf8');
const code=source.slice(source.indexOf('      function consoleConnection('),source.indexOf('      setInterval(reconnectConsole,5000);')+'      setInterval(reconnectConsole,5000);'.length);
function harness(state, responses){
  const bar={hidden:state==='ready',dataset:{state}},calls=[];
  let reloads=0,probe;
  runInNewContext(code,{
    token:'current-token',document:{getElementById:()=>bar},AbortSignal,
    location:{reload:()=>reloads++},setInterval:fn=>{probe=fn;},
    fetch:async path=>{calls.push(path);const result=responses.shift();if(result instanceof Error)throw result;return result;},
  });
  return {bar,calls,probe,get reloads(){return reloads;}};
}
const health=()=>({ok:true,json:async()=>({status:'ready'})});
const page=token=>({ok:true,headers:{get:()=>`script-src 'nonce-${token}'`},text:async()=>'<script>const token="safe";</script>'});
test('a recovered service reloads the current page',async()=>{
  const h=harness('offline',[health(),page('current-token')]);await h.probe();
  assert.equal(h.reloads,1);assert.deepEqual(h.calls,['/api/health','/']);
});
test('a denied account never reloads repeatedly',async()=>{
  const h=harness('offline',[health(),{ok:false},health(),{ok:false}]);
  await h.probe();await h.probe();assert.equal(h.reloads,0);assert.equal(h.bar.dataset.state,'session');
});
test('stale page token recovers, while another forbidden action does not loop',async()=>{
  const h=harness('session',[health(),page('current-token'),health(),page('new-token')]);
  await h.probe();assert.equal(h.reloads,0);await h.probe();assert.equal(h.reloads,1);
});
test('a legacy launch gate cannot be mistaken for an authenticated page',async()=>{
  const gate={...page('gate'),text:async()=>'<p>launch required</p>'};
  const h=harness('session',[health(),gate]);await h.probe();assert.equal(h.reloads,0);
});
test('network loss retains the offline notice and never reloads',async()=>{
  const h=harness('ready',[new Error('offline')]);await h.probe();
  assert.equal(h.bar.hidden,false);assert.equal(h.bar.dataset.state,'offline');assert.equal(h.reloads,0);
});

// 执行开发者面板原脚本，覆盖侧栏 replaceState 不产生 hashchange 的真实入口。
const developerCode=readFileSync(new URL('../src/onboarding/static/developer_console.js',import.meta.url),'utf8');
function developerHarness({developer=true,hash='#overview',failure=false}={}){
  const nodes=new Map(),calls=[],nav=new EventTarget();
  const node=id=>{
    if(!nodes.has(id))nodes.set(id,Object.assign(new EventTarget(),{textContent:'',replaceChildren(){},append(){}}));
    return nodes.get(id);
  };
  const responses={
    '/api/dashboard':{installation:{version:'0.1.0a20'},activity:{tasks:{recent_tasks:[]}}},
    '/api/evaluation':{gate:'INCOMPLETE',availability:'missing',cases:[]},
    '/api/health':{status:'ready',role:'stable',generation:2},
    '/api/status':{},
  };
  runInNewContext(developerCode,{
    document:{body:{dataset:{developerView:String(developer)}},querySelector:()=>nav,
      querySelectorAll:()=>[],getElementById:node,createElement:()=>({textContent:''})},
    location:{hash},window:{addEventListener(){},api:async path=>{
      calls.push(path);if(failure)throw Error('offline');return responses[path];
    }},
  });
  return {nodes,calls,nav,node};
}
test('sidebar developer navigation loads real data without a hashchange',async()=>{
  const h=developerHarness();assert.equal(h.calls.length,0);
  h.nav.dispatchEvent(new Event('click'));
  h.nav.dispatchEvent(new Event('click'));
  await new Promise(setImmediate);
  assert.deepEqual(h.calls,['/api/dashboard','/api/evaluation','/api/health','/api/status']);
  assert.equal(h.node('developer-role').textContent,'stable');
  assert.match(h.node('developer-role-note').textContent,/0.1.0a20/);
  assert.equal(h.node('developer-evaluation').textContent,'INCOMPLETE');
  assert.match(h.node('developer-message').textContent,/已读取/);
});
test('a direct developer link loads and reports an actual fetch failure',async()=>{
  const h=developerHarness({hash:'#developer',failure:true});
  await new Promise(setImmediate);
  assert.equal(h.calls.length,4);
  assert.equal(h.node('developer-message').textContent,'offline');
});
test('ordinary users do not fetch developer information',async()=>{
  const h=developerHarness({developer:false,hash:'#developer'});
  h.nav.dispatchEvent(new Event('click'));
  await new Promise(setImmediate);
  assert.deepEqual(h.calls,[]);
});
