// KR leases resources; OpenCLI owns the driver and its native waits.
import { pathToFileURL } from 'node:url';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import {waitForBoundBrowser,presentOwnedPage,requireOfficialExtension,attentionController,officialWindowController,ensureOwnedViewport,navigateOwnedPage} from './opencli_readiness.mjs';
let input = '';
for await (const chunk of process.stdin) input += chunk;
const request = JSON.parse(input);
const load = file => import(pathToFileURL(path.join(request.root, file)).href);
let data = request.checkpoint || {};
let bridge, page, releaseSiteSessionLease, runId, inspectSignals;
let requestExecuted = false;
let viewportOverride = false;
let attention, output, workClosed=false, retainedForInteraction=false;
const cleanupWarnings=[];
let stage = 'connect';
const session = 'kr:xiaohongshu';
const stop = code => { throw Object.assign(new Error(code), {code}); };
const timings = {};
const runnerStarted = performance.now();
const measure = async (name, action) => {
  const started = performance.now();
  stage = name;
  try { return await action(); }
  finally { timings[name] = (performance.now() - started) / 1000; }
};
try {
  const {fetchDaemonStatus} = await load('dist/src/browser/daemon-transport.js');
  if (request.operation === 'inventory') {
    const status = await fetchDaemonStatus();
    output={ok:true,data:status || {extensionConnected:false,profiles:[]}};
  } else {
    const status = await waitForBoundBrowser(load, request.contextId);
    if (status.daemonVersion !== '2.0.2') stop('opencli_daemon_identity_mismatch');
    if (!['1.0.26'].includes(status.extensionVersion)) stop('opencli_extension_identity_mismatch');
    requireOfficialExtension(status, request.operation);
    const risk = await readFile(path.join(request.root, 'clis/xiaohongshu/risk-control.js'), 'utf8');
    if (!/retryOnBlock\s*=\s*false/.test(risk)) stop('opencli_fail_fast_patch_missing');
    const {BrowserBridge} = await load('dist/src/browser/bridge.js');
    const client = await load('dist/src/browser/daemon-client.js');
    releaseSiteSessionLease = client.releaseSiteSessionLease;
    runId = client.generateRunId();
    client.setDaemonRunContext({runId,command:'kr:xiaohongshu',access:'read'});
    bridge = new BrowserBridge();
    page = await measure('bridge_connect_s', () => bridge.connect({contextId:request.contextId,session,surface:'adapter',
      // Stable KR session key reuses the work tab. Persistent adapter sessions
      // disable upstream idle expiry, even with an explicit idleTimeout.
      siteSession:'ephemeral',windowMode:request.operation === 'login' ? 'foreground'
        : 'background',
      idleTimeout:request.idleTimeoutS ?? (request.operation === 'login' ? 300 : 120)}));
    if (['search','detail','login'].includes(request.operation)) {
      // Page.connect只构造通道。先绑定本站点工作页，布局诊断/CDP/导航必须同一target。
      const tabs = await page.tabs();
      if (tabs.length > 1) stop('opencli_session_page_ambiguous');
      page.setActivePage(tabs[0]?.page || await page.newTab());
    }
    if(request.operation!=='release')attention=attentionController(page,await officialWindowController(load,page,request.pythonExecutable,
        {initialBackground:!['login','attention'].includes(request.operation)}));
    const signalJs = `(() => {
      const body = document.body?.innerText || '';
      const url = location.href;
      const scope = document.querySelector('#noteContainer');
      const bodyPresent = Boolean(scope?.querySelector('#detail-desc,.note-text'));
      const wall = [...document.querySelectorAll('[role="dialog"],.login-container,.captcha-container,.verify-container')]
        .filter(e => e.getBoundingClientRect().width > 0).map(e=>e.innerText || '').join(' ');
      const captcha = [...document.querySelectorAll('.captcha-container,.verify-container')]
        .some(e => e.getBoundingClientRect().width > 0);
      const risk = captcha || /安全验证|安全限制|访问频繁|请求太频繁|访问频次异常|访问链接异常|滑块验证|拖动滑块/.test(wall || (!bodyPresent ? body : ''))
        || /website-login\\/error|error_code=300017|error_code=300031/.test(url);
      const mine = [...document.querySelectorAll('a[href*="/user/profile/"]')]
        .some(a => (a.innerText || '').trim() === '我');
      const login = !mine && !bodyPresent && (Boolean(document.querySelector('#login-btn'))
        || /登录后查看|登录后推荐|请登录|扫码登录|登录探索更多/.test(body));
      return {authenticated_ui:mine,risk,login,path:location.pathname};
    })()`;
    const signals = async () => {
      const readiness=await attention.observe({resolvePrior:['session_probe','resume'].includes(request.operation)});
      if(readiness.action==='foreground' && !readiness.manual)await page.wait({time:0.9,timeout:2});
      const state = await page.evaluate(signalJs);
      if (state.risk) stop('SECURITY_BLOCK');
      return state;
    };
    inspectSignals = signals;
    const ensureViewport = async (requireLayout) => {
      if (['search','detail'].includes(request.operation)) {
        // 先登记恢复责任；异常路径也会清理本工作页的模拟覆盖。
        viewportOverride = true;
        await ensureOwnedViewport(page, attention, {requireLayout});
      }
    };
    page.goto = async (...args) => {
      stage = 'navigate';
      // Masonry measures its columns during hydration; repair before loading.
      await ensureViewport(false);
      requestExecuted = true;
      await navigateOwnedPage(page,client,{url:args[0],contextId:request.contextId,session,surface:'adapter',
        siteSession:'ephemeral',windowMode:request.operation==='login'?'foreground':'background',
        idleTimeout:request.idleTimeoutS ?? (request.operation==='login'?300:120)});
      await page.wait({time:0.5,timeout:2});
      await ensureViewport(true);
      const state = await signals();
      if (state.login && request.operation !== 'login') stop('AUTH_REQUIRED');
      stage = request.operation;
    };
    if (request.operation === 'release') {
      process.env.OPENCLI_CLOSE_OWNED_TAB = '1';
      await page.closeWindow();
      data = {released:true,ownership:'borrowed',browser_closed:false};
    } else if (request.operation === 'session_probe' || request.operation === 'resume') {
      data = await signals();
      if (!data.authenticated_ui) stop(data.login ? 'AUTH_REQUIRED' : 'auth_state_unknown');
    } else if (request.operation === 'login') {
      await page.goto('https://www.xiaohongshu.com/explore');
      const state = await signals();
      if (!state.authenticated_ui) {
        // Only activate the ordinary login control. Never enter credentials or verification.
        await page.evaluate(`(() => {
          const button = [...document.querySelectorAll('button,.login-btn')]
            .find(e => /^登录$/.test((e.innerText || '').trim()));
          button?.click(); return Boolean(button);
        })()`);
      }
      data = {login_opened:true,...state,presentation:await presentOwnedPage(load,page,runId)};
    } else if (request.operation === 'inspect') {
      data = {tabs:await client.sendCommand('tabs',{op:'list',session,surface:'adapter',contextId:request.contextId})};
      data.tabs = data.tabs.map(tab => ({id:tab.id,page:tab.page,title:tab.title,url:String(tab.url || '').split('?')[0]}));
    } else if (request.operation === 'search') {
      const {command} = await load('clis/xiaohongshu/search.js');
      data = await measure('search_s', () => command.func(page,{query:request.keyword,limit:request.limit,sort:'comprehensive',
        'note-type':['image','video'].includes(request.search_type)?request.search_type:'all',
        'publish-time':'anytime',scope:'all',location:'all'}));
      const state = await signals();
      if (state.login) stop('AUTH_REQUIRED');
      await page.evaluate(`(() => {
        const rows = ${JSON.stringify(data)}, now = Date.now();
        let previous = {};
        try { previous = JSON.parse(sessionStorage.getItem('kr:signed-notes') || '{}'); } catch {}
        const kept = Object.fromEntries(Object.entries(previous).filter(([,v])=>now-v.at<600000));
        for (const row of rows) {
          const id = row.url?.match(/\\/([a-f0-9]{24})(?:[?/#]|$)/)?.[1];
          if (id && row.url.includes('xsec_token=')) kept[id]={url:row.url,at:now};
        }
        try { sessionStorage.setItem('kr:signed-notes',JSON.stringify(Object.fromEntries(Object.entries(kept).slice(-64)))); } catch {}
        return true;
      })()`);
    } else if (request.operation === 'detail') {
      const targetId = request.url.match(/\/([a-f0-9]{24})(?:[?/#]|$)/)?.[1];
      if (!targetId) stop('invalid_xhs_note_id');
      data.note_id = targetId;
      data.completed_stages ||= [];
      if (!request.url.includes('xsec_token=')) {
        stage = 'signed_link_resolution';
        const signed = await page.evaluate(`(() => {
          const id = ${JSON.stringify(targetId)};
          // A new/expired owned page may still be opaque about:blank. It is not
          // a broken browser connection, and must not trigger a navigation retry.
          let cache;
          try { cache = JSON.parse(sessionStorage.getItem('kr:signed-notes') || '{}')[id]; } catch {}
          if (cache && Date.now()-cache.at<600000) return cache.url;
          return [...document.querySelectorAll('a[href]')].map(a=>a.href)
            .find(h=>h.includes(id)&&h.includes('xsec_token=')) || '';
        })()`);
        if (!signed) stop('signed_link_expired_search_again');
        request.url = signed;
      }
      // Reuse the same note page for the upstream comments/media collectors.
      // Delegate waits/extraction to OpenCLI; do not implement a polling driver.
      const goto = page.goto.bind(page);
      let loaded = false;
      page.goto = async (url, ...rest) => {
        if (loaded && url === request.url) {
          const state = await signals();
          if (state.path.includes(targetId)) return;
          stop('page_identity_changed');
        }
        await goto(url,...rest); loaded = true;
        const state = await signals();
        if (state.login) stop('AUTH_REQUIRED');
      };
      const {readXhsDetailPage} = await load('clis/xiaohongshu/risk-control.js');
      if (!data.completed_stages.includes('body')) {
        const {NOTE_EXTRACT_JS} = await load('clis/xiaohongshu/note.js');
        const body = await measure('body_s', () => readXhsDetailPage(page,{url:request.url,extractJs:NOTE_EXTRACT_JS,retryOnBlock:false}));
        if (body.loginWall) stop('AUTH_REQUIRED');
        if (body.notFound) stop('dead_link');
        if (!body.author && !body.desc) stop('empty_detail');
        Object.assign(data,{title:body.title || '',author:body.author || '',content:body.desc || '',
          desc:body.desc || '',likes:body.likes,comment_count:body.comments});
        data.completed_stages.push('body');
      }
      if (request.include_comments && !data.completed_stages.includes('comments') && /^\s*0\s*$/.test(String(data.comment_count))) {
        data.comments = [];
        data.comments_coverage = 'observed_zero_comments';
        data.completed_stages.push('comments');
      }
      if (request.include_comments && !data.completed_stages.includes('comments')) {
        const {buildCommentsExtractJs,assertCommentsLoaded} = await load('clis/xiaohongshu/comments.js');
        // 只增强已审核的上游DOM抽取结果；不新增签名API，不按昵称推父ID。
        const evaluate = page.evaluate.bind(page);
        let observed;
        page.evaluate = async js => {
          if (typeof js === 'string' && js.includes('const results = []') && js.includes('.parent-comment')) {
            js = js.replace('const results = []', `const readNativeCommentId = el => {
              const named = el?.getAttribute('data-comment-id');
              if (named && /^[a-f0-9]{24}$/i.test(named)) return named;
              return el?.id?.match(/^comment[-_]?([a-f0-9]{24})$/i)?.[1] || null;
            }; let krRootIndex = -1; const results = []`)
              .replace('results.push({ author, authorHrefRaw, text,',
                'krRootIndex++; results.push({ root_index:krRootIndex, native_comment_id:readNativeCommentId(item), author, authorHrefRaw, text,')
              .replace('results.push({ author: sAuthor,',
                'results.push({ root_index:krRootIndex, native_comment_id:readNativeCommentId(sub), author: sAuthor,');
            const budget = Math.max(1,Math.min(200,request.comment_budget ?? 100));
            js = js.replace('for (const p of parents)', 'for (const p of Array.from(parents).slice(0,limitCount))')
              .replace('// Extract nested replies', `if (results.length >= ${budget}) break; // Extract nested replies`)
              .replace(".forEach(sub => {", `.forEach(sub => { if (results.length >= ${budget}) return;`)
              .replace('return { pageUrl: location.href, securityBlock, loginWall, notFound, commentTotal, skippedEmpty, results }',
                `return { krBudgetReached:results.length >= ${budget}, pageUrl:location.href, securityBlock, loginWall, notFound, commentTotal, skippedEmpty, results }`);
            observed = await evaluate(js);
            return observed;
          }
          return evaluate(js);
        };
        try {
          const replyLimit=request.comment_reply_limit ?? 10;
          const rounds=request.comment_max_pages ?? 5;
          let extractJs=buildCommentsExtractJs(replyLimit>0,request.comment_limit)
            .replace('i < 60','i < '+rounds)
            .replace('round < 3','round < '+Math.min(3,rounds))
            .replace('if (!text) {','if (!text && !images.length) {')
            .replace('if (!sText) return','if (!sText && !sImages.length) return')
            .replace("p.querySelectorAll('.reply-container .comment-item-sub, .sub-comment-list .comment-item').forEach(sub => {",
              `Array.from(p.querySelectorAll('.reply-container .comment-item-sub, .sub-comment-list .comment-item')).slice(0,${replyLimit}).forEach(sub => {`);
          const snapshot=await measure('comments_s',()=>readXhsDetailPage(page,{url:request.url,extractJs,retryOnBlock:false}));
          if(snapshot.securityBlock)stop('SECURITY_BLOCK');
          if(snapshot.loginWall)stop('AUTH_REQUIRED');
          if(snapshot.notFound)stop('dead_link');
          data.comments=snapshot.results || [];
          assertCommentsLoaded(snapshot,data.comments.length,'xiaohongshu/comments');
          if (observed?.results) {
            data.comments = data.comments.map((row,index) => {
              const raw = observed.results[index];
              if (!raw || raw.text !== row.text || Boolean(raw.is_reply) !== Boolean(row.is_reply)) return row;
              return {...row,root_index:raw.root_index,native_comment_id:raw.native_comment_id};
            });
            data.comments_observation = {skipped_empty:observed.skippedEmpty || 0, observed_total:observed.commentTotal,
              extractor:'opencli_dom', exhausted_proven:false};
            data.comments_observation.expanders_remaining = await evaluate(`(() =>
              [...document.querySelectorAll('.parent-comment button,.parent-comment [role="button"],.parent-comment span')]
                .filter(e=>e.getBoundingClientRect().width>0 && /^(展开|查看更多回复|查看.*回复|共\\d+条回复)/.test((e.innerText||'').trim())).length)()`);
          }
        } finally { page.evaluate = evaluate; }
        // 总预算限制返回；未展完回复/全站覆盖绝不宣称complete。
        const budget = request.comment_budget ?? 100;
        data.comments_budget_truncated = data.comments.length > budget || Boolean(observed?.krBudgetReached);
        data.comments = data.comments.slice(0,budget);
        data.comments_coverage='bounded_top_level_with_replies';
        data.completed_stages.push('comments');
      }
      if (!data.completed_stages.includes('media')) {
        const {buildDownloadExtractJs} = await load('clis/xiaohongshu/download.js');
        const media = await measure('media_discovery_s', async () => {
          // 上游旧提取会丢整段query；保留实际页面提供的完整URL。
          const upstream = buildDownloadExtractJs(targetId)
            .replace("let src = raw.split('?')[0];", "let src = raw;")
            .replace("src = src.replace(/\\/imageView\\d+\\/\\d+\\/w\\/\\d+/, '');", "");
          const extractJs = `(() => {
            const result = ${upstream.trim().replace(/;$/, '')};
            const entry = window.__INITIAL_STATE__?.note?.noteDetailMap?.[${JSON.stringify(targetId)}];
            const note = entry?.note || entry;
            let imageIndex = 0, videoIndex = 0;
            const playbackDuration = document.querySelector('video')?.duration;
            for (const row of result.media || []) {
              if (row.type === 'image') {
                row.image_index = imageIndex;
                const original = note?.imageList?.[imageIndex++];
                if (typeof original?.fileId === 'string') row.native_file_id = original.fileId;
                row.role = note?.type === 'video' ? 'video_cover' : 'content_image';
                row.rendition = row.url.split('!')[1]?.split('?')[0] || 'page_default';
              } else if (row.type === 'video') {
                row.representation_index = videoIndex++;
                row.role = 'video_representation';
                row.representation_selection = 'first_observed_playable_candidate';
                if (Number.isFinite(playbackDuration) && playbackDuration > 0) row.expected_duration_s = playbackDuration;
              }
            }
            return result;
          })()`;
          // Body/comments already waited for this exact note. Its media extractor
          // reads rendered state; don't repeat a navigation settle on a ready page.
          if (loaded) {
            const state = await signals();
            if (state.login) stop('AUTH_REQUIRED');
            if (!state.path.includes(targetId)) stop('page_identity_changed');
            return page.evaluate(extractJs);
          }
          return readXhsDetailPage(page,{url:request.url,extractJs,retryOnBlock:false});
        });
        if (media.securityBlock) stop('SECURITY_BLOCK');
        if (media.loginWall) stop('AUTH_REQUIRED');
        data.media_manifest=media.media || [];
        data.completed_stages.push('media');
      }
      if (!data.content && !data.media_manifest?.length) stop('empty_detail');
    } else stop('unsupported_operation');
    timings.runner_work_s = (performance.now()-runnerStarted)/1000;
    output={ok:true,data,metadata:{browser_timings:timings},request_executed:requestExecuted};
  }
} catch (error) {
  let code = error.code || 'opencli_failed';
  // Classify a failed native wait from the visible page; never retry navigation.
  if (inspectSignals && !['SECURITY_BLOCK','CAPTCHA_REQUIRED','RATE_LIMITED'].includes(code)) {
    try {
      const state = await inspectSignals();
      if (state.login) code = 'AUTH_REQUIRED';
    } catch (signalError) { code = signalError.code || code; }
  }
  // Retain actionable, allowlisted upstream reasons, never raw URLs/tokens.
  const message = String(error.message || '');
  if(attention){try{await attention.observe({layoutFailedAfterRepair:viewportOverride && /masonry remained collapsed|visible browser page/.test(message)});}catch{/* Never hide original error. */}}
  const reason = message.match(/search_input_not_available|search_input_not_filled|ambiguous_search_input|ambiguous_or_missing_search_button|search_button_disabled|filter_panel_not_found|ambiguous_filter_trigger|ambiguous_filter_panel|filter_option_not_found|filter_option_not_active|filter_layout_mismatch|filter_group_not_found/)?.[0]
    || (/Unexpected Xiaohongshu.*payload shape/.test(message) ? 'unexpected_search_payload'
      : /search filter/.test(message) ? 'search_filter_not_applied'
      : /masonry remained collapsed/.test(message) ? 'search_layout_collapsed'
      : /visible browser page/.test(message) ? 'browser_page_not_visible' : code);
  const safeMessage = message.replace(/https?:\/\/[^\s)"']+/g,'[url]')
    .replace(/["']?(?:xsec_token|cookie|authorization|api_key|token)["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+)/gi,'[redacted]')
    .replace(/Bearer\s+[^\s,;]+/gi,'[redacted]').slice(0,320);
  timings.runner_work_s = (performance.now()-runnerStarted)/1000;
  output={ok:false,code,data,metadata:{browser_timings:timings},diagnostic:{stage,reason,message:safeMessage},request_executed:requestExecuted};
  process.exitCode = 1;
} finally {
  if(attention){try{await attention.finish();}catch{cleanupWarnings.push('attention_cleanup_failed');}}
  retainedForInteraction = Boolean(page && ['AUTH_REQUIRED','SECURITY_BLOCK','CAPTCHA_REQUIRED',
    'RATE_LIMITED','COMMAND_RESULT_UNKNOWN'].includes(output?.code));
  if (viewportOverride) {
    try {
      const target=page?.getActivePage();
      if(target && (await page.tabs()).some(tab=>tab.page===target))await page.cdp('Emulation.clearDeviceMetricsOverride');
    } catch {cleanupWarnings.push('viewport_cleanup_failed');}
  }
  try {
    if (page && ['search','detail','probe'].includes(request.operation) && !retainedForInteraction) {
      process.env.OPENCLI_CLOSE_OWNED_TAB='1';
      try { await page.closeWindow();workClosed=true; } catch {cleanupWarnings.push('work_page_cleanup_failed');}
    }
  } finally {
    try { if (runId) await releaseSiteSessionLease?.({runId,session,surface:'adapter'}); }
    catch {cleanupWarnings.push('lease_release_failed');}
    finally {try{await bridge?.close();}catch{cleanupWarnings.push('bridge_close_failed');}}
  }
}
output.metadata={...output.metadata,...(attention?{attention:attention.metadata()}:{}),work_page_closed:workClosed,work_session:session,
  retained_for_interaction:retainedForInteraction,
  ...(retainedForInteraction?{retention_max_s:request.idleTimeoutS ?? 120,automatic_retry:false}:{}),
  ...(cleanupWarnings.length?{cleanup_warnings:cleanupWarnings}:{})};
process.stdout.write(JSON.stringify(output));
