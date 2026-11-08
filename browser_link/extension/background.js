/* One auto-connected port per Chrome profile. No debugger or remote JS. */
importScripts('sites/routes.js','visibility.js');
let native=null, ready=false, draining=false, reloadPending=false, reconnectDelay=1000, reconnectBlocked=false;
let generation='', profileId='', profileReady, status={state:'connecting',error:''};
const pending=new Map(), owned=new Map(), locks=new Set();
let ownershipReady=Promise.resolve(), persistWrite=Promise.resolve();
const HOST='com.knowledgeradar.browser_link';
const SITES=globalThis.KRSiteRoutes;
const OPS=new Set(['search','detail','comments','login','attention','probe','auth_verify','release','release_login']);
// Session-only, bounded diagnostics: no page text, query, account or credentials.
let diagnosticEvents=[],diagnosticWrite=Promise.resolve();
function record(p,stage,{code='',host=''}={}) {
  const row={at:Date.now(),request_id:String(p.id || '').slice(0,96),platform:p.platform,
    operation:p.operation || '',tab_id:p.tabId ?? null,stage};
  if(p.documentId)row.document_id=p.documentId;
  if(p.generation)row.generation=p.generation;
  if(code)row.code=/^[a-z_]{1,64}$/.test(code)?code:'operation_failed';
  if(host && SITES[p.platform]?.includes(host))row.host=host;
  diagnosticEvents.push(row);diagnosticEvents=diagnosticEvents.slice(-80);
  const snapshot=diagnosticEvents.map(e=>({...e}));
  diagnosticWrite=diagnosticWrite.catch(()=>{}).then(()=>chrome.storage.session.set({browserDiagnostics:snapshot}));
}
async function diagnostics(platform) {
  const tabs=[];
  for(const [site,item] of owned)if(site===platform){
    let available=false,active=false;
    try{const tab=await chrome.tabs.get(item.id);available=true;active=!!tab.active;}catch{}
    tabs.push({tab_id:item.id,available,active,retained:!!item.retained,observed:!!item.observed});
  }
  return {schema:'kr-browser-diagnostics/v1',implementation_revision:'evidence-attention-v1',events:diagnosticEvents.filter(e=>e.platform===platform),tabs};
}
function ensureProfile() {
  // One persisted identity, including concurrent startup/install/popup events.
  if(!profileReady)profileReady=(async()=>{
    const config=await chrome.storage.local.get('profileId');
    profileId=config.profileId || crypto.randomUUID();
    if(!config.profileId)await chrome.storage.local.set({profileId});
  })().catch(error=>{profileReady=null;throw error;});
  return profileReady;
}
function target(platform,value) {
  try {const u=new URL(value);return u.protocol==='https:' && SITES[platform]?.includes(u.hostname) && !u.username && !u.password;} catch{return false;}
}
function setStatus(state,error='') {
  status={state,error,updated_at:Date.now()};
  void chrome.action.setBadgeText({text:state==='connected' || state==='connecting'?'':'!'}).catch(()=>{});
  void chrome.action.setTitle({title:`KR 浏览器连接器：${state}${error?' · '+error:''}`}).catch(()=>{});
}
function post(port,message) {try{port.postMessage(message);return true;}catch{return false;}}
async function presentAttention(p, signals) {
  if(pending.get(p.id)!==p)return {action:'none'};
  const item=owned.get(p.platform);
  if(!item || !(await ownedTab(item)))return {action:'none'};
  const decision=KRVisibilityPolicy.decide(signals,{presented:!!item.autoAttention});
  if(decision.action!=='foreground')return decision;
  // Record before focusing so our own focus event is not mistaken for takeover.
  item.autoAttention={reason:decision.reason,manual:decision.manual,request_id:p.id,until:Date.now()+300000};
  item.retained=decision.manual;item.retainUntil=item.autoAttention.until;
  await persist();
  if(pending.get(p.id)!==p){await backgroundAttention(p.platform);return {action:'none'};}
  await chrome.tabs.update(item.id,{active:true});
  if(pending.get(p.id)!==p){await backgroundAttention(p.platform);return {action:'none'};}
  await chrome.windows.update(item.windowId,{state:'normal',focused:true});
  // Cancellation can arrive while Chrome is applying the focus operation.
  // Recheck after it resolves so a late focus cannot outlive cancellation.
  if(pending.get(p.id)!==p){
    const current=await ownedTab(item);
    if(current && current.url===item.url)await chrome.windows.update(item.windowId,{state:'minimized'});
    return {action:'none'};
  }
  if(!decision.manual)await chrome.alarms.create('attention:'+p.platform,{when:Date.now()+300000});
  record(p,'attention_presented',{code:decision.reason});
  return decision;
}
async function backgroundAttention(platform) {
  const item=owned.get(platform);if(!item?.autoAttention)return;
  const tab=await ownedTab(item);
  // A user navigation transfers ownership; do not hide their new page.
  if(!tab || tab.url!==item.url)return;
  await chrome.windows.update(item.windowId,{state:'minimized'});
  item.autoAttention=null;item.retained=false;item.retainUntil=0;item.manual=false;
  await persist();record({platform,tabId:item.id},'attention_backgrounded');
}
async function persist(){
  const snapshot=[...owned].map(([site,item])=>[site,{...item}]);
  persistWrite=persistWrite.catch(()=>{}).then(async()=>{
    await chrome.storage.session.set({ownedTabs:snapshot});
    await chrome.storage.local.set({ownedWorkTabs:snapshot});
  });
  await persistWrite;
}
async function ownedTab(item) {
  const tab=await chrome.tabs.get(item.id);
  if(!item.marker || tab.windowId!==item.windowId || tab.groupId!==item.groupId)return null;
  const group=await chrome.tabGroups.get(item.groupId);
  return group.title===item.marker ? tab : null;
}
async function release(platform,force=false,reason='idle_cleanup',requestOwnsLock=false) {
  const item=owned.get(platform);
  if(!item)return {work_page_closed:true,cleanup_reason:'no_owned_page'};
  if(locks.has(platform) && !requestOwnsLock)return {work_page_closed:false,cleanup_reason:'page_in_use'};
  try {
    const tab=await ownedTab(item);if(!tab){owned.delete(platform);await persist();
      return {work_page_closed:false,ownership_relinquished:true,cleanup_reason:'ownership_not_proven'};}
    if((item.manual || item.retained || item.observed) && !force)return {work_page_closed:false,cleanup_reason:'interaction_retained'};
    // URL equality plus session ownership: never close a user-navigated tab.
    if(tab.url===item.url || tab.url==='about:blank'){
      record({platform,tabId:item.id},'tab_close_requested',{code:reason});
      await chrome.tabs.remove(item.id);
    }else{
      owned.delete(platform);await persist();
      return {work_page_closed:false,ownership_relinquished:true,cleanup_reason:'user_navigation'};
    }
  }catch{
    return {work_page_closed:false,cleanup_reason:'work_page_cleanup_failed'};
  }
  if(owned.get(platform)===item){owned.delete(platform);await persist();}
  return {work_page_closed:true,cleanup_reason:reason};
}
function finish(p,ok,data={},code='',notify=true) {
  if(pending.get(p.id)!==p)return;
  pending.delete(p.id);locks.delete(p.platform);clearTimeout(p.timer);
  if(!ok && p.tabId!=null)void chrome.tabs.sendMessage(p.tabId,{kind:'cancel',request_id:p.id},{frameId:0}).catch(()=>{});
  record(p,'request_finished',{code:code || data.failure_type || (ok?'ok':'operation_failed')});
  const item=owned.get(p.platform);if(item){
    item.lastUsed=Date.now();
    if(data.risk || data.login || ['anti_bot_verification','auth_state_unconfirmed','login_required','native_command_timeout'].includes(data.failure_type || code)){item.retained=true;item.retainUntil=Date.now()+300000;}
    void persist();
  }
  if(notify)post(p.port,{type:'result',request_id:p.id,platform:p.platform,generation:p.generation,ok,
    data:{...data,browser_diagnostics:{last_stage:p.lastStage || 'command_received',tab_id:p.tabId,retained:!!item?.retained}},
    code,request_executed:!!p.dispatched || !!p.navigated});
  if(code==='cancelled')void backgroundAttention(p.platform).catch(()=>{});
  if(reloadPending && !pending.size)void finishUpdate();
}
async function finishUpdate() {
  if(pending.size)return;
  const port=native;native=null;ready=false;
  if(port)port.disconnect();
  chrome.runtime.reload();
}
function reconnect(){
  if(draining || reconnectBlocked || native || !profileId)return;
  void chrome.alarms.create('reconnect',{when:Date.now()+reconnectDelay});
  reconnectDelay=Math.min(60000,reconnectDelay*2);
}
async function connect(){
  await ownershipReady;
  try{await ensureProfile();}catch(error){setStatus('disconnected',error.message);return;}
  if(native || draining || reconnectBlocked || !profileId)return;
  setStatus('connecting');
  try {
    const port=chrome.runtime.connectNative(HOST);native=port;
    port.onDisconnect.addListener(()=>{
      const error=chrome.runtime.lastError;
      if(native!==port)return;
      native=null;ready=false;
      if(draining && !reloadPending && !reconnectBlocked)draining=false;
      if(!reconnectBlocked)setStatus('disconnected',error?.message || 'native_host_disconnected');
      for(const p of [...pending.values()])finish(p,false,{},'browser_disconnected',false);
      reconnect();
    });
    port.onMessage.addListener(message=>{
      if(native!==port || !message)return;
      if(message.type==='hello_ack'){
        if(message.protocol!==1){ready=false;reconnectBlocked=true;setStatus('incompatible','protocol_mismatch');native=null;port.disconnect();return;}
        generation=message.generation;ready=true;reconnectDelay=1000;setStatus('connected');return;
      }
      if(message.type==='hello_rejected'){ready=false;draining=true;reconnectBlocked=true;setStatus('blocked',message.code);return;}
      if(message.type==='drain'){draining=true;setStatus('draining');return;}
      if(message.type==='resume' && !reloadPending){draining=false;setStatus('connected');return;}
      if(message.type==='cancel'){const p=pending.get(message.request_id);if(p)finish(p,false,{},'cancelled');return;}
      if(message.type==='command')void execute(message,port);
    });
    post(port,{type:'hello',protocol:1,profile_id:profileId,extension_version:chrome.runtime.getManifest().version});
  }catch(error){native=null;setStatus('disconnected',error.message);reconnect();}
}
async function dispatch(p) {
  if(pending.get(p.id)!==p || p.dispatched)return;
  p.dispatched=true;
  p.lastStage='extract_dispatched';record(p,p.lastStage);
  try{await chrome.tabs.sendMessage(p.tabId,{kind:'extract',request_id:p.id,generation:p.generation,platform:p.platform,operation:p.operation,payload:p.payload,timeout_ms:p.timeout},p.documentId?{documentId:p.documentId}:{frameId:0});}
  catch{finish(p,false,{},'content_unavailable');}
}
async function tabFor(p,existingOnly=false) {
  let item=owned.get(p.platform);
  if(item){
    try{
      const tab=await ownedTab(item);
      if(tab && tab.url===item.url && target(p.platform,tab.url)){
        // The subsequent content observation decides whether an old handoff is resolved.
        record({...p,tabId:tab.id},'tab_reused');return tab;
      }
    }catch{}
    owned.delete(p.platform);await persist();
  }
  if(existingOnly)return null;
  // Independent window in the same Chrome Profile; never append to a user window.
  const observed=p.operation==='login';
  // On Windows, focused:false can override the initial minimized state.
  // Quiet creation must omit focused and geometry, not show then minimize.
  const window=await chrome.windows.create({url:'about:blank',type:'normal',
    ...(observed?{focused:true}:{state:'minimized'})});
  const tab=window.tabs?.[0];
  if(!tab)throw Error('work_window_empty');
  const marker='KR · '+crypto.randomUUID();
  let groupId;
  try {
    groupId=await chrome.tabs.group({tabIds:tab.id,createProperties:{windowId:window.id}});
    await chrome.tabGroups.update(groupId,{title:marker,color:'grey'});
    // 自有空白页先静音再导航，不能等站点开始播放后才补救。
    await chrome.tabs.update(tab.id,{muted:true});
  } catch(error) {
    // Only the exact newly-created blank page can be removed after setup failure.
    try{const current=await chrome.tabs.get(tab.id);if(current.windowId===window.id && current.url==='about:blank')await chrome.tabs.remove(tab.id);}catch{}
    throw error;
  }
  record({...p,tabId:tab.id},'tab_created');
  item={id:tab.id,windowId:window.id,groupId,marker,url:'about:blank',lastUsed:Date.now(),manual:false,observed,retainUntil:observed?Date.now()+300000:0};owned.set(p.platform,item);await persist();
  if(item.observed)await chrome.windows.update(tab.windowId,{state:'normal',focused:true});
  if(pending.get(p.id)!==p){void release(p.platform);return null;}
  return tab;
}
async function execute(message,port) {
  const id=message.request_id, platform=message.platform;
  const reject=code=>post(port,{type:'result',request_id:id,platform,generation:message.generation,ok:false,code,request_executed:false});
  if(!id || pending.has(id))return;
  if(message.generation!==generation)return reject('stale_generation');
  if(draining)return reject('channel_draining');
  if(!SITES[platform] || !OPS.has(message.operation))return reject('unsupported_operation');
  if(message.payload?.url && !target(platform,message.payload.url))return reject('invalid_site_url');
  // Read the local journal only: no tab creation, focus, DOM extraction or navigation.
  if(message.operation==='probe' && message.payload?.diagnostics_only===true){
    return post(port,{type:'result',request_id:id,platform,generation,ok:true,data:await diagnostics(platform),request_executed:false});
  }
  if(locks.has(platform) || pending.size>=2)return reject('browser_busy');
  const p={id,platform,port,generation,operation:message.operation,payload:message.payload || {},dispatched:false,navigated:false,tabId:null,
    timeout:Math.max(1,Math.min(20000,Number(message.timeout_ms)||20000))};
  pending.set(id,p);locks.add(platform);
  record(p,'command_received');
  p.timer=setTimeout(()=>finish(p,false,{},'native_command_timeout'),p.timeout);
  try {
    if(['release','release_login'].includes(p.operation)){
      const item=owned.get(platform);if(item)item.manual=false;
      const cleanup=await release(platform,p.payload.preserve_interaction!==true,'explicit_release',true);
      finish(p,true,{...cleanup,interaction_retained:owned.has(platform)});return;
    }
    const passive=['probe','attention','auth_verify'].includes(p.operation);
    const tab=await tabFor(p,passive);
    if(pending.get(id)!==p)return;
    if(!tab)return finish(p,false,{},'work_tab_unavailable');
    p.tabId=tab.id;
    const item=owned.get(platform);
    if(['login','attention'].includes(p.operation)){
      item.manual=true;item.manualUntil=Date.now()+300000;
      // Open login once; subsequent attention focuses without reloading it.
      if(p.operation==='login'){
        if(!target(platform,p.payload.url))return finish(p,false,{},'invalid_site_url');
        p.target=p.payload.url;
        if(tab.url!==p.payload.url){item.url=p.payload.url;p.navigated=true;p.lastStage='navigation_requested';record(p,p.lastStage);await chrome.tabs.update(tab.id,{url:item.url});}
      }
      await chrome.tabs.update(tab.id,{active:true});await chrome.windows.update(tab.windowId,{state:'normal',focused:true});
      await persist();
      if(p.operation==='attention')return finish(p,true,{manual_wait:true});
      if(!p.navigated)await dispatch(p);
      return;
    }
    const url=p.payload.url;
    if(url && !target(platform,url))return finish(p,false,{},'invalid_site_url');
    p.target=url || tab.url;
    // Probe cannot create, navigate or refresh. Same-URL work reuses the loaded document.
    if(!passive && url && tab.url!==url){
      item.url=url;await persist();if(pending.get(id)!==p)return;
      p.navigated=true;p.lastStage='navigation_requested';record(p,p.lastStage);await chrome.tabs.update(tab.id,{url});
    }
    else await dispatch(p);
  }catch(error){finish(p,false,{},error.message || 'native_command_failed');}
}
chrome.runtime.onMessage.addListener((message,sender,respond)=>{
  if(sender.id!==chrome.runtime.id)return;
  if(!sender.tab){
    if(message?.kind==='native-status'){respond({...status,ready,version:chrome.runtime.getManifest().version});return;}
    if(message?.kind==='native-reconnect'){
      // Recovery is transport-only; never replay a site task or change pairing.
      if(!reconnectBlocked && !draining)void connect();
      respond({...status,ready,version:chrome.runtime.getManifest().version});return;
    }
  }
  if(message?.kind==='content-attention'){
    const p=pending.get(message.request_id);
    if(!p || sender.frameId!==0 || sender.tab.id!==p.tabId || message.generation!==p.generation || message.platform!==p.platform)return;
    void presentAttention(p,message.signals).then(respond,()=>respond({action:'none',error:'attention_failed'}));
    return true;
  }
  if(message?.kind==='content-attention-resolved'){
    const item=owned.get(message.platform);
    if(sender.frameId!==0 || item?.id!==sender.tab.id || generation!==message.generation
        || item.autoAttention?.request_id!==message.request_id)return;
    void backgroundAttention(message.platform).then(()=>respond({ok:true}),()=>respond({ok:false}));
    return true;
  }
  if(message?.kind==='content-ready' && sender.frameId===0){
    for(const p of pending.values()){
      if(p.tabId!==sender.tab?.id || p.platform!==message.platform || !target(p.platform,message.url))continue;
      if(p.dispatched && p.documentId && sender.documentId && p.documentId!==sender.documentId){
        finish(p,false,{},'COMMAND_RESULT_UNKNOWN');continue;
      }
      p.documentId=sender.documentId || p.documentId;
      p.lastStage='content_ready';record(p,p.lastStage);
      let expected=false;
      try{const actual=new URL(message.url),expectedUrl=new URL(p.target);expected=actual.pathname===expectedUrl.pathname && actual.search===expectedUrl.search;}catch{}
      // Redirects are observed by the site parser; never re-navigate to defeat a wall.
      if(expected || target(p.platform,message.url))void dispatch(p);
    }
  }
  if(message?.kind==='content-started'){
    const p=pending.get(message.request_id);
    if(p && sender.tab?.id===p.tabId && sender.frameId===0 && message.platform===p.platform && message.generation===p.generation){
      if(p.documentId && sender.documentId && p.documentId!==sender.documentId)return;
      p.documentId=sender.documentId || p.documentId;
      p.lastStage='content_started';record(p,p.lastStage);
    }
  }
  if(message?.kind==='content-result'){
    const p=pending.get(message.request_id);
    if(!p || sender.tab?.id!==p.tabId || sender.frameId!==0 || message.platform!==p.platform || message.generation!==p.generation)return;
    if(p.documentId && sender.documentId && p.documentId!==sender.documentId)return;
    p.lastStage='content_result';record(p,p.lastStage);
    const item=owned.get(p.platform);if(item && target(p.platform,message.data?.url))item.url=message.data.url;
    const done=()=>finish(p,!!message.ok,message.data || {},message.code || '');
    const attention=owned.get(p.platform)?.autoAttention;
    if(attention && (!attention.manual || message.data?.attention_resolved===true)){
      void backgroundAttention(p.platform).then(done,done);
    } else done();
  }
});
chrome.tabs.onUpdated.addListener((id,change,tab)=>{
  for(const p of pending.values())if(p.tabId===id && change.status){
    let host='';try{host=new URL(tab.url).hostname;}catch{}
    record(p,change.status==='complete'?'navigation_complete':'navigation_loading',{host});
    if(change.status==='loading' && p.dispatched)finish(p,false,{},'COMMAND_RESULT_UNKNOWN');
  }
});
chrome.windows.onFocusChanged.addListener(id=>{
  // A user foregrounding a work window takes it over for a bounded handoff.
  for(const [platform,item] of owned)if(item.windowId===id && !item.observed && !item.manual && !item.autoAttention){
    record({platform,tabId:item.id},'tab_relinquished',{code:'user_took_work_window'});
    owned.delete(platform);void persist();
    for(const p of [...pending.values()])if(p.platform===platform)finish(p,false,{},'user_took_work_window');
  }
});
chrome.tabs.onRemoved.addListener(id=>{
  for(const [platform,item] of owned)if(item.id===id){record({platform,tabId:id},'tab_removed');owned.delete(platform);void persist();}
  for(const p of [...pending.values()])if(p.tabId===id)finish(p,false,{},'browser_disconnected');
});
chrome.alarms.onAlarm.addListener(alarm=>{
  if(alarm.name.startsWith('attention:')){
    const platform=alarm.name.slice(10),item=owned.get(platform);
    if(item?.autoAttention && !item.autoAttention.manual && Date.now()>=item.autoAttention.until)void backgroundAttention(platform);
    return;
  }
  if(alarm.name==='reconnect')void connect();
  if(alarm.name==='idle')for(const [platform,item] of owned){
    if(Date.now()-item.lastUsed>60000)void release(platform);
  }
});
chrome.runtime.onUpdateAvailable.addListener(()=>{
  draining=true;reloadPending=true;setStatus('updating');
  if(!pending.size)void finishUpdate();
});
chrome.runtime.onInstalled.addListener(()=>void connect());
chrome.runtime.onStartup.addListener(()=>void connect());
ownershipReady=(async()=>{
  const session=await chrome.storage.session.get('ownedTabs');
  const journal=await chrome.storage.local.get('ownedWorkTabs');
  for(const [platform,item] of session.ownedTabs || journal.ownedWorkTabs || [])if(SITES[platform]){
    try{if(await ownedTab(item))owned.set(platform,item);}catch{}
  }
  const history=await chrome.storage.session.get('browserDiagnostics');
  diagnosticEvents=(history.browserDiagnostics || []).slice(-80);
  await chrome.alarms.create('idle',{periodInMinutes:1});
})();
void ownershipReady.then(connect);
