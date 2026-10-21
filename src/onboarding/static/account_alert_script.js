
function accountDeepLink(){const p=new URLSearchParams(location.hash.split('?')[1]||'').get('profile');if(p){activate('accounts');§('#account-search').value=p;accountFilter='all';renderAccounts();}}
window.addEventListener('hashchange',()=>setTimeout(accountDeepLink,0));

let accountRequest=false,accountFingerprint='',accountFeedback='',accountSnapshot=null,accountFilter='all',accountOffline=false;
const accountBusy=new Set();
function accountStatus(x){
  const state=x.login_status||(x.needs_action?'needs_recovery':'unobserved');
  const labels={verified:'已登录',needs_recovery:'需要恢复',verifying:'正在验证',check_failed:'检查异常',unobserved:'待核验'};
  return {state,label:labels[state]||'待核验'};
}
function renderAccounts(){
  if(!accountSnapshot)return;
  const all=accountSnapshot.accounts||[],query=§('#account-search').value.trim().toLowerCase();
  const matches=x=>accountFilter==='all'||(accountFilter==='recovery'?x.needs_action:accountFilter==='verified'?accountStatus(x).state==='verified':!x.needs_action&&accountStatus(x).state!=='verified');
  const rows=all.filter(x=>matches(x)&&[x.platform_label,x.slot_label,x.display_label,x.account_number,x.profile_id].join(' ').toLowerCase().includes(query));
  const groups=new Map();
  rows.forEach(x=>{const id=x.group_id||'other';if(!groups.has(id))groups.set(id,{id,label:x.group_label||'其他平台',rows:[]});groups.get(id).rows.push(x);});
  const counts={all:all.length,recovery:all.filter(x=>x.needs_action).length,verified:all.filter(x=>accountStatus(x).state==='verified').length,unobserved:all.filter(x=>!x.needs_action&&accountStatus(x).state!=='verified').length};
  Object.entries(counts).forEach(([key,value])=>§('#account-count-'+key).textContent=value);
  document.querySelectorAll('[data-account-filter]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.accountFilter===accountFilter)));
  §('#account-readonly').hidden=!accountSnapshot.read_only;
  §('#account-cards').innerHTML=rows.length?[...groups.values()].sort((a,b)=>b.rows.filter(x=>x.needs_action).length-a.rows.filter(x=>x.needs_action).length).map(group=>
    '<section class="account-group" aria-labelledby="account-group-'+esc(group.id)+'"><div class="account-group-heading"><h3 id="account-group-'+esc(group.id)+'">'+esc(group.label)+'</h3><span>'+group.rows.length+' 个账号</span></div><div class="account-grid">'+group.rows.sort((a,b)=>Number(b.needs_action)-Number(a.needs_action)).map(x=>{
      const status=accountStatus(x),disabled=accountSnapshot.read_only||accountBusy.has(x.profile_id);
      const detail=x.needs_action?(x.reason||'请打开对应窗口，完成登录后验证恢复。'):status.state==='verified'?'最近真实业务通过；状态到期后会重新核验。':status.state==='check_failed'?'检查未完成，请在实际调用时重试。':'暂无认证成功记录，实际调用时会检查。';
      return '<article class="account-card" data-login-status="'+esc(status.state)+'"><div class="account-card-heading"><h4>'+esc(x.platform_label)+(x.slot_label?' <span class="account-slot">'+esc(x.slot_label)+'</span>':'')+'</h4><span class="account-badge '+esc(status.state)+'">'+(accountOffline?'上次：':'')+esc(status.label)+'</span></div><div class="account-identity"><p class="account-name">'+esc(x.display_label)+'</p><p class="account-number">登记编号：<b>'+esc(x.account_number||'未单独登记')+'</b></p></div><div class="account-observation"><p>'+esc(detail)+'</p></div><div class="account-actions"><button type="button" class="button '+(x.needs_action?'account-recover':'')+'" data-account="'+esc(x.profile_id)+'" data-account-action="open" '+(disabled?'disabled':'')+' title="'+(accountSnapshot.read_only?'开发预览只读':x.needs_action?'打开此登记账号的登录窗口':'打开此登记账号的浏览器窗口')+'">'+(x.needs_action?'打开登录窗口':'打开窗口')+'</button><button type="button" class="button account-verify" data-account="'+esc(x.profile_id)+'" data-account-action="verify" '+(disabled?'disabled':'')+' title="'+(accountSnapshot.read_only?'开发预览只读':'检查此登记账号的真实认证状态')+'">验证登录</button>'+(x.needs_action?'<button type="button" class="button" data-account="'+esc(x.profile_id)+'" data-account-action="ack" '+(disabled?'disabled':'')+'>已知晓</button>':'')+'</div></article>';
    }).join('')+'</div></section>').join(''):'<div class="account-empty">'+(all.length?'没有符合条件的账号。':'尚未登记平台账号。')+'</div>';
}
async function refreshAccounts(){
  if(accountRequest)return;accountRequest=true;
  §('#account-refresh').disabled=true;
  const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),6000);
  try{
    const snapshot=await api('/api/account-alerts',undefined,controller.signal);
    const fingerprint=JSON.stringify(snapshot);
    const pending=snapshot.accounts.filter(x=>x.needs_action);
    const banner=§('#account-alert-banner');
    banner.hidden=!pending.length;
    banner.textContent=pending.length?'需要处理 '+pending.length+' 个账号：'+pending.map(x=>x.platform_label+' '+(x.slot_label||'')+' '+x.display_label+(x.account_number?'（'+x.account_number+'）':'')).join('、')+'。点击查看并恢复。':'';
    §('#account-nav-count').hidden=!pending.length;§('#account-nav-count').textContent=pending.length;
    §('#account-status').dataset.state=pending.length?'attention':'connected';
    const message=accountFeedback||(pending.length?'有 '+pending.length+' 个账号需要恢复，已在各分类中优先显示。':'当前没有已记录的账号恢复待办。');
    if(§('#account-status').textContent!==message)§('#account-status').textContent=message;
    accountSnapshot=snapshot;
    if(fingerprint!==accountFingerprint||accountOffline){accountFingerprint=fingerprint;accountOffline=false;renderAccounts();accountDeepLink();}
  }catch(error){accountOffline=true;§('#account-status').dataset.state='offline';§('#account-status').textContent=accountSnapshot?'连接中断，以下为上次记录；请刷新状态重试。':'暂时无法读取账号状态；请刷新状态重试。';renderAccounts();}
  finally{clearTimeout(timeout);accountRequest=false;§('#account-refresh').disabled=false;}
}
§('#account-alert-banner').onclick=()=>{activateView('accounts');accountFilter='recovery';renderAccounts();};
§('#account-refresh').onclick=()=>{accountFeedback='';refreshAccounts();};
§('#account-search').oninput=renderAccounts;
document.querySelectorAll('[data-account-filter]').forEach(b=>b.onclick=()=>{accountFilter=b.dataset.accountFilter;renderAccounts();});
§('#account-cards').onclick=async event=>{
  const button=event.target.closest('[data-account]');if(!button)return;
  const profile=button.dataset.account;if(accountSnapshot?.read_only||accountBusy.has(profile))return;
  accountBusy.add(profile);renderAccounts();
  try{const result=await api('/api/account-recovery',{profile_id:button.dataset.account,action:button.dataset.accountAction});accountFeedback=result.detail||result.status||'操作已提交';§('#account-status').textContent=accountFeedback;accountFingerprint='';await refreshAccounts();}
  catch(error){§('#account-status').dataset.state='offline';§('#account-status').textContent=error.message;}finally{accountBusy.delete(profile);renderAccounts();}
};
refreshAccounts();setInterval(refreshAccounts,2000);
