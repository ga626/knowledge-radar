import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source=readFileSync(new URL('../browser_link/extension/background.js',import.meta.url),'utf8');
const event=()=>{const listeners=new Set();return {addListener:f=>listeners.add(f),emit:(...a)=>[...listeners].forEach(f=>f(...a))};};
const tick=async()=>{for(let i=0;i<100;i++)await Promise.resolve();};
async function setup({paired=true,profile='paired-id',startupRace=false,create,sendMessage,stored=[],history=[]}={}){
 const sent=[],content=[],created=[],updated=[],removed=[],alarms=[],windowCreates=[],windowUpdates=[],groups=new Map(),timers=new Map(),tabsById=new Map();
 const storage={ownedTabs:stored,browserDiagnostics:history};let nextId=7,timerId=0,connections=0,reloads=0,errorReads=0;
 const port={onMessage:event(),onDisconnect:event(),postMessage:m=>sent.push(m),disconnect:()=>{port.onDisconnect.emit();}};
 const runtime={id:'ext',onMessage:event(),onUpdateAvailable:event(),onInstalled:event(),onStartup:event(),
  connectNative:name=>{assert.equal(name,'com.knowledgeradar.browser_link');connections++;return port;},
  getManifest:()=>({version:'0.2.1'}),reload:()=>{reloads++;}};
 Object.defineProperty(runtime,'lastError',{get:()=>{errorReads++;return null;}});
 const tabs={onRemoved:event(),onUpdated:event(),
  create:async options=>{created.push(options);const tab=create?await create(options):{id:nextId++,url:options.url,active:false,windowId:1};tabsById.set(tab.id,tab);return tab;},
  get:async id=>{if(!tabsById.has(id))throw Error('not found');return tabsById.get(id);},
  update:async(id,options)=>{updated.push({id,...options});const tab=tabsById.get(id);Object.assign(tab,options);
   if(options.url){const platform=options.url.includes('liepin')?'liepin':options.url.includes('zhihu.com')?'zhihu':'maimai';runtime.onMessage.emit({kind:'content-ready',url:options.url,platform},{id:'ext',frameId:0,tab:{id}},()=>{});}return tab;},
  remove:async id=>{removed.push(id);tabsById.delete(id);tabs.onRemoved.emit(id);},
  sendMessage:async(id,message)=>{content.push({id,...message});if(sendMessage)return sendMessage(id,message);}};
 // Chrome defaults new groups to the current (user) window, moving tabIds.
 tabs.group=async({tabIds,createProperties})=>{const tab=tabsById.get(tabIds);tab.windowId=createProperties?.windowId ?? 1;tab.groupId=tabIds+100;return tab.groupId;};
 const local={paired,profileId:profile};
 const chrome={runtime,tabs,windows:{onFocusChanged:event(),update:async(id,options)=>{windowUpdates.push({id,...options});},create:async options=>{windowCreates.push(options);const tab=await tabs.create({url:options.url,active:options.focused});tab.windowId=tab.id+10;return {id:tab.windowId,tabs:[tab]};}},tabGroups:{get:async id=>groups.get(id),update:async(id,value)=>groups.set(id,value)},storage:{local:{get:async()=>({...local}),set:async values=>Object.assign(local,values)},
  session:{get:async()=>storage,set:async values=>Object.assign(storage,values)}},alarms:{create:async(name,details)=>alarms.push({name,...details}),onAlarm:event()},
  action:{setBadgeText:async()=>{},setTitle:async()=>{}}};
 const context=vm.createContext({chrome,console,URL,Date,Map,Set,Promise,crypto:{randomUUID:()=> 'test-profile'},
  importScripts:(...paths)=>paths.forEach(path=>vm.runInContext(readFileSync(new URL('../browser_link/extension/'+path,import.meta.url),'utf8'),context)),
  setTimeout:f=>{const id=++timerId;timers.set(id,f);return id;},clearTimeout:id=>timers.delete(id)});
 vm.runInContext(source,context);
 if(startupRace){runtime.onInstalled.emit();runtime.onStartup.emit();}
 await tick();port.onMessage.emit({type:'hello_ack',protocol:1,generation:'gen'});
 const command=(id,platform='liepin',operation='search',payload)=>port.onMessage.emit({type:'command',request_id:id,platform,operation,generation:'gen',timeout_ms:10000,
  payload:payload || {url:platform==='liepin'?'https://www.liepin.com/zhaopin/?key=Python':'https://maimai.cn/community/home/recommended'}});
 const result=(id,platform='liepin',data={})=>{const tab=content.find(c=>c.request_id===id)?.id;runtime.onMessage.emit({kind:'content-result',request_id:id,generation:'gen',platform,ok:true,data},
  {id:'ext',frameId:0,tab:{id:tab}},()=>{});};
 return {windowCreates,windowUpdates,groups,sent,content,created,updated,removed,alarms,timers,port,runtime,chrome,local,storage,tabsById,command,result,
  counts:()=>({connections,reloads,errorReads})};
}
test('fresh install auto-connects once without pairing or opening site tabs',async()=>{
 const s=await setup({paired:false,profile:null,startupRace:true});assert.equal(s.counts().connections,1);
 assert.equal(s.local.profileId,'test-profile');assert.equal(s.created.length,0);
 s.runtime.onInstalled.emit();s.runtime.onStartup.emit();await tick();
 for(let i=0;i<4;i++)s.runtime.onMessage.emit({kind:'native-reconnect'},{id:'ext'},()=>{});
 await tick();
 assert.equal(s.counts().connections,1);
});

