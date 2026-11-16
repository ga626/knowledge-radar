import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

test('YouTube expands description, waits for rows, and does not forge completeness or language',async()=>{
 let expanded=false, requested=false, waits=0;
 const visible={getBoundingClientRect:()=>({width:100,height:20})};
 const more={...visible,click:()=>{expanded=true;}};
 const button={...visible,innerText:'Show transcript',click:()=>{requested=true;}};
 const row=text=>({...visible,innerText:text,querySelector:()=>null});
 const document={readyState:'complete',body:{innerText:'video'},title:'Video',
   querySelector:s=>s.includes('#expand')?more:null,
   querySelectorAll:s=>s.includes('ytd-transcript-segment-renderer')?(requested?[row('00:01 First'),...(waits>1?[row('00:02 Second')]:[])]:[]):expanded?[button]:[]};
 const context={document,location:{href:'https://www.youtube.com/watch?v=abc12345678'},URL,KRSites:{},
   KRWaitForChange:async()=>{waits++;}};
 context.globalThis=context;
 vm.runInNewContext(readFileSync(new URL('../browser_link/extension/sites/youtube.js',import.meta.url),'utf8'),context);
 const result=await context.KRSites.youtube.extract('transcript',{url:context.location.href,language:'zh'},new AbortController().signal);
 assert.equal(expanded,true);assert.equal(requested,true);
 assert.match(result.transcript,/First/);assert.match(result.transcript,/Second/);
 assert.equal(result.language,'');assert.equal(result.completeness,'unverified');
 assert.ok(result.transcript_diagnostics.panel_rows>=2);
});
