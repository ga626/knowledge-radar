import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = () => fs.readFileSync(new URL('../src/onboarding/static/runtime_status.js', import.meta.url), 'utf8');
const state = {state:'circuit_open', source_identity:'candidate', incident_id:'incident', manual_recovery_allowed:true,
  recovery:{automatic_failures:4,maximum_restarts:3,remaining_restarts:0},last_failure:{code:'unexpected_exit',exit_code:9}};
async function page({store=new Map(), offline=false, receipt='accepted', runtime=state, readOnly=false, rejected=false, receiptOffline=false}={}) {
  const elements = new Map();
  for (const id of ['runtime-card','runtime-state','runtime-progress','runtime-failure','runtime-impact','runtime-detail','runtime-message','runtime-refresh','runtime-recover'])
    elements.set(id,{textContent:'',disabled:false,hidden:false,dataset:{readOnly:String(readOnly)},listeners:{},addEventListener(k,f){this.listeners[k]=f;}});
  const calls = [];
  const context = {document:{getElementById:id=>elements.get(id)},sessionStorage:{getItem:k=>store.get(k)??null,setItem:(k,v)=>store.set(k,v),removeItem:k=>store.delete(k)},
    crypto:{randomUUID:()=> 'intent-1'},AbortController, setTimeout,clearTimeout,
    fetch:async(url,options={})=>{calls.push({url,payload:options.body?JSON.parse(options.body):null});
      if(offline) throw new Error('offline');
      if(receiptOffline && url.endsWith('/receipt')) throw new Error('receipt offline');
      if(rejected && url.endsWith('/recover')) return {ok:false,json:async()=>({status:'rejected',reason:'incident_changed_refresh_status'})};
      return {ok:true,json:async()=>url.endsWith('/recover')?{status:'accepted',recovery_request_id:'intent-1'}:url.endsWith('/receipt')?{status:receipt}:{status:'ok',runtime}};}};
  vm.runInNewContext(source().replaceAll('__TOKEN_JSON__',JSON.stringify('test-token')),context);
  await new Promise(resolve=>setImmediate(resolve));
  return {elements,calls,store,click:id=>elements.get(id).listeners.click()};
}
test('runtime card accepts one intent, preserves pending, never starts on refresh',async()=>{
  const p=await page();
  const first=p.click('runtime-recover');
  const second=p.click('runtime-recover');
  await Promise.all([first,second]);
  assert.equal(p.calls.filter(x=>x.url.endsWith('/recover')).length,1);
  assert.match(p.elements.get('runtime-message').textContent,/接收|等待|确认/);
  await p.click('runtime-refresh');
  assert.equal(p.calls.filter(x=>x.url.endsWith('/recover')).length,1);
  assert.ok(p.store.size);
});
test('reopen reconciles same intent, unknown does not replay',async()=>{
  const store=new Map([['kr-runtime-recovery',JSON.stringify({request_id:'old-intent',expected_source:'candidate',expected_incident:'incident'})]]);
  const p=await page({store,receipt:'unknown'});
  assert.equal(p.calls.filter(x=>x.url.endsWith('/recover')).length,0);
  assert.equal(p.calls.find(x=>x.url.endsWith('/receipt')).payload.request_id,'old-intent');
  assert.ok(p.elements.get('runtime-recover').disabled);
});
test('offline is not ready and preview cannot recover',async()=>{
  const offline=await page({offline:true});
  assert.match(offline.elements.get('runtime-state').textContent,/无法联系/);
  assert.ok(offline.elements.get('runtime-recover').disabled);
  const preview=await page({readOnly:true});
  assert.ok(preview.elements.get('runtime-recover').disabled);
});
test('ready is separate from historical request outcome',async()=>{
  const store=new Map([['kr-runtime-recovery',JSON.stringify({request_id:'old-intent',expected_source:'candidate',expected_incident:'incident'})]]);
  const p=await page({store,receipt:'unknown',runtime:{...state,state:'ready',manual_recovery_allowed:false}});
  assert.match(p.elements.get('runtime-state').textContent,/就绪/);
  assert.match(p.elements.get('runtime-message').textContent,/未知/);
});
test('confirmed rejection is not an unknown accepted intent and can be corrected',async()=>{
  const p=await page({rejected:true});
  await p.click('runtime-recover');
  assert.match(p.elements.get('runtime-message').textContent,/拒绝/);
  assert.equal(p.elements.get('runtime-recover').disabled,false);
  await p.click('runtime-refresh');
  assert.match(p.elements.get('runtime-message').textContent,/拒绝/);
  assert.equal(p.calls.filter(x=>x.url.endsWith('/recover')).length,1);
  const reopened=await page({store:p.store});
  assert.match(reopened.elements.get('runtime-message').textContent,/拒绝/);
});
test('receipt lookup failure does not erase a separately verified ready state',async()=>{
  const store=new Map([['kr-runtime-recovery',JSON.stringify({request_id:'old-intent',expected_source:'candidate',expected_incident:'incident'})]]);
  const p=await page({store,receiptOffline:true,runtime:{...state,state:'ready',manual_recovery_allowed:false}});
  assert.match(p.elements.get('runtime-state').textContent,/就绪/);
  assert.match(p.elements.get('runtime-message').textContent,/未知|确认/);
});
test('fresh page recovers the server-held request after the old tab was closed',async()=>{
  const p=await page({runtime:{...state,recovery:{...state.recovery,request_id:'server-held',result:'unknown'}},receipt:'unknown'});
  assert.equal(p.calls.find(x=>x.url.endsWith('/receipt'))?.payload.request_id,'server-held');
  assert.match(p.elements.get('runtime-message').textContent,/未知/);
  assert.equal(p.calls.filter(x=>x.url.endsWith('/recover')).length,0);
});