test('existing profile identity is retained without legacy paired gate',async()=>{
 const s=await setup({paired:false});assert.equal(s.sent.find(m=>m.type==='hello').profile_id,'paired-id');
 assert.equal(s.counts().connections,1);
});

test('identity conflict cannot be overridden by reconnect or startup',async()=>{
 const s=await setup();s.port.onMessage.emit({type:'hello_rejected',code:'paired_profile_mismatch'});
 s.port.onDisconnect.emit();await tick();
 s.runtime.onStartup.emit();s.runtime.onMessage.emit({kind:'native-reconnect'},{id:'ext'},()=>{});await tick();
 assert.equal(s.counts().connections,1);assert.equal(s.created.length,0);
 assert.equal(s.alarms.filter(a=>a.name==='reconnect').length,0);
 let status;s.runtime.onMessage.emit({kind:'native-status'},{id:'ext'},value=>{status=value;});
 assert.equal(status.state,'blocked');assert.equal(status.ready,false);
});

test('protocol mismatch remains terminal without reconnect or task replay',async()=>{
 const s=await setup();s.port.onMessage.emit({type:'hello_ack',protocol:99,generation:'wrong'});
 s.runtime.onStartup.emit();s.runtime.onMessage.emit({kind:'native-reconnect'},{id:'ext'},()=>{});await tick();
 let status;s.runtime.onMessage.emit({kind:'native-status'},{id:'ext'},value=>{status=value;});
 assert.equal(status.state,'incompatible');assert.equal(status.ready,false);
 assert.equal(s.counts().connections,1);assert.equal(s.created.length,0);
});
test('different sites share port and use bounded per-site tabs',async()=>{
 const s=await setup();s.command('a');s.command('b','maimai');s.command('c','liepin');await tick();
 assert.equal(s.created.length,2);assert.equal(s.counts().connections,1);
 assert.equal(s.sent.find(m=>m.request_id==='c').code,'browser_busy');
 s.command('d');await tick();assert.equal(s.sent.find(m=>m.request_id==='d').code,'browser_busy');
 s.result('a');s.result('b','maimai');await tick();assert.equal(s.timers.size,0);
});
test('cancelled and cross-tab or cross-generation results never win',async()=>{
 const s=await setup();s.command('a');await tick();
 s.runtime.onMessage.emit({kind:'content-result',request_id:'a',generation:'gen',platform:'liepin',ok:true},{id:'ext',frameId:0,tab:{id:999}},()=>{});
 assert.equal(s.sent.filter(m=>m.type==='result').length,0);
 s.port.onMessage.emit({type:'cancel',request_id:'a'});s.result('a');await tick();
 assert.equal(s.sent.filter(m=>m.type==='result').length,1);assert.equal(s.sent.at(-1).code,'cancelled');
});
test('same loaded URL reuses document without repeated navigation',async()=>{
 const s=await setup();s.command('a');await tick();s.result('a');await tick();s.command('b');await tick();
 assert.equal(s.created.length,1);assert.equal(s.updated.filter(x=>x.url).length,1);
 assert.equal(s.content.filter(x=>x.kind==='extract').length,2);s.result('b');
});
test('login attention focuses once, no reload; passive probe creates no tab',async()=>{
 const s=await setup();s.command('probe','liepin','probe',{});await tick();assert.equal(s.created.length,0);
 assert.equal(s.sent.find(x=>x.request_id==='probe').code,'work_tab_unavailable');
 s.command('login','liepin','login',{url:'https://www.liepin.com/web/user/'});await tick();s.result('login');await tick();
 s.command('attention','liepin','attention',{});await tick();
 assert.equal(s.created.length,1);assert.equal(s.updated.filter(x=>x.url).length,1);
 assert.equal(s.sent.find(x=>x.request_id==='attention').ok,true);
});

