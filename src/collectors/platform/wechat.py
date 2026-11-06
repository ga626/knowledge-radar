"""微信只读页面合同；来源状态与解析归平台，传输、媒体和存证归公共层。

DOM字段与懒图处理参考OpenCLI 97dc0cd5214b9602bcda78d7422f231ee8a803e9
clis/weixin/{search,download}.js（Apache-2.0）；不引入其下载器、验证轮询或草稿写入。
"""

from __future__ import annotations

import hashlib
import json
import html as html_lib
import re
from datetime import datetime, timezone, timedelta
from typing import Annotated, Literal
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, field_validator

CONTRACT = "wechat-readonly-v1"
UPSTREAM = "97dc0cd5214b9602bcda78d7422f231ee8a803e9"
PRESENTATION = {"group": "platform", "label": "微信", "site": "微信公众号"}


class WechatReadOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    transport: Literal["http", "opencli"] = "http"
    image_indexes: list[Annotated[int, Field(ge=0)]] | None = Field(default=None, max_length=100)
    media_limit: int = Field(default=30, ge=1, le=100)
    include_attachments: bool = False
    refresh: bool = False

    @field_validator("image_indexes")
    @classmethod
    def unique_indexes(cls, value):
        if value is not None and len(set(value)) != len(value):
            raise ValueError("image_indexes_must_be_unique")
        return value


def _parts(url):
    try:
        p = urlsplit(str(url))
        if p.scheme not in {"https", "http"} or p.username or p.password or p.port not in {None, 443, 80}:
            return None
        return p
    except (ValueError, TypeError):
        return None


def article_url(url):
    p = _parts(url)
    if not p or p.hostname != "mp.weixin.qq.com":
        return False
    q = parse_qs(p.query)
    return bool(
        re.fullmatch(r"/s/[A-Za-z0-9_-]+", p.path)
        or (p.path == "/s" and all(q.get(k) for k in ("__biz", "mid", "idx", "sn")))
    )


def matches_url(url):
    return article_url(url)


def article_identity(url):
    """只去已知转发参数；短长链接无映射证据时绝不猜同一篇。"""
    p = _parts(url)
    if not article_url(url):
        return ""
    if p.path.startswith("/s/"):
        return "https://mp.weixin.qq.com" + p.path
    q = parse_qs(p.query)
    return "https://mp.weixin.qq.com/s?" + urlencode([(k, q[k][0]) for k in ("__biz", "mid", "idx", "sn")])


def failure(code, *, url="", message="", **extra):
    manual = code in {"anti_bot_verification", "authentication_required", "access_forbidden"}
    return {
        "platform": "微信",
        "url": url,
        "status": "waiting_for_user" if manual else "failed",
        "content": "",
        "body_verified": False,
        "body_complete": False,
        "failure_type": code,
        "manual_action_required": manual,
        "error": {
            "type": code,
            "message": message or code,
            "retryable": code in {"rate_limited", "network_error"},
            "manual_action_required": manual,
            "next_action": "请在正常页面处理后显式继续；不自动换通道重试。"
            if manual
            else "检查来源回执或选择另一篇资料。",
        },
        "source_presentation": PRESENTATION,
        **extra,
    }


def _field(soup, selectors):
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            value = node.get("content") if node.name == "meta" else node.get_text(" ", strip=True)
            if value:
                return str(value).strip()
    return ""


