const states={
  connected:['已连接','已就绪。KR 会自动使用，无需保持弹窗打开。'],
  connecting:['正在连接','正在连接本机服务，连接完成后即可使用。'],
  disconnected:['正在恢复连接','本机服务暂未连接，正在后台自动重连。'],
  blocked:['浏览器身份冲突','另一个浏览器或 Profile 已绑定。请使用原来安装连接器的浏览器；此处不会自动覆盖绑定。'],
  incompatible:['需要更新组件','扩展与本机服务版本不兼容，请更新 KR 浏览器连接器。'],
  draining:['正在更新组件','当前任务结束后会自动恢复连接。'],
  updating:['正在更新组件','更新后将自动重新连接。']
};
const retry=document.getElementById('reconnect');
const error=document.getElementById('error');
document.getElementById('version').textContent='v'+chrome.runtime.getManifest().version;
let refreshing=false;
async function refresh(kind='native-status'){
  if(refreshing)return;
  refreshing=true;
  try{
    const result=await chrome.runtime.sendMessage({kind});
    if(!result)throw Error('extension_worker_unavailable');
    const state=result.ready && result.state==='connected'?'connected':result.state;
    const copy=states[state] || ['暂时无法连接','请稍后再试；诊断信息可帮助定位连接问题。'];
    document.getElementById('state').textContent=copy[0];
    document.getElementById('description').textContent=copy[1];
    document.getElementById('indicator').dataset.state=state;
    retry.hidden=state!=='disconnected';
    error.textContent=result.error || '';
    document.getElementById('diagnostics').hidden=!result.error;
  }catch(err){
    document.getElementById('state').textContent='暂时无法连接';
    document.getElementById('description').textContent='连接器尚未响应，请稍后重试。';
    document.getElementById('indicator').dataset.state='disconnected';
    retry.hidden=false;
    error.textContent=err.message;
    document.getElementById('diagnostics').hidden=false;
  }finally{refreshing=false;retry.disabled=false;}
}
retry.addEventListener('click',()=>{retry.disabled=true;void refresh('native-reconnect');});
const poll=setInterval(()=>void refresh(),750);
window.addEventListener('pagehide',()=>clearInterval(poll),{once:true});
void refresh();
