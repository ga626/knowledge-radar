// 驱动只执行站点适配器给出的页面合同；不包含任何平台选择器。
import {pathToFileURL} from 'node:url';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {waitForBoundBrowser,presentOwnedPage,requireOfficialExtension,attentionController,officialWindowController} from './opencli_readiness.mjs';
let input = '';
for await (const chunk of process.stdin) input += chunk;
const request = JSON.parse(input);
const load = file => import(pathToFileURL(path.join(request.root, file)).href);
let bridge, page, runId, releaseSiteSessionLease;
let requestExecuted = false;
let viewportOverride = false;
let verificationClosed = false;
let workClosed = false;
let retainedForInteraction = false;
let output, attention;
const cleanupWarnings = [];
const passiveLogin = request.passiveManualLogin && ['login','attention','release_login'].includes(request.operation);
const isolatedWork = request.isolatedWork && ['search','detail','comments','probe','auth_verify'].includes(request.operation);
// 独立调用不重用扩展租约键：迟到的idle alarm只能清理旧代际。
// 需要跨调用DOM上下文的适配器不启用此合同；人工登录始终保留原交接页。
const session = request.session + (passiveLogin ? ':login' : isolatedWork ? `:work:${randomUUID()}`
  : request.operation === 'auth_verify' ? ':verify' : '');