def page_state(soup, url):
    """仅页面状态区判别，不因文章讨论验证码/付费而封锁正文。"""
    body = soup.select_one("#js_content")
    shell = soup.select_one(".weui-msg, .page_msg, #js_verify, #verify, #js_pay_preview")
    text = shell.get_text(" ", strip=True) if shell else (soup.get_text(" ", strip=True) if not body else "")
    if (
        any(t in urlsplit(url).path.lower() for t in ("verify", "antispider"))
        or soup.select_one("#js_verify, #verify, #seccodeInput, #captcha")
        or ("环境异常" in text and ("去验证" in text or "完成验证" in text))
        or ("访问过于频繁" in text)
        or "请输入验证码" in text
        or "此验证码用于确认" in text
    ):
        return "anti_bot_verification"
    if any(
        word in text
        for word in ("该内容已被发布者删除", "此内容因违规无法查看", "该内容已被删除", "内容已删除", "该内容已不存在")
    ):
        return "article_deleted"
    if soup.select_one("#js_pay_preview, .paywall, #js_pay_read") or any(
        word in text for word in ("付费阅读", "付费后阅读全文", "购买后阅读全文")
    ):
        return "paid_preview"
    if not body and any(word in text for word in ("请先登录", "登录后查看", "需要登录后才能访问")):
        return "authentication_required"
    return "article" if body else "article_structure_unrecognized"