test('unconfirmed authentication retains the diagnostic tab without a successful login',async()=>{
 const s=await setup();s.command('login','zhihu','login',{url:'https://www.zhihu.com/'});await tick();s.result('login','zhihu');await tick();
 s.command('verify','zhihu','auth_verify',{url:'https://www.zhihu.com/'});await tick();
 s.result('verify','zhihu',{authenticated_ui:false,failure_type:'auth_state_unconfirmed'});await tick();
 assert.equal(s.storage.ownedTabs[0][1].retained,true);
 assert.equal(s.sent.find(x=>x.request_id==='verify').data.authenticated_ui,false);
 assert.equal(s.removed.length,0);
});
test('browser disconnect consumes lastError and schedules transport reconnect only',async()=>{
 const s=await setup();s.command('a');await tick();const navigations=s.updated.length;
 s.port.onDisconnect.emit();await tick();assert.ok(s.counts().errorReads);assert.equal(s.timers.size,0);
 assert.ok(s.alarms.some(a=>a.name==='reconnect'));assert.equal(s.updated.length,navigations);
 s.chrome.alarms.onAlarm.emit({name:'reconnect'});await tick();assert.equal(s.counts().connections,2);
});
test('extension update drains inflight work then disconnects and reloads once',async()=>{
 const s=await setup();s.command('a');await tick();s.runtime.onUpdateAvailable.emit();
 assert.equal(s.counts().reloads,0);s.command('b','maimai');await tick();
 assert.equal(s.sent.find(m=>m.request_id==='b').code,'channel_draining');
 s.result('a');await tick();assert.equal(s.counts().reloads,1);
});
test('Host drain and disconnect can reconnect to upgraded generation',async()=>{
 const s=await setup();s.port.onMessage.emit({type:'drain'});s.port.onDisconnect.emit();await tick();
 assert.ok(s.alarms.some(a=>a.name==='reconnect'));
 s.chrome.alarms.onAlarm.emit({name:'reconnect'});await tick();assert.equal(s.counts().connections,2);
});
test('owned tab closed midrequest becomes explicit failure',async()=>{
 const s=await setup();s.command('a');await tick();s.chrome.tabs.onRemoved.emit(7);await tick();
 assert.equal(s.sent.find(m=>m.request_id==='a').code,'browser_disconnected');assert.equal(s.timers.size,0);
});
test('user-navigated tabs are never commandeered',async()=>{
 const s=await setup();s.command('a');await tick();s.result('a');await tick();
 s.tabsById.get(7).url='https://www.liepin.com/web/geek/recommend';
 s.command('b');await tick();assert.equal(s.created.length,2);assert.equal(s.removed.length,0);
});
test('cancel during tab creation cannot navigate after completion',async()=>{
 let created;const s=await setup({create:()=>new Promise(resolve=>{created=resolve;})});
 s.command('a');await tick();s.port.onMessage.emit({type:'cancel',request_id:'a'});created({id:7,url:'about:blank',active:false});await tick();
 assert.equal(s.updated.filter(x=>x.url||x.active).length,0);assert.equal(s.timers.size,0);assert.ok(s.removed.includes(7));
});
test('arbitrary JS and foreign URLs rejected before page actions',async()=>{
 const s=await setup();s.command('script','liepin','evaluate',{source:'document.cookie'});await tick();
 assert.equal(s.sent.find(m=>m.request_id==='script').code,'unsupported_operation');
 s.command('wrong','liepin','detail',{url:'https://example.com/'});await tick();
 assert.equal(s.sent.find(m=>m.request_id==='wrong').code,'invalid_site_url');assert.equal(s.updated.length,0);
});