const siteSession = !isolatedWork && request.manualWait ? 'persistent' : 'ephemeral';
// Python调用总时限60秒；新工作代际的120秒只作为异常退出兜底，正常结束显式关闭。
const idleTimeout = isolatedWork ? 120 : request.idleTimeoutS ?? (['login','attention'].includes(request.operation) ? 300 : 120);
const stop = code => { throw Object.assign(new Error(code), {code}); };
const started = performance.now();
let navigate;
const workPage = ['search','detail','comments','probe','auth_verify'].includes(request.operation);
const ensureViewport = async () => {
  const viewport = await page.evaluate('/* kr:viewport */ ({width:innerWidth,height:innerHeight})');
  if (viewport.width <= 0 || viewport.height <= 0) {
    await page.cdp('Emulation.setDeviceMetricsOverride', {width:1280,height:800,deviceScaleFactor:1,mobile:false});
    viewportOverride = true;
  }
};
try {
  const status = await waitForBoundBrowser(load, request.contextId);
  if (status.daemonVersion !== '2.0.2' || !['1.0.26'].includes(status.extensionVersion)) stop('opencli_identity_mismatch');
  if (request.operation === 'inventory') {
    output = {ok:true, data:{connected:true}};
  } else {
    requireOfficialExtension(status, request.operation);
    const {BrowserBridge} = await load('dist/src/browser/bridge.js');
    const client = await load('dist/src/browser/daemon-client.js');
    releaseSiteSessionLease = client.releaseSiteSessionLease;
    runId = client.generateRunId();
    client.setDaemonRunContext({runId,command:session,access:'read'});
    bridge = new BrowserBridge();
    page = await bridge.connect({contextId:request.contextId,session,surface:'adapter',
      siteSession,windowMode:['login','attention'].includes(request.operation) ? 'foreground'
        : 'background',
      idleTimeout});
    navigate = async url => {
      // Page.goto 即使 waitUntil:none 仍修改页面全局对象。只用原生导航，
      // 等待继续交给上游page.wait；不重试写动作或在target丢失后自动重开。
      const target = page.getActivePage();
      const tabs = await page.tabs();
      if (!target || !tabs.some(tab => tab.page === target)) stop('browser_navigation_interrupted');
      requestExecuted = true;
      const result = await client.sendCommandFull('navigate', {
        contextId:request.contextId,session,surface:'adapter',page:target,url,
        siteSession,windowMode:['login','attention'].includes(request.operation)?'foreground':'background',
        idleTimeout});
      if (result.page !== target) stop('browser_navigation_interrupted');
      page.setActivePage(target);
    };
    if (['release','release_login'].includes(request.operation)) {
      process.env.OPENCLI_CLOSE_OWNED_TAB = '1';
      await page.closeWindow();
      output = {ok:true,data:{released:true,browser_closed:false}};
    } else if (passiveLogin) {
      // 人工页面只使用上游原生tabs操作，绝不goto/exec/CDP/采集。
      // 初次交接丢弃旧采集连接；attention只复用，不重载或自动重开。
      if (request.operation === 'login') {
        await client.sendCommand('close-owned-tab', {session:request.session,surface:'adapter',contextId:request.contextId});
        process.env.OPENCLI_CLOSE_OWNED_TAB = '1';
        await page.closeWindow();
        requestExecuted = true;
        page.setActivePage(await page.newTab(request.url));
      } else {
        const tabs = await page.tabs();
        if (tabs.length !== 1) stop('manual_login_page_closed');
        page.setActivePage(tabs[0].page);
      }
      const presentation = await presentOwnedPage(load,page,runId);
      output = {ok:true,data:{manual_login_pending:true},request_executed:requestExecuted,
        metadata:{elapsed_s:(performance.now()-started)/1000,ownership:'borrowed',presentation,
          manual_page:'native_only',debugger_attached:false}};
    } else {
      // 最小化的KR工作窗口会暴露0x0；先修布局再导航，避免站点反复重排/空DOM。
      if (workPage) {
        // connect只构造Page，不绑定target；必须先绑定本站点自有页，
        // 否则布局、旁听与navigate可能落到不同target，首次请求丢上下文。
        const tabs = await page.tabs();
        if (tabs.length > 1) stop('opencli_session_page_ambiguous');
        page.setActivePage(tabs[0]?.page || await page.newTab());
        await ensureViewport();
      }
      attention=attentionController(page,await officialWindowController(load,page,request.pythonExecutable,
        {initialBackground:!['login','attention'].includes(request.operation)}));
      const captureEnabled = request.capturePattern ? await page.startNetworkCapture(request.capturePattern) : false;
      if (request.url) {
        await navigate(request.url);
        // 上游无网络旁听时会在navigate中detach，导致导航前的布局覆盖丢失。
        if (workPage) await ensureViewport();
      }
      if (request.waitSelector && ['search','detail','comments','auth_verify'].includes(request.operation) && !(request.steps || []).length) {
        try { await page.wait({selector:request.waitSelector,timeout:15}); }
        catch { /* 由适配器判别登录、风控、空结果或结构改变，绝不重试导航。 */ }
      }
      if (request.waitText && ['search','detail'].includes(request.operation)) {
        try { await page.wait({text:request.waitText,timeout:8}); } catch { /* 站点判别正文缺失。 */ }
      }
      if (['search','detail','comments'].includes(request.operation)) requestExecuted = true;
      // Establish renderer visibility before the single business read. Do not
      // replay an expression that may call a platform API after presentation.
      const readiness = await attention.observe();
      if(readiness.action==='foreground' && !readiness.manual)await page.wait({time:0.9,timeout:2});
      let state = await page.evaluate(request.expression);
      await attention.observe({resolvePrior:request.operation==='auth_verify'});
      let stepsExecuted = 0;
      for (const step of (request.steps || []).slice(0,3)) {
        if (state.risk) stop('SECURITY_BLOCK');
        if (state.login) stop('AUTH_REQUIRED');
        // 展开等有副作用的页面动作不能被evaluate导航重试重放。
        const action = await page.evaluateOnce(step.expression);
        if (action?.ready) break;
        if (!action?.clicked && !step.fallbackUrl) break;
        requestExecuted = true; stepsExecuted++;
        if (!action?.clicked) {
          await navigate(step.fallbackUrl);
          if (workPage) await ensureViewport();
        }
        try { await page.wait(step.wait || {time:0.3,timeout:3}); } catch { /* 再观察状态。 */ }
        state = await page.evaluate(request.expression);
        await attention.observe();
      }
      // 仅旁听页面自己产生的响应；不重发接口、不导出请求头或Cookie。
      if (captureEnabled && request.networkExpression && !state.risk && !state.login) {
        const rows = await page.readNetworkCapture({retainIncomplete:false});
        const safe = rows.filter(r => String(r.url || '').includes(request.capturePattern))
          .slice(-10).map(r => ({url:r.url,status:r.status,responsePreview:r.responsePreview,
            responseBodyTruncated:r.responseBodyTruncated}));
        state = {...state,...await page.evaluate(`(${request.networkExpression})(${JSON.stringify(safe)})`)};
      }
      if (request.operation === 'login' && !state.risk && !state.authenticated_ui && request.loginExpression) {
        await page.evaluate(request.loginExpression);
        state = await page.evaluate(request.expression);
        await attention.observe();
      }
      let presentation;
      if (['login','attention'].includes(request.operation)) {
        presentation = await presentOwnedPage(load,page,runId);
      }
      if (state.risk) stop('SECURITY_BLOCK');
      if (state.login && !['login','attention'].includes(request.operation)) stop('AUTH_REQUIRED');
      if (request.operation === 'auth_verify') {
        if (viewportOverride) {
          await page.cdp('Emulation.clearDeviceMetricsOverride', {});
          viewportOverride = false;
        }
        process.env.OPENCLI_CLOSE_OWNED_TAB = '1';
        await page.closeWindow();
        verificationClosed = true;
      }
      output = {ok:true,data:state,request_executed:requestExecuted,
        metadata:{elapsed_s:(performance.now()-started)/1000,ownership:'borrowed',presentation,
          steps_executed:stepsExecuted,
          network_capture:request.capturePattern ? (captureEnabled ? 'enabled' : 'unsupported_dom_fallback') : 'not_requested'}};
    }
  }
} catch(error) {
  if(attention && page){try{await attention.observe();}catch{/* Preserve original failure. */}}
  const code = error.code || (/stale page identity|^Page not found:\s*\S+\s*$/.test(String(error.message || ''))
    ? 'browser_navigation_interrupted' : 'opencli_page_failed');
  const diagnostic = String(error.message || '').replace(/https?:\/\/[^\s)"']+/g,'[url]')
    .replace(/Bearer\s+[^\s,;]+/gi,'[redacted]')
    .replace(/["']?(?:cookie|authorization|api_key|token)["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+)/gi,'[redacted]').slice(0,320);
  output = {ok:false,code,
    request_executed:requestExecuted,diagnostic:{message:diagnostic},metadata:{elapsed_s:(performance.now()-started)/1000}};
  process.exitCode = 1;
} finally {
  if(attention){try{await attention.finish();}catch{cleanupWarnings.push('attention_cleanup_failed');}}
  // 只保留本次已存在的工作页，沿用上游有限idle期限；不刷新、不前置、不重放。
  retainedForInteraction = Boolean(page && request.operation !== 'auth_verify' && ['AUTH_REQUIRED','SECURITY_BLOCK','CAPTCHA_REQUIRED',
    'RATE_LIMITED','COMMAND_RESULT_UNKNOWN'].includes(output?.code));
  try {
    try {
      if (viewportOverride) {
        // CDP会通过resolveTab自动建页；清理时先只读核对，绝不复活已关闭页面。
        try {
          const target = page?.getActivePage();
          if (target && (await page.tabs()).some(tab => tab.page === target)) {
            await page.cdp('Emulation.clearDeviceMetricsOverride', {});
          } else cleanupWarnings.push('viewport_target_gone');
        } catch { cleanupWarnings.push('viewport_cleanup_failed'); }
      }
      if (page && (isolatedWork || request.operation === 'auth_verify') && !verificationClosed && !retainedForInteraction) {
        process.env.OPENCLI_CLOSE_OWNED_TAB = '1';
        try { await page.closeWindow(); workClosed = true; } // 只关本调用的自有标签，不留可复用空白页。
        catch { cleanupWarnings.push(isolatedWork ? 'work_page_cleanup_failed' : 'verification_cleanup_failed'); }
      }
    } finally {
      try { if (runId) await releaseSiteSessionLease?.({runId,session,surface:'adapter'}); }
      catch { cleanupWarnings.push('lease_release_failed'); }
    }
  } finally {
    try { await bridge?.close(); } catch { cleanupWarnings.push('bridge_close_failed'); }
  }
}
if(attention)output.metadata={...output.metadata,attention:attention.metadata()};
if (cleanupWarnings.length) {
  output.metadata = {...output.metadata,cleanup_warnings:cleanupWarnings};
}
if (isolatedWork) {
  output.metadata = {...output.metadata,work_session:session,isolated_work:true,
    work_page_closed:workClosed || verificationClosed,retained_for_interaction:retainedForInteraction,
    ...(retainedForInteraction?{retention_max_s:idleTimeout,automatic_retry:false}: {})};
}
process.stdout.write(JSON.stringify(output));