def parse_html(raw, url, *, transport="http", snapshot=None):
    from generic_web.collector import html_fragment_to_markdown

    soup = BeautifulSoup(raw or "", "lxml")
    state = page_state(soup, url)
    if state != "article":
        return failure(state, url=url)
    snapshot = snapshot or {}
    root = soup.select_one("#js_content")
    title = _field(soup, ("#activity-name", 'meta[property="og:title"]', "title")) or snapshot.get("title", "")
    account = _field(soup, ("#js_name", "#profileBt a", ".profile_nickname")) or snapshot.get("account_name", "")
    byline = _field(
        soup,
        (
            "#js_author_name",
            ".rich_media_meta_text:not(#js_name):not(#publish_time):not(#copyright_logo):not(.rich_media_meta_tag)",
        ),
    ) or snapshot.get("byline", "")
    published = _field(soup, ("#publish_time",)) or snapshot.get("published_at", "")
    script = "\n".join(node.get_text() for node in soup.find_all("script"))
    ts = re.search(r'(?:var\s+)?(?:create_time|publish_time)\s*[:=]\s*["\']?(\d{10,13})', script)
    if not published and ts:
        timestamp = int(ts[1]) / (1000 if len(ts[1]) == 13 else 1)
        try:
            published = datetime.fromtimestamp(timestamp, timezone(timedelta(hours=8))).isoformat()
        except (ValueError, OverflowError, OSError):
            pass
    manifest, links = [], []
    seen = set()
    for kind, selector, attrs in [
        ("image", "img", ("data-src", "src")),
        ("audio", "audio, mpvoice, mp-common-mpaudio, mp-common-claudio", ("src", "data-src")),
        ("video", "video, iframe, mp-video, mpvideo, mp-common-videosnap", ("src", "data-src")),
    ]:
        for index, node in enumerate(root.select(selector)):
            raw_url = next((str(node.get(a)) for a in attrs if node.get(a)), "")
            target = urljoin(url, html_lib.unescape(raw_url)) if raw_url else ""
            if not target and kind in {"audio", "video"}:
                manifest.append(
                    {
                        "type": kind,
                        "url": "",
                        "status": "unresolved",
                        "locator": f"wechat:{kind}:{index}",
                        "image_index": None,
                        "role": "article_content",
                        "media_access": "embedded_player_not_media",
                        "media_id": str(node.get("vid") or node.get("data-vid") or node.get("voice_encode_fileid") or ""),
                        "alt": "",
                    }
                )
                continue
            if target in seen or not target:
                continue
            seen.add(target)
            valid = bool(_parts(target))
            manifest.append(
                {
                    "type": kind,
                    "url": target if valid else "",
                    "status": "manifest_only" if valid else "unresolved",
                    "locator": f"wechat:{kind}:{index}",
                    "image_index": index if kind == "image" else None,
                    "role": "article_content",
                    "media_access": "direct_candidate"
                    if kind != "video" or node.name == "video"
                    else "embedded_player_not_media",
                    "media_id": str(node.get("vid") or node.get("data-vid") or node.get("voice_encode_fileid")
                                    or parse_qs(urlsplit(target).query).get("vid", [""])[0]),
                    "alt": str(node.get("alt") or ""),
                }
            )
            if kind == "image" and valid:
                node["src"] = target
    for node in root.select("a[href]"):
        target = urljoin(url, node["href"])
        if not _parts(target):
            continue
        attachment = bool(re.search(r"\.pdf(?:$|[?#])", target, re.I))
        links.append(
            {
                "url": target,
                "title": node.get_text(" ", strip=True),
                "relation": "attachment" if attachment else "cites",
                "status": "not_requested",
            }
        )
    # 已知代码行号列不属于文章代码；不能删除真正的数字代码。
    for node in root.select(".code-snippet__line-index"):
        node.decompose()
    from kr_core.source_urls import public_evidence_payload

    content = public_evidence_payload(html_fragment_to_markdown(str(root)))
    images = [r for r in manifest if r["type"] == "image"]
    if not content.strip() and not manifest:
        return failure("empty_detail", url=url)
    identity = article_identity(url)
    data = {
        "platform": "微信",
        "url": url,
        "title": title,
        "account_name": account,
        "byline": byline,
        "author": byline,
        "published_at": published,
        "published_time_precision": "page_observed" if published else "unknown",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "content": content,
        "status": "ok",
        "body_verified": True,
        "body_complete": True,
        "page_state": "article",
        "article_identity": identity,
        "collection_identity": {
            "contract": CONTRACT,
            "article_identity": identity,
            "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        },
        "media_manifest": manifest,
        "images": images,
        "outbound_links": links,
        "attachments": [r for r in links if r["relation"] == "attachment"],
        "original_mark": _field(soup, ("#copyright_logo",)),
        "source_presentation": PRESENTATION,
        "collector": "wechat_" + transport,
        "provenance": {
            "provider_id": "wechat",
            "transport": transport,
            "processing_method": "wechat_dom_to_markdown",
            "upstream_reference": UPSTREAM,
            "publisher": account,
            "author": byline,
            "published_at": published,
        },
        "comments_complete": False,
        "comments_coverage": "not_collected",
        "completed_stages": ["collection"],
    }
    if not content.strip():
        data["content_observation"] = "observed_empty_original_text"
    return data


# 只读当前页面选定的公开文章区域，不导出Cookie、存储或整页账号数据。
ARTICLE_EXPRESSION = r"""(() => {
 const root=document.querySelector('#js_content');
 const text=(s)=>document.querySelector(s)?.textContent?.trim()||'';
 const gate=document.querySelector('.weui-msg,.page_msg,#js_verify,#verify,#js_pay_preview');
 return {url:location.href,html:root ? '<html><h1 id="activity-name">'+
   (document.querySelector('#activity-name')?.innerHTML||'')+'</h1>'+
   '<div id="js_content">'+root.innerHTML+'</div>'+(gate?.outerHTML||'')+'</html>' :
   '<html><body>'+(gate?.outerHTML||document.body?.innerText?.slice(0,5000)||'')+'</body></html>',
   title:text('#activity-name'),account_name:text('#js_name'),byline:text('#js_author_name'),published_at:text('#publish_time')};
})()"""


def collect_opencli(url):
    from runtime.opencli_bridge import invoke_page

    result = invoke_page(
        session="wechat",
        operation="detail",
        url=url,
        expression=ARTICLE_EXPRESSION,
        wait_selector="#js_content,.weui-msg,.page_msg,#js_verify",
        isolated_work=True,
    )
    if not result.get("ok"):
        return failure(result.get("code", "opencli_failed"), url=url, transport_metadata=result.get("metadata", {}))
    snapshot = result.get("data") or {}
    final = snapshot.get("url", "")
    if not article_url(final):
        state = page_state(BeautifulSoup(snapshot.get("html", ""), "lxml"), final)
        return failure(
            state if state == "anti_bot_verification" else "article_redirect_unresolved",
            url=url,
            discovery_url=url,
            transport_metadata=result.get("metadata", {}),
        )
    data = parse_html(snapshot.get("html", ""), final, transport="opencli", snapshot=snapshot)
    data["discovery_url"] = url
    data["transport_metadata"] = result.get("metadata", {})
    return data


COMMENTS_EXPRESSION = r"""(() => {
 const visible=e=>!!e && e.getBoundingClientRect().width>0 && e.getBoundingClientRect().height>0;
 const gate=document.querySelector('#js_verify,#verify,.weui-msg,.page_msg');
 const gateText=gate?.innerText||'';
 const risk=!!gate && /验证|访问过于频繁|环境异常/.test(gateText);
 const candidates=[...document.querySelectorAll('#js_cmt_list,#js_comment_list,.discuss_container,#js_bt_cmt_container,.js_cmt_container')];
 const container=candidates.find(visible);
 const readerDiagnostic={visible_container:!!container,hidden_container_count:candidates.filter(e=>!visible(e)).length,
   hidden_load_failure:candidates.some(e=>!visible(e)&&/加载失败/.test(e.textContent||'')),
   visible_load_failure:!!container&&/加载失败/.test(container.innerText||''),
   client_prompt:/请在微信.*(?:查看|打开)|在微信中打开|即将前往微信打开此内容/.test(document.body.innerText||'')};
 const roots=container ? [...container.querySelectorAll('.discuss_item,[data-comment-id]')]
   .filter(e=>!e.parentElement.closest('.discuss_item,[data-comment-id]') && visible(e)) : [];
 const extract=(e,isReply=false,rootIndex=null)=>({
   native_comment_id:e.getAttribute('data-comment-id')||e.getAttribute('data-content-id')||null,
   content:(e.querySelector('.discuss_message_content,.comment_content,.discuss_content')||e).innerHTML,
   author:(e.querySelector('.discuss_nickname,.nickname,.comment_nickname')?.innerText||'').trim(),
   is_reply:isReply,root_index:rootIndex});
 const rows=[];
 roots.forEach((e,index)=>{rows.push(extract(e));
   e.querySelectorAll('.discuss_reply_item,.reply_item,.discuss_reply').forEach(reply=>{
     if(visible(reply))rows.push(extract(reply,true,index));
   });});
 const controls=container ? [...container.querySelectorAll('button,a,[role="button"]')]
   .filter(e=>visible(e)&&/展开|更多|加载/.test(e.innerText||'')) : [];
 return {url:location.href,risk,login:!!gate&&/登录后/.test(gateText),comments:rows,
   reader_diagnostic:readerDiagnostic,container_observed:!!container,root_count:roots.length,expanders_remaining:controls.length,
   empty_confirmed:!!container&&!roots.length&&/暂无留言|暂无评论/.test(container.innerText||''),
   exhausted:!!container&&/已显示全部留言|已加载全部评论|没有更多留言/.test(container.innerText||'')};
})()"""


def collect_comments(url, *, options=None, invoke=None):
    """仅普通读者页面可见留言；不调用公众号管理接口或索取owner凭据。"""
    from runtime.opencli_bridge import invoke_page
    from runtime.read_options import CommentReadOptions, normalized_options
    from runtime.comments_contract import normalize_comments
    scope=normalized_options(CommentReadOptions,options)
    result=(invoke or invoke_page)(session='wechat',operation='comments',url=url,
        expression=COMMENTS_EXPRESSION,wait_selector='#js_content,.weui-msg,.page_msg,#js_verify',
        steps=[{'expression':'(()=>{window.scrollTo(0,document.body.scrollHeight);return {clicked:true}})()',
                'wait':{'time':3,'timeout':5}},
               {'expression':'(()=>{const visible=e=>e&&e.getBoundingClientRect().width>0&&e.getBoundingClientRect().height>0;const panels=[...document.querySelectorAll("#js_cmt_list,#js_comment_list,.discuss_container,.js_cmt_container")];if(panels.some(visible))return {ready:true};const e=document.querySelector("#js_comment_bottom_text");if(!visible(e))return {clicked:false};e.click();return {clicked:true}})()',
                'wait':{'time':1,'timeout':3}}],isolated_work=True)
    if not result.get('ok'):
        return {'comments':[],'comments_complete':False,'comments_gap':result.get('code','reader_comments_failed'),
                'comments_metadata':{'transport':result.get('metadata',{}),'automatic_retry':False}}
    snapshot=result.get('data') or {}
    if not article_url(snapshot.get('url','')) or article_identity(snapshot['url']) != article_identity(url):
        return {'comments':[],'comments_complete':False,'comments_gap':'page_identity_changed'}
    rows=snapshot.get('comments') or []
    identity=hashlib.sha256(json.dumps(rows,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:24]
    normalized,roots=[],[]
    reply_counts={}
    for index,row in enumerate(rows[:scope['total_limit']]):
        item=dict(row)
        identifier=str(row.get('native_comment_id') or f'snapshot:{identity}:{index}')
        item.update(comment_id=identifier,id_scope='platform' if row.get('native_comment_id') else 'snapshot',snapshot_id=identity)
        root_index=item.pop('root_index',None)
        if not item.get('is_reply'):
            if len(roots)>=scope['root_limit']:
                break
            roots.append(identifier);item['root_comment_id']=identifier
        elif isinstance(root_index,int) and 0<=root_index<len(roots):
            if reply_counts.get(root_index,0)>=scope['reply_limit']:
                continue
            reply_counts[root_index]=reply_counts.get(root_index,0)+1
            item.update(root_comment_id=roots[root_index],parent_comment_id=roots[root_index],relationship_confidence='dom_direct_reply')
        normalized.append(item)
    diagnostic=snapshot.get('reader_diagnostic') or {}
    exposed=bool(snapshot.get('container_observed'))
    reader_gap=('reader_comments_load_failed' if diagnostic.get('visible_load_failure') else
                'reader_client_required' if diagnostic.get('client_prompt') else
                'reader_comments_not_exposed' if not exposed else 'reader_window_not_exhausted')
    empty=exposed and snapshot.get('empty_confirmed') is True and not diagnostic.get('visible_load_failure')
    exhausted=exposed and snapshot.get('exhausted') is True and not snapshot.get('expanders_remaining') and not diagnostic.get('visible_load_failure')
    complete=empty or (exhausted and len(normalized)==len(rows))
    data={'comments':normalized,'comments_complete':complete,'comments_exhausted':exhausted or empty,
          'comments_empty_confirmed':empty,'comments_coverage':'observed_zero_comments' if empty else 'visible_reader_window',
          'comments_gap':'' if complete else reader_gap,
          'comments_metadata':{'transport':result.get('metadata',{}),'reader_diagnostic':diagnostic,'automatic_retry':False,'root_count':len(roots),
            'pagination_unit':'single_visible_reader_snapshot; no_hidden_api_or_pagination_claim',
            'expanders_remaining':snapshot.get('expanders_remaining'),'source':'reader_visible_dom',
            'scope':'selected_visible_reader_comments; not_owner_moderation_queue'}}
    return normalize_comments(data,platform='微信',source_url=url,options=scope)


def collect_article(url, *, transport="http", timeout_s=30):
    from runtime.execution_context import check_execution

    if not matches_url(url):
        return failure("unsupported_url", url=url)
    check_execution()
    if transport == "opencli":
        return collect_opencli(url)
    from generic_web import GenericWebRequest, collect_url

    result = collect_url(GenericWebRequest(url=url, timeout=timeout_s, use_jina=False))
    if result.error:
        return failure(result.error["type"], url=url, message=result.error.get("message", ""))
    return (
        {**result.metadata["wechat"], "content": result.content}
        if result.metadata.get("wechat")
        else failure("article_structure_unrecognized", url=url)
    )
