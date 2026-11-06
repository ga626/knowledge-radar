"""只从正常原文页面解析媒体；下载、转写及证据归公共层。"""

from urllib.parse import parse_qs, urlsplit
from collectors.platform.wechat import article_url, article_identity

EXPRESSION = r"""(() => {
 const root=document.querySelector('#js_content');
 const gate=document.querySelector('.weui-msg,.page_msg,#js_verify');
 const text=gate?.textContent||(!root?document.body?.innerText||'':'');
 const rows=[];
 const read=(d)=>[...d.querySelectorAll('video,audio')].forEach(n=>{
  const src=n.currentSrc||n.src||n.querySelector('source')?.src||'';
  let vid='';try{vid=new URL(src,location.href).searchParams.get('vid')||''}catch{}
  rows.push({type:n.tagName.toLowerCase()==='video'?'video':'audio',url:src,
   media_id:n.getAttribute('vid')||n.getAttribute('data-vid')||vid,
   duration_s:Number.isFinite(n.duration)?n.duration:null});
 });
 if(root){read(root);root.querySelectorAll('iframe').forEach(f=>{try{if(f.contentDocument)read(f.contentDocument)}catch{}});}
 return {url:location.href,article:!!root,risk:/环境异常|去验证|验证码/.test(text),
  login:/请先登录|登录后查看/.test(text),items:rows.slice(0,100)};
})()"""


def approved_resource(url):
    try:
        p=urlsplit(str(url))
        return p.scheme=='https' and not p.username and not p.password and p.port in {None,443} and any(
            (p.hostname or '')==host or (p.hostname or '').endswith('.'+host)
            for host in ('qpic.cn','qq.com','weixin.qq.com'))
    except ValueError:
        return False


def resolve(article, manifest, *, invoke=None):
    from runtime.opencli_bridge import invoke_page
    from runtime.execution_context import check_execution
    if not article_url(article):
        return {'items':[], 'error':{'type':'unsupported_url'}}
    check_execution()
    result=(invoke or invoke_page)(session='wechat-media',operation='detail',url=article,
        expression=EXPRESSION,wait_selector='#js_content video,#js_content audio,.weui-msg,#js_verify',isolated_work=True)
    if not result.get('ok'):
        return {'items':[], 'error':{'type':result.get('code','media_resolution_failed')}}
    data=result.get('data') or {}
    if data.get('risk') or data.get('login'):
        return {'items':[], 'error':{'type':'anti_bot_verification' if data.get('risk') else 'authentication_required'}}
    if not data.get('article') or article_identity(data.get('url','')) != article_identity(article):
        return {'items':[], 'error':{'type':'media_article_identity_mismatch'}}
    rows=[]
    for ref in manifest:
        if ref.get('type') not in {'audio','video'}:
            continue
        media_id=ref.get('media_id') or parse_qs(urlsplit(ref.get('url','')).query).get('vid',[''])[0]
        matches=[r for r in data.get('items',[]) if r.get('type')==ref['type'] and approved_resource(r.get('url',''))
                 and media_id and r.get('media_id')==media_id]
        if len(matches)==1:
            rows.append({**ref,'url':matches[0]['url'],'media_access':'direct_candidate',
                'resolution_method':'original_article_browser_dom','duration_s':matches[0].get('duration_s')})
    return {'items':rows,'metadata':result.get('metadata',{}),
            'unresolved_count':sum(r.get('type') in {'audio','video'} for r in manifest)-len(rows)}