test('Zhihu routes exact hosts, reuses answer snapshot, and rejects cross-platform URLs',async()=>{
 const s=await setup();const url='https://www.zhihu.com/question/123/answer/1937205528846655537';
 s.command('answer','zhihu','detail',{url});await tick();s.result('answer','zhihu');await tick();
 s.command('part','zhihu','detail',{url,read_part:1,snapshot_id:'snapshot'});await tick();
 assert.equal(s.created.length,1);assert.equal(s.updated.filter(x=>x.url).length,1);
 assert.equal(s.content.filter(x=>x.kind==='extract').length,2);s.result('part','zhihu');await tick();
 s.command('foreign','zhihu','detail',{url:'https://www.liepin.com/job_detail/a.html'});await tick();
 assert.equal(s.sent.find(x=>x.request_id==='foreign').code,'invalid_site_url');
 s.command('article','zhihu','detail',{url:'https://zhuanlan.zhihu.com/p/123'});await tick();
 assert.equal(s.content.at(-1).platform,'zhihu');s.result('article','zhihu');
 const manifest=JSON.parse(readFileSync(new URL('../browser_link/extension/manifest.json',import.meta.url),'utf8'));
 assert.ok(manifest.content_scripts.some(c=>c.js.includes('sites/zhihu.js')&&c.matches.includes('https://zhuanlan.zhihu.com/*')));
 assert.ok(!manifest.permissions.includes('debugger')&&!manifest.permissions.includes('cookies'));
});
test('deadline cancels content and late results are discarded',async()=>{
 const s=await setup();s.command('a');await tick();for(const f of [...s.timers.values()])f();await tick();
 s.result('a');assert.equal(s.sent.filter(m=>m.type==='result').length,1);
 assert.equal(s.sent.at(-1).code,'native_command_timeout');assert.equal(s.content.at(-1).kind,'cancel');
});
test('tab creation and missing content errors settle pending request',async()=>{
 const s=await setup({create:async()=>{throw Error('create_failed');}});s.command('a');await tick();
 assert.equal(s.sent.at(-1).code,'create_failed');assert.equal(s.timers.size,0);
 const x=await setup({sendMessage:async()=>{throw Error('no receiver');}});x.command('a');await tick();
 assert.equal(x.sent.at(-1).code,'content_unavailable');assert.equal(x.timers.size,0);
});

test('unknown outcome preserves work tab and exposes bounded read-only diagnostics',async()=>{
 const s=await setup();s.command('timed','zhihu','search',{url:'https://www.zhihu.com/search?q=private-secret',keyword:'private-secret'});await tick();
 for(const f of [...s.timers.values()])f();await tick();
 const tab=s.storage.ownedTabs[0][1];tab.lastUsed=0;s.chrome.alarms.onAlarm.emit({name:'idle'});await tick();
 assert.equal(s.removed.length,0,'unknown result must remain available for inspection');
 const created=s.created.length,extracted=s.content.length,navigations=s.updated.length;
 s.command('diag','zhihu','probe',{diagnostics_only:true});await tick();
 const report=s.sent.find(x=>x.request_id==='diag');assert.equal(report.ok,true);
 assert.ok(report.data.events.some(x=>x.stage==='tab_created'));
 assert.ok(report.data.events.some(x=>x.stage==='navigation_requested'));
 assert.ok(report.data.events.some(x=>x.stage==='extract_dispatched'));
 assert.ok(report.data.events.some(x=>x.stage==='request_finished'&&x.code==='native_command_timeout'));
 assert.equal(report.data.tabs[0].retained,true);assert.ok(report.data.events.length<=80);
 assert.ok(!JSON.stringify(report.data).includes('private-secret'));
 assert.equal(s.created.length,created);assert.equal(s.content.length,extracted);assert.equal(s.updated.length,navigations);
});

test('legacy observe flag cannot foreground routine work or commandeer user navigation',async()=>{
 const s=await setup();const url='https://www.zhihu.com/search?q=test';
 s.command('visible','zhihu','search',{url,observe:true});await tick();
 assert.equal(s.windowCreates[0].state,'minimized');assert.equal(s.windowUpdates.length,0);
 s.result('visible','zhihu');await tick();
 s.command('same','zhihu','search',{url,observe:true});await tick();assert.equal(s.created.length,1);
 s.result('same','zhihu');await tick();
 s.tabsById.get(7).url='https://www.zhihu.com/question/999';
 s.command('new','zhihu','search',{url,observe:true});await tick();
 assert.equal(s.created.length,2);assert.equal(s.removed.length,0);
});

