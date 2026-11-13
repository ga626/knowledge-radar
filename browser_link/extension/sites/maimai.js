(() => {
const state = () => (() => {
 const visible=e=>e&&e.getBoundingClientRect().width>0&&e.getBoundingClientRect().height>0;
 const wall=[...document.querySelectorAll('[role="dialog"],[class*="login"],[class*="captcha"],[class*="verify"],[class*="modal"]')].filter(visible).map(e=>e.innerText||'').join(' ');
 const risk=/确认您是真人|安全验证|滑动验证|访问被拦截|访问过于频繁/.test(wall)||/captcha|verify/.test(location.pathname);
 const login=/\/platform\/login/.test(location.pathname)||/扫码登录|验证码登录|登录脉脉/.test(wall);
 const authenticated_ui=[...document.querySelectorAll('[class*="avatar"],a[href*="/profile/"]')].some(visible)&&!login;
 return {risk,login,authenticated_ui,url:location.href};
})();
const json = async (path, params, signal) => {
 let headers = {accept: '*/*'};
 const url = path + '?' + new URLSearchParams(params);
 let r = await fetch(url, {credentials:'same-origin', headers, signal});
 const csrf = r.headers.get('X-CSRF-Token');
 if (r.status === 204 && csrf) {
   r = await fetch(url, {credentials:'same-origin', headers:{...headers,'x-csrf-retry':'1','x-csrf-token':csrf}, signal});
 }
 let data = null; try { data = await r.json(); } catch {}
 // Only the public retry deadline crosses the bridge; never return auth/CSRF headers.
 return {http_status:r.status,data,headers:{'retry-after':r.headers.get('Retry-After')}};
};
globalThis.KRSites.maimai = {
 async extract(op, payload, signal, progress=()=>{}) {
  let d=state(); if (d.risk || d.login) return d;
  if (['search','probe','auth_verify'].includes(op)) {
   const path = op === 'search' ? '/community/api/common/search' : '/community/api/common/get-user-info';
   const params = op === 'search' ? {frm:'website',query:payload.keyword || '',__platform:'community_web',lim:Math.max(1,Math.min(20,payload.limit || 10))} : {};
   progress('site_api_wait');const probe=await json(path,params,signal);progress('site_api_received');
   return {...state(),probe,authenticated_api:probe.http_status===200 && probe.data?.result==='ok' && !!probe.data?.user};
  }
  if (op === 'comments') {
   const check=()=>{const s=state();if(s.risk || s.login) throw new Error(s.risk?'SECURITY_BLOCK':'AUTH_REQUIRED');};
   check(); (() => {const heading=[...document.querySelectorAll('div')].find(e=>e.children.length===0&&(e.innerText||'').trim()==='评论');heading?.scrollIntoView({block:'center'});return {clicked:!!heading};})();
   // One bounded observer wait for lazy comments, not blind refresh/retry.
   progress('comments_wait');await globalThis.KRWaitFor(".bg-background.px-6.py-3 > .relative.mt-4 [data-rich-text],[data-testid=\"comment-item\"], [class*=\"commentItem\"], [class*=\"comment-item\"], [class*=\"CommentItem\"]",signal,3000);
   for(let i=0;i<2;i++) {check(); const action=(() => {const e=[...document.querySelectorAll('button,a,[role="button"],span')].find(e=>e.getBoundingClientRect().width&&/^(查看更多评论|加载更多评论|查看更多回复|展开回复|查看\d+条回复)$/.test((e.innerText||'').trim()));e?.click();return {clicked:!!e};})(); if(!action.clicked)break; await globalThis.KRWaitForChange(signal,800);}
   check();return (() => {const state=(() => {
 const visible=e=>e&&e.getBoundingClientRect().width>0&&e.getBoundingClientRect().height>0;
 const wall=[...document.querySelectorAll('[role="dialog"],[class*="login"],[class*="captcha"],[class*="verify"],[class*="modal"]')].filter(visible).map(e=>e.innerText||'').join(' ');
 const risk=/确认您是真人|安全验证|滑动验证|访问被拦截|访问过于频繁/.test(wall)||/captcha|verify/.test(location.pathname);
 const login=/\/platform\/login/.test(location.pathname)||/扫码登录|验证码登录|登录脉脉/.test(wall);
 const authenticated_ui=[...document.querySelectorAll('[class*="avatar"],a[href*="/profile/"]')].some(visible)&&!login;
 return {risk,login,authenticated_ui,url:location.href};
})();
 const heading=[...document.querySelectorAll('div')].find(e=>e.children.length===0&&(e.innerText||'').trim()==='评论');
 const scope=heading?.parentElement;
 const modern=scope?[...scope.children].filter(e=>e.querySelector('[data-rich-text]')):[];
 const rows=modern.length?modern:[...document.querySelectorAll("[data-testid=\"comment-item\"], [class*=\"commentItem\"], [class*=\"comment-item\"], [class*=\"CommentItem\"]")];
 return {...state,comments_scope_found:!!scope,comments_empty_confirmed:!!scope&&/暂无评论|还没有评论/.test(scope.innerText||''),
 comments_loading:!!scope&&/加载中/.test(scope.innerText||''),
 comments:rows.map(e=>({content:(e.querySelector('[data-rich-text]')?.innerText||e.innerText||'').trim(),
   author:(e.querySelector('.font-bold')?.innerText||'').trim(),id:e.getAttribute('data-id')||'',
   replies:[...e.querySelectorAll('[data-rich-text]')].slice(1).map(r=>({content:(r.innerText||'').trim()}))})).filter(x=>x.content)};})();
  }
  const flight=[...document.querySelectorAll('script')].filter(e=>e.textContent.includes('self.__next_f.push(')).map(e=>e.textContent).join('\n');
  return {...d,html:flight.slice(0,600000),
    html_truncated:flight.length>600000,
    dom_content:(document.querySelector('article,[class*="postContent"],[class*="feedContent"],[class*="gossipContent"]')?.innerText||'').trim()};
 },
 singleShotOps: ['search','probe','auth_verify','comments'],
 ready: (d,op) => ['search','probe','auth_verify'].includes(op) ? !!d.probe : op==='comments' ? true : !!d.dom_content || /feedData|gossipData/.test(d.html || ''),
};
})();
