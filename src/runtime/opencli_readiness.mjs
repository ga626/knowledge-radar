// 共用扩展连接与页面展示；不包含站点知识，不启动用户浏览器或重启daemon。
import '../../browser_link/extension/visibility.js';
import {execFile} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {randomUUID} from 'node:crypto';
export const visibilityPolicy = globalThis.KRVisibilityPolicy;

export function attentionController(page, controlWindow) {
  let presented=false, manual=false, reason='';
  const control = async (foreground, why) => {
    if (!controlWindow) throw new Error('kr_window_controller_unavailable');
    return controlWindow(foreground, why);
  };
  return {
    async observe({layoutFailedAfterRepair=false,resolvePrior=false}={}) {
      const state=await page.evaluate(`/* kr:visibility */ (${visibilityPolicy.observe.toString()})()`);
      if(!state?.visibility)return {action:'none'};
      // Only a concrete renderer failure gets this extra signal, not empty output/timeouts.
      if(layoutFailedAfterRepair){
        state.visibility.reason='collapsed_layout_after_repair';
        state.visibility.repair_attempted=true;state.visibility.stalled=true;
      } else if(state.visibility.reason){
        // Same-document confirmation after a bounded wait; no navigation or API replay.
        await page.wait({time:0.9,timeout:2});
        const next=await page.evaluate(`/* kr:visibility */ (${visibilityPolicy.observe.toString()})()`);
        if(next?.visibility?.reason===state.visibility.reason)state.visibility.stalled=true;
      }
      const decision=visibilityPolicy.decide(state,{presented});
      if(decision.action==='foreground'){
        const result=await control(true,decision.reason);
        if(result?.skipped)return {action:'none',reason:result.reason};
        presented=true;manual=decision.manual;reason=decision.reason;
      } else if(decision.action==='background'){
        await control(false,'resolved');presented=false;manual=false;
      } else if(resolvePrior && !presented && !state.interaction?.visible){
        // Also clears a handoff from an earlier request, if this lease owns one.
        await control(false,'resolved');
      }
      return decision;
    },
    async finish(){if(presented && !manual){await control(false,'completed');presented=false;}},
    metadata:()=>({presented,manual,reason}),
  };
}
export async function waitForBoundBrowser(load, contextId) {
  const {getDaemonHealth} = await load('dist/src/browser/daemon-transport.js');
  const {waitForBridgeReady} = await load('dist/src/browser/bridge-readiness.js');
  const health = await waitForBridgeReady(async options => {
    const value = await getDaemonHealth(options);
    if (value.status?.daemonVersion && value.status.daemonVersion !== '2.0.2') {
      throw Object.assign(new Error('daemon identity mismatch'), {code:'opencli_daemon_identity_mismatch'});
    }
    return value;
  }, {contextId, timeoutMs:18000});
  if (health.state !== 'ready') {
    throw Object.assign(new Error('bound browser unavailable'), {
      code:health.state === 'profile-required' ? 'opencli_daily_browser_binding_required' : 'browser_disconnected'
    });
  }
  return health.status;
}

