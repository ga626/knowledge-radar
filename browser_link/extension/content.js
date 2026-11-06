(() => {
  const requests = new Map();
  const send = message => chrome.runtime.sendMessage(message).catch(() => {});
  const platform = Object.keys(globalThis.KRSiteRoutes).find(key=>globalThis.KRSiteRoutes[key].includes(location.hostname));
  const site = globalThis.KRSites[platform];
  if (!site) return;
  const change = (signal, timeout) => new Promise(resolve => {
    let settled=false;
    const finish = () => {if(settled)return;settled=true;observer.disconnect();clearTimeout(timer);signal.removeEventListener('abort',finish);resolve();};
    const observer=new MutationObserver(finish);
    const timer=setTimeout(finish, timeout);
    observer.observe(document.documentElement, {attributes:true,childList:true,subtree:true,characterData:true});
    signal.addEventListener('abort',finish,{once:true});
    if(signal.aborted)finish();
  });
  globalThis.KRWaitForChange = change;
  globalThis.KRWaitFor = async (selector, signal, timeout) => {
    const deadline=Date.now()+timeout;
    while(!signal.aborted && Date.now()<deadline && !document.querySelector(selector)) await change(signal,Math.min(300,deadline-Date.now()));
  };
  async function attention(message, data, {stalled=false, presented=false}={}) {
    const observed=KRVisibilityPolicy.observe();
    observed.visibility.stalled=stalled;
    const decision=KRVisibilityPolicy.decide(observed,{presented});
    if(decision.action!=='foreground')return decision;
    return await chrome.runtime.sendMessage({kind:'content-attention',request_id:message.request_id,
      generation:message.generation,platform,signals:observed}).catch(()=>({action:'none'}));
  }
  async function watchInteraction(message) {
    const control=new AbortController(),until=Date.now()+300000;
    let clearSince=0;
    while(Date.now()<until){
      const observation=KRVisibilityPolicy.observe();
      if(observation.interaction.visible)clearSince=0;
      else if(!clearSince)clearSince=Date.now();
      else if(Date.now()-clearSince>=600){
        await send({kind:'content-attention-resolved',request_id:message.request_id,generation:message.generation,platform});return;
      }
      await change(control.signal,300);
    }
  }
  async function extract(message, controller) {
    void send({kind:'content-started',request_id:message.request_id,generation:message.generation,platform});
    const signal=controller.signal;
    const deadline=Date.now()+Math.max(1,Math.min(18000,message.timeout_ms || 18000));
    const cancel=()=>controller.abort();const timer=setTimeout(cancel,Math.max(1,deadline-Date.now()));
    try {
      if(message.operation==='login'){
        if(site.login)site.login();
        await send({kind:'content-result',request_id:message.request_id,generation:message.generation,platform,ok:true,data:{manual_wait:true,url:location.href}});
        return;
      }
      let data, presented=false,manualAttention=false;
      const start=Date.now();
      let signature='', stableAt=Date.now();
      do {
        if(signal.aborted)return;
        data=await site.extract(message.operation,message.payload || {},signal);
        if(data.risk || data.login || data.detail_login_required){
          const decision=await attention(message,data,{presented});
          presented ||= decision.action==='foreground';manualAttention=decision.manual===true;
          break;
        }
        const next=JSON.stringify([data.items?.map(x=>x.url),data.url,data.content?.length]);
        if(next!==signature){signature=next;stableAt=Date.now();}
        const ready=site.ready ? site.ready(data,message.operation) : !!(data.items?.length || data.empty || data.content || data.authenticated_ui);
        const singleShot=site.singleShotOps?.includes(message.operation);
        if(!ready && !singleShot && Date.now()-start>=900 && !presented){
          const decision=await attention(message,data,{stalled:true});
          presented=decision.action==='foreground';manualAttention=decision.manual===true;
          if(manualAttention)break;
          // Continue the same DOM wait; never reload, navigate or replay a platform API.
        }
        if(ready && (message.operation!=='search' || singleShot || Date.now()-stableAt>=600))break;
        // A community API operation runs exactly once; never repeat it as a DOM poll.
        if(singleShot)break;
        await change(signal,300);
      } while(Date.now()<deadline);
      if(signal.aborted || requests.get(message.request_id)!==controller)return;
      if(data?.authenticated_ui && !KRVisibilityPolicy.observe().interaction.visible)data.attention_resolved=true;
      const payload={kind:'content-result', request_id:message.request_id, generation:message.generation, platform, ok:true,data};
      if(new TextEncoder().encode(JSON.stringify(payload)).length>900000) throw new Error('content_payload_too_large');
      await send(payload);
      if(manualAttention)void watchInteraction(message);
    } catch(error) {
      if(!signal.aborted) await send({kind:'content-result',request_id:message.request_id,generation:message.generation,platform,ok:false,code:error.message || 'content_failed'});
    } finally {clearTimeout(timer);if(requests.get(message.request_id)===controller)requests.delete(message.request_id);}
  }
  chrome.runtime.onMessage.addListener((message,sender) => {
    if(sender.id!==chrome.runtime.id || !message?.request_id)return;
    if(message.kind==='cancel'){requests.get(message.request_id)?.abort();requests.delete(message.request_id);return;}
    if(message.kind!=='extract' || message.platform!==platform)return;
    requests.get(message.request_id)?.abort();
    const controller=new AbortController();requests.set(message.request_id,controller);
    void extract(message,controller);
  });
  void send({kind:'content-ready',url:location.href,platform});
})();
