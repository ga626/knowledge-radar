import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';

const source=readFileSync(new URL('../src/onboarding/static/console.ts',import.meta.url),'utf8');
const helpers=source.slice(source.indexOf('function openDrawer()'),source.indexOf("$('#drawer-close').onclick=closeDrawer;"));
const template=readFileSync(new URL('../src/onboarding/static/page.html',import.meta.url),'utf8');

test('component cards and their guide remain usable after retiring instance settings',()=>{
  const ids=new Set([...template.matchAll(/id="([^"]+)"/g)].map(x=>'#'+x[1]));
  const nodes=new Map();
  const node=id=>{
    assert.ok(ids.has(id),'element must exist: '+id);
    if(!nodes.has(id))nodes.set(id,{classList:{add(){},toggle(){}},textContent:'',innerHTML:''});
    return nodes.get(id);
  };
  let opened=0;
  const context={$:node,esc:String,plans:{},openDrawer(){opened++;}};
  const start=source.indexOf('let componentCatalog=');
  assert.ok(start>=0,'component rendering must remain present');
  runInNewContext(source.slice(start,source.indexOf('function storage(d)',start)),context);
  const item={id:'fixture',label:'组件',status:'missing',description:'说明',boundary:'范围',install_mode:'guided',guide:{steps:['步骤'],official_url:'https://example.org',official_label:'说明'}};
  context.components({groups:[{id:'basic',label:'基础',items:[item]}]});
  assert.match(node('#component-groups').innerHTML,/组件/);
  context.showComponentGuide(item);
  assert.equal(node('#drawer-title').textContent,'组件');
  assert.equal(opened,1);
});

test('configuration and component guides become visible, then close and clear pending secrets',()=>{
  assert.match(template, /id="drawer" aria-hidden="true"/);
  const nodes=new Map();
  const node=id=>{
    if(!nodes.has(id)){
      const classes=new Set(),attrs=new Map(id==='#drawer'?[['aria-hidden','true']]:[]);
      nodes.set(id,{value:'test-only-pending-key',classList:{add:v=>classes.add(v),remove:v=>classes.delete(v),contains:v=>classes.has(v)},
        setAttribute:(k,v)=>attrs.set(k,v),getAttribute:k=>attrs.get(k)});
    }
    return nodes.get(id);
  };
  const context={$:node};runInNewContext(helpers,context);
  context.openDrawer();
  assert.equal(node('#drawer').getAttribute('aria-hidden'),'false');
  assert.equal(node('#drawer').classList.contains('open'),true);
  assert.equal(node('#drawer-bg').classList.contains('open'),true);
  context.closeDrawer();
  assert.equal(node('#drawer').getAttribute('aria-hidden'),'true');
  assert.equal(node('#drawer').classList.contains('open'),false);
  assert.equal(node('#drawer-bg').classList.contains('open'),false);
  assert.equal(node('#drawer-input').value,'');
});

function providerHarness(guide, options={}) {
  const nodes=new Map();
  const node=id=>{if(!nodes.has(id))nodes.set(id,{value:'',checked:false,textContent:''});return nodes.get(id);};
  node('#drawer-input').value=options.value||'';
  node('#drawer-enable').checked=options.enabled??false;
  node('#drawer-connections').value=options.connections||'';
  const calls=[];
  const context={activeGuide:guide,guides:[guide],$:node,URL,
    api:async(path,payload)=>{calls.push({path,payload});if(options.failure)throw Error(options.failure);return {configuration_generation:'new'};},
    refresh:async()=>{},config:async()=>{}};
  const start=source.indexOf('async function saveProvider('),end=source.indexOf("$('#drawer-save').onclick",start);
  runInNewContext(source.slice(start,end),context);
  return {context,calls,node};
}

test('saving a key preserves explicit authority and generation without executing a search',async()=>{
  const h=providerHarness({id:'brave',key:'BRAVE_SEARCH_API_KEY',configuration_generation:'old'}, {value:'test-secret',enabled:false});
  await h.context.saveProvider();
  assert.equal(h.calls.length,1);assert.equal(h.calls[0].path,'/api/config');
  assert.equal(h.calls[0].payload.providers.brave.enabled,false);
  assert.equal(h.calls[0].payload.values.BRAVE_SEARCH_API_KEY,'test-secret');
  assert.equal(h.calls[0].payload.expected_generation,'old');
  assert.equal(h.node('#drawer-input').value,'');
});

test('deleting a key sends an explicit tombstone instead of saving an empty input',async()=>{
  const h=providerHarness({id:'exa',key:'EXA_API_KEY',configuration_generation:'old'},{value:'pending-secret'});
  await h.context.saveProvider(true);
  assert.equal(h.calls[0].payload.delete_keys[0],'EXA_API_KEY');
  assert.equal(Object.keys(h.calls[0].payload.values).length,0);
});

test('concurrent-edit errors remain visible',async()=>{
  const other=providerHarness({id:'ddgs',configuration_generation:'old'},{failure:'配置已被其他窗口修改'});
  await other.context.saveProvider();assert.equal(other.node('#drawer-status').textContent,'配置已被其他窗口修改');
});
