globalThis.KRSites.liepin = {
  login: () => {const el=[...document.querySelectorAll('a,button')].find(e=>e.getBoundingClientRect().width&&/^(登录|登录\/注册|立即登录)$/.test((e.innerText||'').trim()));el?.click();},
  extract: () => (() => {
 const visible=e=>e&&e.getBoundingClientRect().width>0&&e.getBoundingClientRect().height>0;
 const take=(root,selectors)=>selectors.map(s=>root.querySelector(s)).filter(visible).map(e=>e.innerText.trim()).find(Boolean)||'';
 const dialogs=[...document.querySelectorAll('[role="dialog"],[aria-modal="true"],[class*="modal"],[class*="popup"],[class*="mask"]')].filter(visible);
 const wall=dialogs.map(e=>e.innerText||'').join(' ');
 const risk=[...document.querySelectorAll('[class*="captcha"],.geetest_panel,[id*="captcha"]')].some(visible)
   || /安全验证|滑动验证|人机验证|访问过于频繁/.test(wall) || /captcha|verify|safe-check/.test(location.pathname);
 const authenticated_ui=[...document.querySelectorAll('a[href*="/user/"],[class*="user-name"],[class*="user-avatar"]')].some(visible);
 const login=!authenticated_ui&&(/扫码登录|验证码登录|请先登录/.test(wall)||/passport|login/.test(location.hostname+' '+location.pathname));
 const seen=new Set();const items=[...document.querySelectorAll('a[href*="/job/"],a[href*="/a/"]')].filter(visible).flatMap(a=>{
  const u=new URL(a.href,location.href);if(!/(^|\.)liepin\.com$/.test(u.hostname)||!/^\/(job|a)\/\d+(\.shtml)?/.test(u.pathname)||seen.has(u.href))return [];
  seen.add(u.href);const card=a.closest('.job-card-pc-container,[class*="job-card"],li')||a;
  return [{title:take(card,['.job-title','[class*="job-title"]','h3'])||a.innerText.trim().split('\n')[0],url:u.href,
   salary:take(card,['.job-salary','[class*="salary"]']),company:take(card,['.company-name','[class*="company-name"]']),
   area:take(card,['.job-dq','[class*="job-dq"]','[class*="location"]']),desc:card.innerText.trim(),platform:'猎聘',
   content_type:'recruitment_job_candidate',source:'liepin_dom'}];
 });
 let job={};for(const e of document.querySelectorAll('script[type="application/ld+json"]')){
  try{const d=JSON.parse(e.textContent);const xs=Array.isArray(d)?d:(d['@graph']||[d]);job=xs.find(x=>x['@type']==='JobPosting')||job;}catch{}
 }
 const temp=document.createElement('div');temp.innerHTML=job.description||'';
 const content=temp.textContent.trim()||take(document,['.job-intro-container','.job-description','[data-selector="job-intro-content"]','.content-word']);
 return {risk,login,authenticated_ui,items,content,url:location.href,
  title:job.title||take(document,['h1','.job-title']),salary:take(document,['.job-salary','[class*="salary"]']),
  company:job.hiringOrganization?.name||take(document,['.company-name']),
  extraction_source:job.description?'jobposting_jsonld':'job_intro_primary',
  empty:[...document.querySelectorAll('.empty-content,.empty-result,[class*="no-result"]')].some(visible)};
})(),
  authenticated: d => !!(d.authenticated_ui || d.items?.length || d.content),
};
