import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const source=readFileSync(new URL('../src/collectors/platform/boss_dom.js',import.meta.url),'utf8');
test('BOSS security redirect is a risk signal even without a dialog',async()=>{
 const document={title:'Please wait',body:{innerText:''},querySelectorAll:()=>[],querySelector:()=>null};
 const site=vm.runInNewContext(source,{document,location:{href:'https://www.zhipin.com/web/passport/zp/security.html',pathname:'/web/passport/zp/security.html'},URL});
 const state=await site.extract('search');
 assert.equal(state.risk,true);assert.equal(state.login,false);assert.equal(state.items.length,0);
});
