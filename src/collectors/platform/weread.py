"""微信读书关联的公众号搜一搜。只发现文章，复用获准的 OpenCLI 浏览器。

卡片字段参考 jayhigh425/wechat-article-search-skill @ 50efea1a (MIT)。
不采用其独立 CDP/Profile、Cookie 导出、window.open 改写或正文存储。
"""

from __future__ import annotations

import hashlib
from urllib.parse import urlencode, urlsplit, urlunsplit

from collectors.platform.wechat import article_url, article_identity

CONTRACT = "weread-article-discovery-v1"
HOME = "https://weread.qq.com/"
SEARCH = "https://search.weixin.qq.com/cgi-bin/newsearchweb/userclientjump?"

# 登录检查只读取可见公开界面；不读 Cookie、localStorage、书架或私人笔记。
AUTH_EXPRESSION = r"""(() => {
 const visible=n=>!!n && n.getClientRects().length>0;
 const text=document.body?.innerText||'';
 const login=[...document.querySelectorAll('button,a,[role=button],.navBar_link,.wr_index_page_top_section_header_action_link')]
   .some(n=>visible(n)&& /^(登录|微信登录|扫码登录)$/.test(n.textContent.trim()));
 const risk= /环境异常|访问过于频繁|请完成安全验证/.test(text.slice(0,1000));
 const member=document.querySelector('.wr_index_page_top_section_header_action_avatar,.navBar_avatar,.navBar_user,.user_avatar,.navBar .avatar');
 const account=member?.querySelector('img')?.getAttribute('src')||member?.getAttribute('src')||'';
 return {url:location.href,login,risk,authenticated_ui:visible(member),account_marker:account,
   reading_home:/我的书架|继续阅读/.test(text),loading:!text.trim()};
})()"""

SEARCH_EXPRESSION = r"""(() => {
 const cards=[...document.querySelectorAll('.search_list_item')];
 const clone=document.body?.cloneNode(true);
 clone?.querySelectorAll('.search_list_item,script,style').forEach(n=>n.remove());
 const gate=(clone?.textContent||'').trim().slice(0,3000);
 return {url:location.href,card_count:cards.length,
  risk:/环境异常|访问频繁|操作频繁|请完成安全验证/.test(gate),
  login:/请先登录|扫码登录|微信登录|登录已过期/.test(gate),
  empty:/暂无搜索结果|没有搜索结果|未找到相关|暂无相关|没有找到相关/.test(gate),
  ended:/暂无更多内容|没有更多了/.test(gate),loading:/加载中/.test(gate),
  items:cards.slice(0,100).map((n,i)=>{
   const text=s=>n.querySelector(s)?.textContent?.trim()||'';
   return {title:text('.article__title-text'),snippet:text('.article__desc').slice(0,500),
    account_name:text('.source__title'),published_at:text('.source__text.date'),
    url:n.querySelector('a[href*="mp.weixin.qq.com"]')?.href||'',
    candidate_ref:n.getAttribute('data-id')||String(i)};
  })};
})()"""

# 只旁听正常页面发起的 broker 响应，既不重放请求，也不暴露响应账号信息。
BROKER_EXPRESSION = r"""rows => {
 let broker_error=null;const broker_articles=[];
 for(const row of rows){
  try{
   const d=typeof row.responsePreview==='string'?JSON.parse(row.responsePreview):row.responsePreview;
   const code=d?.errCode;
   if(code && Number(code)!==0) broker_error={type:Number(code)===-2010?'authentication_required':'provider_error',code};
   else if(row.status>=400) broker_error={type:({401:'authentication_required',403:'access_forbidden',429:'rate_limited',451:'legal_restriction'})[row.status]||'http_error',status_code:row.status};
   for(const box of (d?.content?.data||[])){
    for(const item of (box.items||[])){
     broker_articles.push({candidate_ref:String(item.docID||''),url:String(item.doc_url||''),doc_type:item.docType});
    }
   }
  }catch{if(row.status>=400)broker_error={type:'http_error',status_code:row.status};}
 }
 return {broker_error,broker_articles};
}"""

SCROLL_EXPRESSION = r"""(() => {
 const cards=[...document.querySelectorAll('.search_list_item')];
 if(!cards.length)return {ready:true};
 const last=cards.at(-1);last.scrollIntoView({block:'end'});
 let n=last.parentElement;
 while(n&&n!==document.body){
  if(n.scrollHeight>n.clientHeight && /auto|scroll/.test(getComputedStyle(n).overflowY)){
   n.scrollTop=n.scrollHeight;n.dispatchEvent(new Event('scroll'));break;
  }n=n.parentElement;
 }
 window.scrollTo(0,document.body.scrollHeight);window.dispatchEvent(new Event('scroll'));
 return {clicked:true};
})()"""


def failure(code, **extra):
    manual = code in {"authentication_required", "anti_bot_verification", "access_forbidden"}
    return {"provider": "weread_sosou", "items": [], "status": "waiting_for_user" if manual else "failed",
            "error": {"type": code, "retryable": False, "manual_action_required": manual,
                      "next_action": "显式调用 health_check(mode='request_browser_interaction:weread:login')，用户正常登录或验证后继续。" if manual
                      else "查看来源回执；公共搜索仍可独立使用。"}, **extra}


def browser_scope():
    """绑定代际，不输出浏览器地址或用户身份；搜索结果不跨调用缓存。"""
    from runtime.opencli_bridge import connection_config
    c = connection_config()
    state = c["state_root"]
    binding = state / "binding.json"
    marker = binding.read_bytes() if binding.is_file() else b""
    return hashlib.sha256(str(c["context_id"]).encode() + marker).hexdigest()


