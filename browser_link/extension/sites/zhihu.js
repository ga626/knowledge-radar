/* 固定只读知乎适配器。共享DOM与正常读者评论控件；无Cookie读取或任意请求。 */
(() => {
  const VERSION='zhihu-dom-v1', PART=90000;
  let snapshot=null;
  const clean=e=>(e?.textContent || '').trim();
  const visible=e=>!!e && e.getBoundingClientRect().width>0 && e.getBoundingClientRect().height>0;
  function target(value) {
    try {
      const u=new URL(value,location.href);
      if(u.protocol!=='https:' || u.username || u.password || u.port || !['zhihu.com','www.zhihu.com','zhuanlan.zhihu.com'].includes(u.hostname))return null;
      const path=u.pathname.replace(/\/$/,'');let m;
      if(u.hostname==='zhuanlan.zhihu.com') {
        m=path.match(/^\/p\/(\d+)$/);return m?{kind:'article',identifier:m[1],url:`https://zhuanlan.zhihu.com/p/${m[1]}`,question_id:''}:null;
      }
      m=path.match(/^\/question\/(\d+)\/answer\/(\d+)$/);
      if(m)return {kind:'answer',identifier:m[2],question_id:m[1],url:`https://www.zhihu.com/question/${m[1]}/answer/${m[2]}`};
      m=path.match(/^\/(answer|question|zvideo)\/(\d+)$/);
      return m?{kind:m[1],identifier:m[2],question_id:m[1]==='question'?m[2]:'',url:`https://www.zhihu.com/${m[1]}/${m[2]}`}:null;
    } catch {return null;}
  }
  const same=(a,b)=>!!a&&!!b&&a.kind===b.kind&&a.identifier===b.identifier&&(!a.question_id||a.question_id===b.question_id);
  // JSON tokens are matched whole; never round long numeric IDs through JS Number.
  function parse(text) {
    return JSON.parse(text.replace(/("(?:\\.|[^"\\])*")|(-?\d{16,})(?=\s*[,}\]])/g,(all,string,number)=>string||JSON.stringify(number)));
  }
  function pageState() {
    const wall=[...document.querySelectorAll('[role="dialog"],.SignFlow,.Modal-wrapper,[class*="Captcha"],[class*="Unhuman"]')].filter(visible).map(el=>{
      const copy=el.cloneNode(true);copy.querySelectorAll('.Comments-container,.CommentsV2,.CommentsV3,[data-comment-list]').forEach(e=>e.remove());return clean(copy);
    }).join(' ');
    const main=clean(document.querySelector('body'));
    let raw={};if(main.startsWith('{')&&main.length<10000){try{raw=parse(main);}catch{}}
    const message=String(raw.error?.message || '');
    const http_status=Number(performance.getEntriesByType('navigation')[0]?.responseStatus || 0);
    // SMS login copy ("验证码 / 获取短信验证码") alone is not a security wall.
    // Scope explicit challenge text to visible dialogs, or an actual challenge route/frame.
    const challengeFrame=[...document.querySelectorAll('iframe[src*="captcha"],iframe[src*="unhuman"]')].some(visible);
    const risk=/安全验证|访问被拦截|访问过于频繁|请求存在异常|暂时限制本次访问|拖动.{0,12}滑块|滑块.{0,12}拼图|图形验证码|图中验证码/.test(wall+' '+message)||/captcha|unhuman/.test(location.pathname)||challengeFrame;
    const headerLogin=[...document.querySelectorAll('.AppHeader-login,.AppHeader-userInfo button,.AppHeader-userInfo a[href*="/signin"]')]
      .some(el=>visible(el)&&/^(登录|注册|登录\s*[/／]\s*注册|登录知乎)$/.test(clean(el)));
    const login=/^\/(signin|signup)(\/|$)/.test(location.pathname)||/登录后查看|登录即可查看|登录知乎/.test(wall)||headerLogin;
    let failure_type='';
    if(risk)failure_type='anti_bot_verification';
    else if(login)failure_type='login_required';
    else if(raw.error || [401,403,429,451].includes(http_status))failure_type=Number(raw.error?.code || http_status)===401?'login_required':Number(raw.error?.code || http_status)===429?'rate_limited':Number(raw.error?.code || http_status)===451?'legal_restriction':'access_forbidden';
    else if(http_status===404||/你似乎来到了没有知识存在的荒原|内容已删除|文章不存在/.test(clean(document.querySelector('.ErrorPage,.ErrorPage-content'))||
      (main.length<500&&!document.querySelector('.RichText,.Post-RichTextContainer')?main:'')))failure_type='not_found';
    const profileVisible=[...document.querySelectorAll('.AppHeader-profile,.AppHeader-userInfo .Avatar,[aria-label="个人中心"],.AppHeader-profileEntry .Avatar')].some(visible);
    return {url:location.href,adapter_version:VERSION,risk,login,failure_type,http_status,
      authenticated_ui:!failure_type&&profileVisible,
      auth_diagnostics:{header_present:!!document.querySelector('.AppHeader'),profile_marker_visible:profileVisible,
        login_entry_visible:headerLogin,document_state:document.readyState,
        initial_state_present:!!document.querySelector('#js-initialData'),
        // Structural hints only; never text, attributes containing identity, or initial-state contents.
        header_profile_shapes:[...document.querySelectorAll('.AppHeader [class*="profile"],.AppHeader [class*="Profile"],.AppHeader [class*="user"],.AppHeader [class*="User"],.AppHeader [class*="Avatar"]')]
          .filter(visible).slice(0,16).map(el=>({tag:el.tagName,classes:[...el.classList].filter(c=>/^(AppHeader|Avatar|User)[a-zA-Z0-9_-]{0,48}$/.test(c)).slice(0,6)}))}};
  }
  async function authState(op,signal) {
    const deadline=Date.now()+(op==='auth_verify'?2500:0);
    let state=pageState();
    // Only observe this document. Never refresh, navigate, click, or repeat an API request.
    while(op==='auth_verify'&&!state.failure_type&&!state.authenticated_ui&&Date.now()<deadline){
      signal.throwIfAborted();
      await globalThis.KRWaitForChange(signal,Math.min(250,deadline-Date.now()));
      state=pageState();
    }
    signal.throwIfAborted();
    return {...state,failure_type:state.failure_type||(!state.authenticated_ui?'auth_state_unconfirmed':''),
      auth_diagnostics:{...state.auth_diagnostics,verification_timed_out:op==='auth_verify'&&!state.failure_type&&!state.authenticated_ui}};
  }
  function initial() {
    const el=document.querySelector('#js-initialData');if(!el)return {};
    try{return parse(el.textContent).initialState || {};}catch{return {};}
  }
  function safeUrl(raw) {
    try{const u=new URL(raw,location.href);return u.protocol==='https:'&&!u.username&&!u.password?u.href:'';}catch{return '';}
  }
  function bodyHtml(raw) {
    const root=document.createElement('div');root.innerHTML=raw;
    root.querySelectorAll('script,style,iframe,form,button,input,object,embed').forEach(e=>e.remove());
    const media=[],seen=new Set();
    root.querySelectorAll('img').forEach((img,index)=>{
      const url=safeUrl(img.getAttribute('data-original')||img.getAttribute('data-actualsrc')||img.getAttribute('data-src')||img.getAttribute('src'));
      if(url){img.setAttribute('src',url);if(!seen.has(url)){seen.add(url);media.push({type:'image',url,position:index,locator:{kind:'body_image',index},alt:img.getAttribute('alt')||''});}}
      else img.removeAttribute('src');
    });
    root.querySelectorAll('video,audio,a[href]').forEach(el=>{
      const url=safeUrl(el.getAttribute('src')||el.querySelector('source')?.getAttribute('src')||el.getAttribute('href'));
      const linked=target(url);
      const type=['VIDEO','AUDIO'].includes(el.tagName)?el.tagName.toLowerCase():linked?.kind==='zvideo'?'video':'';
      if(type&&url&&!seen.has(url)){seen.add(url);media.push({type,url,locator:{kind:'embedded_media',index:media.length}});}
    });
    root.querySelectorAll('*').forEach(el=>{
      for(const attr of [...el.attributes])if(!['href','src','alt','title','colspan','rowspan'].includes(attr.name))el.removeAttribute(attr.name);
      if(el.hasAttribute('href')){const url=safeUrl(el.getAttribute('href'));if(url)el.setAttribute('href',url);else el.removeAttribute('href');}
    });
    return {html:root.innerHTML,media_manifest:media};
  }
  function answerNode(id) {
    return [...document.querySelectorAll('.AnswerItem,[data-answer-id]')].find(el=>{
      if(el.getAttribute('data-answer-id')===id)return true;
      try{if(String(parse(el.getAttribute('data-zop')||'{}').itemId||'')===id)return true;}catch{}
      return [...el.querySelectorAll('meta[itemprop="url"],.ContentItem-time a[href]')].some(a=>{const t=target(a.getAttribute('content')||a.href);return t?.kind==='answer'&&t.identifier===id;});
    });
  }
  async function detail(payload,signal) {
    signal.throwIfAborted();
    const state=pageState();if(state.failure_type)return state;
    const wanted=payload.target,current=target(location.href);
    if(!same(wanted,current))return {...state,failure_type:'page_identity_changed'};
    if(payload.read_part!=null) {
      if(!snapshot||snapshot.id!==payload.snapshot_id||snapshot.url!==location.href||!Number.isInteger(payload.read_part)||payload.read_part<0||payload.read_part>=snapshot.parts.length)
        return {...state,failure_type:'content_snapshot_expired'};
      return {...state,target:current,snapshot_id:snapshot.id,body_sha256:snapshot.sha,part:payload.read_part,body_html:snapshot.parts[payload.read_part]};
    }
    if(!document.querySelector('#js-initialData,.AnswerItem,article,.Post-Main,.QuestionHeader,.ZVideo'))
      await globalThis.KRWaitFor('#js-initialData,.AnswerItem,article,.Post-Main,.QuestionHeader,.ZVideo',signal,2500);
    signal.throwIfAborted();
    let node=current.kind==='answer'?answerNode(current.identifier):current.kind==='article'?document.querySelector('article,.Post-Main'):document.querySelector('.QuestionHeader,.ZVideo');
    let rich=node?.querySelector('.RichContent-inner .RichText,.Post-RichTextContainer,.RichText,.QuestionRichText,.ZVideo-intro');
    // Expand only the requested body's own readonly control, once.
    const expand=[...(node?.querySelectorAll('button')||[])].find(e=>visible(e)&&/^(阅读全文|展开阅读全文|显示全部)$/.test(clean(e)));
    if(expand){expand.click();await globalThis.KRWaitForChange(signal,600);signal.throwIfAborted();rich=node.querySelector('.RichContent-inner .RichText,.RichText');}
    const s=pageState();if(s.failure_type)return s;
    const entities=initial().entities || {};
    const entity=(current.kind==='answer'?entities.answers:current.kind==='article'?entities.articles:entities.questions)?.[current.identifier];
    // A matching map key alone is insufficient if the entity declares another id.
    if(entity?.id!=null&&String(entity.id)!==current.identifier)return {...s,failure_type:'page_identity_changed'};
    if(current.kind==='answer'&&current.question_id&&entity?.question?.id!=null&&String(entity.question.id)!==current.question_id)
      return {...s,failure_type:'page_identity_changed'};
    let html=typeof entity?.content==='string'?entity.content:(current.kind==='question'&&typeof entity?.detail==='string'?entity.detail:rich?.innerHTML||'');
    const restricted=!!node?.querySelector('.PurchaseBtn,.PaidRichContent,.KfeCollection-Purchase')||!!entity?.isPaid;
    if(restricted)return {...s,failure_type:'content_restricted'};
    const directory=[],seen=new Set();
    if(current.kind==='question')for(const a of document.querySelectorAll('a[href]')){
      const t=target(a.href);if(t?.kind!=='answer'||t.question_id!==current.identifier||seen.has(t.identifier))continue;
      seen.add(t.identifier);directory.push({...t,title:clean(a)||'回答',body_verified:false});
      if(directory.length>=Math.max(1,Math.min(20,payload.answer_limit||10)))break;
    }
    if(!html && current.kind!=='question')return {...s,failure_type:'page_structure_changed'};
    if(current.kind==='question'&&!node&&!entity)return {...s,failure_type:'page_structure_changed'};
    const normalized=bodyHtml(html);
    if(normalized.html.length>PART*64)return {...s,failure_type:'content_snapshot_too_large'};
    const parts=[];for(let i=0;i<normalized.html.length;i+=PART){let end=i+PART;if(end<normalized.html.length&&/[\uD800-\uDBFF]/.test(normalized.html[end-1]))end--;parts.push(normalized.html.slice(i,end));i=end-PART;}
    if(!parts.length)parts.push('');
    const sha=[...new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(normalized.html)))].map(x=>x.toString(16).padStart(2,'0')).join('');
    snapshot={id:crypto.randomUUID(),url:location.href,parts,sha};
    const complete=(!!entity&&typeof (current.kind==='question'?entity.detail:entity.content)==='string')||!!rich&&!expand&&!node?.querySelector('.RichContent--unescapable,.ContentItem-more');
    return {...s,target:current,body_html:parts[0],parts:parts.length,snapshot_id:snapshot.id,body_sha256:sha,
      title:String(entity?.question?.title||entity?.title||clean(document.querySelector('h1.QuestionHeader-title,h1.Post-Title,h1'))||document.title.replace(/\s*-\s*知乎$/,'')),
      author:String(entity?.author?.name||clean(node?.querySelector('.AuthorInfo-name,.UserLink-link'))),
      published_at:entity?.createdTime||entity?.created_time||'',updated_at:entity?.updatedTime||entity?.updated_time||'',
      body_source:entity?'matched_initial_data':'matched_dom',body_complete:current.kind!=='zvideo'&&complete,
      body_gap:complete?'':'visible_body_only',media_manifest:normalized.media_manifest,
      answer_directory:directory,directory_coverage:current.kind==='question'?'visible_answers_bounded_not_all_answers':'not_applicable'};
  }
  async function comments(payload, signal) {
    signal.throwIfAborted();
    const state=pageState(), current=target(location.href);
    if(state.failure_type)return state;
    if(!same(payload.target,current))return {...state,failure_type:'page_identity_changed'};
    if(current.kind!=='answer'||location.hostname!=='www.zhihu.com')return {...state,failure_type:'comments_target_unsupported'};
    let node=answerNode(current.identifier);
    if(!node)return {...state,failure_type:'page_structure_changed'};
    const limit=Math.max(1,Math.min(500,Number.isInteger(payload.limit)?payload.limit:20));
    const scope=payload.scope||{},rootLimit=Math.max(1,Math.min(100,scope.root_limit||limit));
    const replyLimit=Math.max(0,Math.min(100,scope.reply_limit??3));
    const panel=()=>answerNode(current.identifier)?.querySelector('.Comments-container');
    const hydrateUntil=Date.now()+800;
    while(Date.now()<hydrateUntil||document.readyState!=='complete'){
      signal.throwIfAborted();
      if(Date.now()>hydrateUntil+2500)break;
      await globalThis.KRWaitForChange(signal,100);
    }
    node=answerNode(current.identifier);
    if(!node)return {...pageState(),failure_type:'page_identity_changed'};
    const advertised=[...node.querySelectorAll('button')].map(e=>clean(e).match(/^(\d+)\s*条评论$/)).find(Boolean);
    const advertisedTotal=advertised?Number(advertised[1]):null;
    // Use only the normal reader control, once. Never synthesize/replay a comment API request.
    if(!panel()||!visible(panel())){
      const button=[...node.querySelectorAll('button')].find(e=>visible(e)&&/^(?:\d+\s*条评论|添加评论)$/.test(clean(e).replace(/\u200b/g,'')));
      if(!button)return {...state,failure_type:'comments_control_not_exposed'};
      button.click();
    }
    const started=Date.now(),deadline=started+5000;
    while(Date.now()<deadline){
      signal.throwIfAborted();
      const s=pageState();if(s.failure_type)return s;
      const p=panel();
      if(p&&visible(p)&&Date.now()-started>=800&&(p.querySelector('[data-id] .CommentContent')||/加载失败|无权|禁止访问/.test(clean(p))))break;
      await globalThis.KRWaitForChange(signal,150);
    }
    signal.throwIfAborted();
    if(!same(payload.target,target(location.href)))return {...pageState(),failure_type:'page_identity_changed'};
    const p=panel();
    if(!p||!visible(p))return {...pageState(),failure_type:'reader_comments_not_exposed',
      comment_diagnostics:{target_connected:node.isConnected,panel_present:!!p,panel_visible:visible(p),
        matching_panel_count:document.querySelectorAll('.Comments-container').length,document_state:document.readyState}};
    const text=clean(p);
    const loading=/加载中|正在加载/.test(text), permission=/无权|禁止访问|权限不足/.test(text);
    if((/加载失败/.test(text)||permission)&&!p.querySelector('.CommentContent'))return {...pageState(),
      failure_type:permission?'reader_comments_permission_denied':'reader_comments_load_failed',
      comment_diagnostics:{reader_state:permission?'permission_denied':'load_failed',permission_observed:permission,
        panel_present:true,panel_visible:true,document_state:document.readyState,direct_api_requests:0}};
    const nodes=[...p.querySelectorAll('[data-id]')].filter(e=>/^\d+$/.test(e.getAttribute('data-id')||'')&&visible(e)&&
      [...e.querySelectorAll('.CommentContent')].some(c=>c.closest('[data-id]')===e));
    const roots=nodes.filter(e=>!e.parentElement.closest('[data-id]')), rows=[],seen=new Set();
    const selected=roots.slice(0,rootLimit);let omitted=roots.length>selected.length;
    const read=(e,rootId='')=>{
      const id=e.getAttribute('data-id');if(seen.has(id))return;seen.add(id);
      const content=[...e.querySelectorAll('.CommentContent')].find(c=>c.closest('[data-id]')===e);
      const fragment=content.cloneNode(true);
      fragment.querySelectorAll('script,style,iframe,object').forEach(x=>x.remove());
      // Sticker alt is content; avatar/badge images never enter the comment body manifest.
      fragment.querySelectorAll('img.sticker,img.emoji,img.emoticon').forEach(x=>x.replaceWith(document.createTextNode(x.alt||'')));
      const media=[...fragment.querySelectorAll('img,video,audio')].map(x=>({type:x.tagName==='IMG'?'image':x.tagName.toLowerCase(),url:safeUrl(x.getAttribute('data-original')||x.getAttribute('src')||'')})).filter(x=>x.url);
      const author=[...e.querySelectorAll('a[href*="/people/"]')].find(x=>x.closest('[data-id]')===e&&clean(x));
      // Nesting proves the thread root, NOT which reply within that thread is the parent.
      rows.push({id,root_id:rootId||id,parent_id:'',is_reply:!!rootId,
        parent_relation:rootId?'unknown':'root',relationship_confidence:rootId?'dom_thread_root_only':'root',
        content:clean(fragment),author:clean(author),media,target_id:current.identifier,
        locator:{kind:'platform_comment',comment_id:id,target_id:current.identifier}});
    };
    for(const root of selected){
      if(rows.length>=limit){omitted=true;break;}read(root);
      const replies=nodes.filter(e=>e!==root&&root.contains(e));omitted||=replies.length>replyLimit;
      for(const reply of replies.slice(0,replyLimit)){if(rows.length>=limit){omitted=true;break;}read(reply,root.getAttribute('data-id'));}
    }
    const empty=!loading&&rows.length===0&&advertisedTotal!==null&&advertisedTotal===0&&/暂无评论|还没有评论/.test(text);
    const count=Number(text.match(/(\d+)\s*条评论/)?.[1]??NaN);
    const expanders=[...p.querySelectorAll('button,a,[role="button"]')].filter(e=>visible(e)&&/展开.*回复|查看.*回复|加载更多/.test(clean(e))).length;
    const observedAll=empty||(!loading&&nodes.length>0&&!expanders&&Number.isFinite(count)&&count===nodes.length);
    const orderSupported=!scope.order||['relevance','score'].includes(scope.order);
    const exhausted=observedAll&&!omitted;
    const satisfied=observedAll&&orderSupported;
    return {...pageState(),comments:rows,empty_confirmed:empty,request_satisfied:satisfied,
      exhausted,has_more:!exhausted,limit,comment_source:'normal_reader_dom',
      coverage:{scope:'visible_reader_threads',actual_order:'platform_default',order_applied:orderSupported,root_count:rows.filter(x=>!x.is_reply).length,
        reply_count:rows.filter(x=>x.is_reply).length,observed_comment_count:nodes.length,
        displayed_total:Number.isFinite(count)?count:null,expanders_remaining:expanders,
        stop_reason:!orderSupported?'requested_order_not_applied':loading?'reader_loading':exhausted?'visible_reader_exhausted':omitted?'requested_limit':'reader_window_not_exhausted'},
      comment_diagnostics:{transport:'normal_reader_dom',direct_api_requests:0,parent_identity:'unknown_for_nested_replies',
        reader_state:loading?'loading':empty?'explicit_zero':rows.length?'rows_observed':'zero_unverified',
        permission_observed:false,panel_present:true,panel_visible:true,document_state:document.readyState,
        advertised_total:advertisedTotal,observed_nodes:nodes.length}};
  }

  globalThis.KRSites.zhihu={
    async extract(op,payload,signal){
      if(['probe','auth_verify'].includes(op))return authState(op,signal);
      const s=pageState();if(s.failure_type)return s;
      if(op==='detail')return detail(payload,signal);
      if(op==='comments')return comments(payload,signal);
      if(op!=='search')return {...s,failure_type:'unsupported_operation'};
      const u=new URL(location.href);if(u.pathname!=='/search'||u.searchParams.get('q')!==payload.keyword)return {...s,failure_type:'search_query_not_applied'};
      const items=[],seen=new Set();
      for(const card of document.querySelectorAll('.SearchResult-Card,.List-item')){
        const a=[...card.querySelectorAll('h2 a[href],a.ContentItem-title[href],a[href]')].find(a=>target(a.href));
        const t=target(a?.href);if(!t||seen.has(t.kind+':'+t.identifier))continue;seen.add(t.kind+':'+t.identifier);
        items.push({url:t.url,title:clean(card.querySelector('h2,.ContentItem-title'))||clean(a),author:clean(card.querySelector('.AuthorInfo-name')),summary:clean(card.querySelector('.RichContent,.RichText')).slice(0,300)});
        if(items.length>=Math.max(1,Math.min(20,payload.limit||10)))break;
      }
      return {...s,items,empty_confirmed:!items.length&&!!document.querySelector('.EmptyState')&&/没有找到|暂无|无结果/.test(clean(document.querySelector('.EmptyState')))};
    },
    singleShotOps:['detail','comments','probe','auth_verify'],
    ready:(d,op)=>!!d.failure_type||(['probe','auth_verify'].includes(op)?true:op==='search'?!!(d.items?.length||d.empty_confirmed):true),
  };
})();