test('navigation and content acknowledgements survive worker restart without page data',async()=>{
 const s=await setup();s.command('trace','zhihu','search',{url:'https://www.zhihu.com/search?q=secret'});await tick();
 s.chrome.tabs.onUpdated.emit(7,{status:'complete'},{id:7,url:'https://www.zhihu.com/search?q=secret'});
 s.runtime.onMessage.emit({kind:'content-started',request_id:'trace',generation:'gen',platform:'zhihu'},{id:'ext',frameId:0,tab:{id:7}},()=>{});await tick();
 s.result('trace','zhihu');await tick();
 const restored=await setup({history:JSON.parse(JSON.stringify(s.storage.browserDiagnostics))});
 restored.command('diag','zhihu','probe',{diagnostics_only:true});await tick();
 const events=restored.sent.find(x=>x.request_id==='diag').data.events;
 assert.ok(events.some(x=>x.stage==='navigation_complete'));
 assert.ok(events.some(x=>x.stage==='content_started'));assert.ok(events.some(x=>x.stage==='content_result'));
 assert.ok(!JSON.stringify(events).includes('?q='));
});

test('risk page is retained even when transport returned success',async()=>{
 const s=await setup();s.command('risk','zhihu','search',{url:'https://www.zhihu.com/search?q=test'});await tick();
 s.result('risk','zhihu',{risk:true,failure_type:'anti_bot_verification'});await tick();
 s.storage.ownedTabs[0][1].lastUsed=0;s.chrome.alarms.onAlarm.emit({name:'idle'});await tick();
 assert.equal(s.removed.length,0);assert.equal(s.storage.ownedTabs[0][1].retained,true);
 s.command('diagnose','zhihu','probe',{diagnostics_only:true});await tick();
 assert.equal(s.sent.find(x=>x.request_id==='diagnose').request_executed,false);
});

test('diagnostic history stays bounded and foreign acknowledgements are ignored',async()=>{
 const s=await setup();s.command('a');await tick();
 s.runtime.onMessage.emit({kind:'content-started',request_id:'a',generation:'wrong',platform:'liepin'},{id:'ext',frameId:0,tab:{id:7}},()=>{});
 for(let i=0;i<100;i++)s.chrome.tabs.onUpdated.emit(7,{status:'complete'},{id:7,url:'https://www.liepin.com/web/geek/jobs?secret=hidden'});
 await tick();s.command('diag','liepin','probe',{diagnostics_only:true});await tick();
 const data=s.sent.find(x=>x.request_id==='diag').data;
 assert.equal(data.events.length,80);assert.ok(!data.events.some(x=>x.stage==='content_started'));
 assert.ok(!JSON.stringify(data).includes('secret'));
});

test('normal work uses an unfocused independent minimized window and closes only its own page',async()=>{
 const s=await setup();s.command('a');await tick();
 assert.equal(Object.hasOwn(s.windowCreates[0],'focused'),false);assert.equal(s.windowCreates[0].state,'minimized');
 assert.equal(Object.hasOwn(s.windowCreates[0],'width'),false);assert.equal(Object.hasOwn(s.windowCreates[0],'height'),false);
 assert.equal(s.tabsById.get(7).windowId,17); // grouping must not move it to user window 1
 assert.equal(s.updated[0].muted,true);assert.ok(s.updated.slice(1).some(x=>x.url));
 s.result('a');await tick();s.tabsById.get(7).active=true;
 s.command('done','liepin','release',{preserve_interaction:true});await tick();
 assert.deepEqual(s.removed,[7]);assert.equal(s.sent.find(x=>x.request_id==='done').data.work_page_closed,true);
});

test('foreign targets do not create even a blank work window',async()=>{
 const s=await setup();s.command('bad','zhihu','detail',{url:'https://example.org/'});await tick();
 assert.equal(s.windowCreates.length,0);assert.equal(s.created.length,0);
});

test('ownership marker mismatch forbids closing a recycled tab id',async()=>{
 const s=await setup();s.command('a');await tick();s.result('a');await tick();
 s.groups.set(107,{title:'user group'});
 s.command('release','liepin','release',{});await tick();
 assert.equal(s.removed.length,0);assert.equal(s.storage.ownedTabs.length,0);
});