def readiness(*, invoke=None):
    from runtime.opencli_bridge import invoke_page
    result = (invoke or invoke_page)(session="wechat-weread", operation="auth_verify", url=HOME,
        expression=AUTH_EXPRESSION, wait_selector=".wr_index_page_top_section_header_wrapper,.navBar,.navBar_link,.navBar_avatar", isolated_work=True)
    if not result.get("ok"):
        return failure({"AUTH_REQUIRED": "authentication_required", "SECURITY_BLOCK": "anti_bot_verification"}
                       .get(result.get("code"), result.get("code", "browser_unavailable")))
    data = result.get("data") or {}
    if data.get("risk"):
        return failure("anti_bot_verification")
    if data.get("login"):
        return failure("authentication_required")
    # 首页识别不足不能当作认证成功；真实搜索会再次验证跨域授权。
    marker = data.get("account_marker") or ""
    return {"ready": bool(data.get("authenticated_ui") or data.get("reading_home")),
            "account_generation": hashlib.sha256(marker.encode()).hexdigest() if marker else "",
            "binding_generation": browser_scope(), "metadata": result.get("metadata", {})}


def search(query, *, limit=10, freshness="", timeout_s=40, invoke=None):
    from runtime.opencli_bridge import invoke_page
    from runtime.execution_context import check_execution
    import time

    started = time.monotonic()
    call = invoke or invoke_page
    check_execution()
    ready = readiness(invoke=call)
    if ready.get("error"):
        return ready
    if not ready.get("ready"):
        return failure("authentication_state_unrecognized")
    if time.monotonic() - started >= timeout_s:
        return failure("deadline_exceeded")
    url = SEARCH + urlencode({"path": "page/search/weread", "query": query, "platform": "pc"})
    result = call(session="wechat-weread", operation="search", url=url,
        expression=SEARCH_EXPRESSION, wait_selector=".search_list_item,.search_empty,.login_container",
        capture_pattern="wx_search_broker_proxy", network_expression=BROKER_EXPRESSION,
        steps=[{"expression":"(() => ({clicked:true}))()",
                "wait":{"selector":".search_list_item,.search_empty,.login_container", "timeout":15}},
               *[{"expression":SCROLL_EXPRESSION.replace(
                   "if(!cards.length)return {ready:true};",
                   "if(!cards.length||cards.length>="+str(int(limit))+"||/暂无更多内容|没有更多了/.test(document.body?.innerText||''))return {ready:true};"),
                   "wait":{"time":0.6,"timeout":3}} for _ in range(2)]],
        isolated_work=True)
    if not result.get("ok"):
        return failure({"AUTH_REQUIRED": "authentication_required", "SECURITY_BLOCK": "anti_bot_verification"}
                       .get(result.get("code"), result.get("code", "browser_unavailable")))
    check_execution()
    data = result.get("data") or {}
    if data.get("broker_error"):
        return failure(data["broker_error"]["type"], provider_diagnostic=data["broker_error"])
    if data.get("risk") or data.get("login"):
        return failure("anti_bot_verification" if data.get("risk") else "authentication_required")
    if not data.get("items") and not data.get("empty"):
        return failure("search_loading_timeout" if data.get("loading") else "search_structure_unrecognized")
    rows = []
    excluded = 0
    broker = {r["candidate_ref"]:r for r in data.get("broker_articles",[]) if r.get("candidate_ref")}
    for r in data.get("items", []):
        target = str(r.get("url") or "")
        # 已观察到的DOM卡片身份与正常响应docID精确关联；不按标题猜链接。
        match = broker.get(str(r.get("candidate_ref", "")).split("&")[0])
        if not target and match:
            target = match.get("url", "")
            if not article_url(target):
                excluded += 1
                continue  # 搜一搜混入的百科等内容不冒充公众号文章。
        if article_url(target):
            # 发现URL用于取正文，完整保留平台返回的参数；稳定身份只用于去重。
            # chksm等参数可能参与正常访问，不能把去重身份当抓取地址。
            target = urlunsplit(urlsplit(target)._replace(scheme="https"))
        # data-id只作候选身份，绝不据此拼接原文URL。
        rows.append({**r, "url": target if article_url(target) else "",
                     "candidate_ref": str(r.get("candidate_ref", "")),
                     "article_identity": article_identity(target),
                     "discovery_url": url, "source_provider": "weread_sosou",
                     "published_time_precision": "search_page_text",
                     "resolution_status": "resolved" if article_url(target) else "article_url_unresolved"})
    unresolved = sum(not row["url"] for row in rows)
    return {"provider": "weread_sosou", "items": rows[:limit], "acquired_items": rows,
            "status": "partial" if unresolved else "ok" if rows else "empty",
            "metadata": {"contract": CONTRACT, "coverage": "observed_search_batch",
                "time_filter": "not_applied" if freshness else "not_requested",
                "article_bodies_fetched": False, "unresolved_article_urls": unresolved,
                "expansion": {"mode":"bounded_page_scroll", "max_scrolls":2,
                              "executed_steps":result.get("metadata",{}).get("steps_executed")},
                "excluded_non_article_cards": excluded,
                "account_generation": ready["account_generation"], "binding_generation": ready["binding_generation"],
                "cache_policy": "disabled_cross_call_borrowed_account",
                "continuation": {"mode": "refine_query", "reason": "no_verified_stable_server_cursor"}
                    if rows and not data.get("ended") else None,
                "transport": result.get("metadata", {}), "elapsed_s": round(time.monotonic()-started,3)}}


def begin_login():
    """显式人工交接；正常页面扫码，不占搜索槽，不导出会话。"""
    from runtime.opencli_bridge import invoke_page
    return invoke_page(session="wechat-weread", operation="login", url=HOME, passive_manual_login=True)
