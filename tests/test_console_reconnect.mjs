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