test('user foreground takeover stops pending work and relinquishes cleanup rights',async()=>{
 const s=await setup();s.command('a');await tick();
 s.chrome.windows.onFocusChanged.emit(s.tabsById.get(7).windowId);await tick();
 assert.equal(s.sent.find(x=>x.request_id==='a').code,'user_took_work_window');
 s.command('release','liepin','release',{});await tick();assert.equal(s.removed.length,0);
});

test('normal completion cleanup preserves a real risk handoff',async()=>{
 const s=await setup();s.command('a');await tick();s.result('a','liepin',{risk:true});await tick();
 s.command('done','liepin','release',{preserve_interaction:true});await tick();
 assert.equal(s.removed.length,0);assert.equal(s.sent.find(x=>x.request_id==='done').data.interaction_retained,true);
});

test('cleanup failure remains visible and keeps ownership for a safe later attempt',async()=>{
 const s=await setup();s.command('a');await tick();s.result('a');await tick();
 s.chrome.tabs.remove=async()=>{throw Error('chrome removal failed');};
 s.command('done','liepin','release',{preserve_interaction:true});await tick();
 const receipt=s.sent.find(x=>x.request_id==='done').data;
 assert.equal(receipt.work_page_closed,false);assert.equal(receipt.cleanup_reason,'work_page_cleanup_failed');
 assert.ok(s.storage.ownedTabs.some(([,item])=>item.id===7));
});


const attentionMessage=(s,id,signals)=>s.runtime.onMessage.emit({kind:'content-attention',request_id:id,platform:'liepin',generation:'gen',signals},
 {id:'ext',frameId:0,tab:{id:7}},()=>{});

test('only a concrete visible challenge foregrounds once and resolution returns to background',async()=>{
 const s=await setup();s.command('challenge');await tick();
 const navigations=s.updated.filter(x=>x.url).length;
 attentionMessage(s,'challenge',{risk:true,http_status:403});await tick();assert.equal(s.windowUpdates.length,0);
 attentionMessage(s,'challenge',{interaction:{visible:true,kind:'slider'}});await tick();
 assert.equal(s.windowUpdates.filter(x=>x.focused).length,1);
 s.chrome.windows.onFocusChanged.emit(17);await tick(); // Our own focus event is not user takeover.
 attentionMessage(s,'challenge',{interaction:{visible:true,kind:'slider'}});await tick();
 assert.equal(s.windowUpdates.filter(x=>x.focused).length,1);
 s.result('challenge','liepin',{risk:true});await tick();
 assert.equal(s.windowUpdates.at(-1).focused,true);
 s.runtime.onMessage.emit({kind:'content-attention-resolved',request_id:'challenge',platform:'liepin',generation:'gen'},
  {id:'ext',frameId:0,tab:{id:7}},()=>{});await tick();
 assert.equal(s.windowUpdates.at(-1).state,'minimized');
 assert.equal(s.updated.filter(x=>x.url).length,navigations);
});

test('ordinary hidden renderer repair stays background and records suppression',async()=>{
 const s=await setup();s.command('hidden');await tick();
 attentionMessage(s,'hidden',{visibility:{hidden:true,dom_ready:true,stalled:true,reason:'zero_viewport'}});await tick();
 assert.equal(s.windowUpdates.filter(x=>x.focused).length,0);
 s.result('hidden','liepin',{content:'same page'});await tick();
 assert.equal(s.windowUpdates.length,0);
 assert.equal(s.created.length,1);
 s.command('diagnostics','liepin','probe',{diagnostics_only:true});await tick();
 const diagnostic=s.sent.find(x=>x.request_id==='diagnostics');
 assert.ok(diagnostic.data.events.some(x=>x.stage==='attention_suppressed' && x.code==='visibility_zero_viewport'));
});

test('foreign frame and stale generation cannot trigger presentation',async()=>{
 const s=await setup();s.command('a');await tick();
 for(const [id,gen,frame] of [[99,'gen',0],[7,'old',0],[7,'gen',1]]){
  s.runtime.onMessage.emit({kind:'content-attention',request_id:'a',platform:'liepin',generation:gen,
   signals:{interaction:{kind:'captcha',visible:true}}},{id:'ext',frameId:frame,tab:{id}},()=>{});
 }
 await tick();assert.equal(s.windowUpdates.length,0);
});