export async function presentOwnedPage(load, page, runId) {
  const {sendCommand, releaseSiteSessionLease} = await load('dist/src/browser/daemon-client.js');
  // tabs/list 与tabs/select不调用Runtime.evaluate；人工登录期不得接管DOM。
  const tabs = await page.tabs();
  const target = tabs.find(tab => tab.page === page.getActivePage()) || (tabs.length === 1 ? tabs[0] : undefined);
  if (!target?.page) throw new Error('opencli_attention_page_identity_missing');
  page.setActivePage(target.page);
  const presentation = async () => {
    const native = (method,args) => sendCommand('chrome-call', {
      chromeMethod:method,chromeArgs:args,contextId:page.contextId});
    const active = (await native('tabs.query',[{active:true}])).filter(tab=>tab.url === target.url);
    if (active.length !== 1) return {visible:false,focused:false,source:'chrome_native',reason:'active_page_not_unique'};
    const window = await native('windows.get',[active[0].windowId]);
    return {visible:window.state !== 'minimized',focused:Boolean(window.focused),source:'chrome_native'};
  };
  const before = await presentation();
  if (before.visible && before.focused) return before;
  // 1.0.26复用会话不调用foreground；上游新租约才会聚焦共享容器。
  // 用短命自有空白tab触发其公开API，再选回原target；不重载登录页、不碰用户tab。
  const {BrowserBridge} = await load('dist/src/browser/bridge.js');
  const attention = new BrowserBridge();
  const session = `${page.session}:attention:${runId}`;
  let blank;
  const previousClose = process.env.OPENCLI_CLOSE_OWNED_TAB;
  try {
    blank = await attention.connect({contextId:page.contextId,session,surface:page.surface,
      siteSession:'ephemeral',windowMode:'foreground',idleTimeout:15});
    await blank.newTab();
    await page.selectTab(target.page);
  } finally {
    try {
      if (blank) {
        process.env.OPENCLI_CLOSE_OWNED_TAB = '1';
        await blank.closeWindow();
      }
    } finally {
      if (previousClose === undefined) delete process.env.OPENCLI_CLOSE_OWNED_TAB;
      else process.env.OPENCLI_CLOSE_OWNED_TAB = previousClose;
      await releaseSiteSessionLease({runId,session,surface:page.surface});
      await attention.close();
    }
  }
  return presentation();
}

// 保留官方扩展。窗口策略由KR承担，不再要求派生扩展或私有CDP命令。
export function requireOfficialExtension(status, operation) {
  if (['inventory','release','release_login'].includes(operation)) return;
  if (status.extensionVersion !== '1.0.26') {
    throw Object.assign(new Error('Use the official OpenCLI extension 1.0.26'),
      {code:'opencli_official_extension_required'});
  }
}

export function callWindowHelper(pythonExecutable, request) {
  if (!pythonExecutable) return Promise.reject(new Error('kr_window_python_missing'));
  return new Promise((resolve,reject)=>{
    const child=execFile(pythonExecutable,
      [fileURLToPath(new URL('./browser_bootstrap.py',import.meta.url)),'--presentation'],
      {windowsHide:true,timeout:5000,maxBuffer:8192,encoding:'utf8'},(error,stdout)=>{
        let value;try{value=JSON.parse(stdout);}catch{reject(new Error('kr_window_helper_failed'));return;}
        if(error || value.error)reject(new Error(value.error || 'kr_window_helper_failed'));
        else resolve(value);
      });
    child.stdin.on('error',()=>{});
    child.stdin.end(JSON.stringify(request));
  });
}

export async function officialWindowController(load,page,pythonExecutable,{helper=callWindowHelper,initialBackground=true}={}) {
  const {sendCommand}=await load('dist/src/browser/daemon-client.js');
  const native=(method,args)=>sendCommand('chrome-call',{contextId:page.contextId,chromeMethod:method,chromeArgs:args});
  const marker='KR-work-'+randomUUID().replaceAll('-','');
  // 临时标识只用于定位本租约的窗口；立即恢复原标题，不修改站点正文。
  const prior=await page.evaluate(`(() => {const title=document.title;document.title=${JSON.stringify(marker)};return title;})()`);
  let tab,identity;
  try{
    const matches=await native('tabs.query',[{title:marker}]);
    if(matches.length!==1)throw new Error('kr_work_tab_not_unique');
    tab=matches[0];
    const siblings=await native('tabs.query',[{windowId:tab.windowId}]);
    if(siblings.length!==1 || siblings[0].id!==tab.id)return async()=>({skipped:true,reason:'kr_work_window_shared'});
    identity=await helper(pythonExecutable,{action:'bind',marker});
    if(initialBackground)await helper(pythonExecutable,{action:'present',identity,title:marker,foreground:false});
  }finally{await page.evaluate(`document.title=${JSON.stringify(prior)}`);}
  const control=async foreground=>{
    // 每次收起/前置之前重核归属：用户加页、移动页面后放弃窗口控制。
    const tabs=await native('tabs.query',[{windowId:tab.windowId}]);
    if(tabs.length!==1 || tabs[0].id!==tab.id)return {skipped:true,reason:'kr_work_window_ownership_changed'};
    return helper(pythonExecutable,{action:'present',identity,title:tabs[0].title,foreground});
  };
  return control;
}