test('cancellation returns a foreground handoff to background without replay',async()=>{
 const s=await setup();s.command('a');await tick();
 attentionMessage(s,'a',{interaction:{visible:true,kind:'qr'}});await tick();
 s.port.onMessage.emit({type:'cancel',request_id:'a'});await tick();
 assert.equal(s.windowUpdates.at(-1).state,'minimized');assert.equal(s.created.length,1);
});

test('resolved signal cannot minimize a page the user has navigated elsewhere',async()=>{
 const s=await setup();s.command('a');await tick();
 attentionMessage(s,'a',{interaction:{visible:true,kind:'login'}});await tick();
 s.result('a','liepin',{login:true});await tick();s.tabsById.get(7).url='https://www.liepin.com/user-owned';
 const count=s.windowUpdates.length;
 s.runtime.onMessage.emit({kind:'content-attention-resolved',request_id:'a',platform:'liepin',generation:'gen'},
  {id:'ext',frameId:0,tab:{id:7}},()=>{});await tick();
 assert.equal(s.windowUpdates.length,count);
});

test('cancellation during an unfinished focus cannot leave a late foreground window',async()=>{
 const s=await setup();s.command('race');await tick();
 const original=s.chrome.windows.update;let continueFocus;
 s.chrome.windows.update=async(id,change)=>{
  if(change.focused)await new Promise(resolve=>{continueFocus=resolve;});
  return original(id,change);
 };
 attentionMessage(s,'race',{interaction:{visible:true,kind:'captcha'}});await tick();
 assert.equal(typeof continueFocus,'function');
 s.port.onMessage.emit({type:'cancel',request_id:'race'});await tick();
 continueFocus();await tick();
 assert.equal(s.windowUpdates.at(-1).state,'minimized');
});

test('BOSS is absent from native routes and extension permissions',()=>{
 const manifest=JSON.parse(readFileSync(new URL('../browser_link/extension/manifest.json',import.meta.url),'utf8'));
 assert.ok(!JSON.stringify(manifest).includes('zhipin.com'));
 const ctx=vm.createContext({URL});vm.runInContext(readFileSync(new URL('../browser_link/extension/sites/routes.js',import.meta.url),'utf8'),ctx);
 assert.equal(ctx.KRSiteRoutes.boss,undefined);
});

test('replacement document terminates dispatched context and rejects its late result',async()=>{
 const s=await setup();s.command('race','zhihu','search',{url:'https://www.zhihu.com/search?q=test'});await tick();
 s.runtime.onMessage.emit({kind:'content-started',request_id:'race',generation:'gen',platform:'zhihu'},
  {id:'ext',frameId:0,documentId:'old-doc',tab:{id:7}},()=>{});
 s.chrome.tabs.onUpdated.emit(7,{status:'loading'},{id:7,url:'https://www.zhihu.com/'});await tick();
 s.runtime.onMessage.emit({kind:'content-ready',platform:'zhihu',url:'https://www.zhihu.com/'},
  {id:'ext',frameId:0,documentId:'new-doc',tab:{id:7}},()=>{});
 s.result('race','zhihu');await tick();
 const results=s.sent.filter(x=>x.type==='result'&&x.request_id==='race');
 assert.equal(results.length,1);assert.equal(results[0].code,'COMMAND_RESULT_UNKNOWN');
 assert.equal(s.content.filter(x=>x.kind==='extract').length,1);
});

test('manual handoff survives expiry and idle without background login polling',async()=>{
 const s=await setup();s.command('a');await tick();
 attentionMessage(s,'a',{interaction:{visible:true,kind:'qr'}});await tick();s.result('a','liepin',{login:true});await tick();
 const item=s.storage.ownedTabs[0][1];item.lastUsed=0;item.manualUntil=1;item.retainUntil=1;item.autoAttention.until=1;
 const actions=s.content.length,windows=s.windowUpdates.length;
 s.chrome.alarms.onAlarm.emit({name:'attention:liepin'});s.chrome.alarms.onAlarm.emit({name:'idle'});await tick();
 assert.equal(s.removed.length,0);assert.equal(s.content.length,actions);assert.equal(s.windowUpdates.length,windows);
 assert.ok(!readFileSync(new URL('../browser_link/extension/content.js',import.meta.url),'utf8').includes('watchInteraction'));
});
