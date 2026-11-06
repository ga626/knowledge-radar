"""脉脉：公开分享HTTP优先，授权社区借用日常Chrome。"""
from __future__ import annotations
import json
import re
from urllib.parse import urlencode, urlsplit, quote
from typing import Dict
COMMUNITY_HOME = 'https://maimai.cn/community/home/recommended'
ME_PATH = '/community/api/common/get-user-info'
SEARCH_PATH = '/community/api/common/search'

def _failure(kind, message, *, manual=False):
    from runtime.recruitment_collection import format_search_error
    error_type = {"auth_probe_inconclusive": "auth_state_unconfirmed",
                  "auth_probe_error": "request_failed", "csrf_handshake_failed": "request_failed"}.get(kind, kind)
    error = {"error": message, "failure_type": kind, "platform_state": kind,
             "manual_action_required": manual, "type": error_type}
    return {"platform": "脉脉", "status": "needs_interaction" if manual else "failed",
            "failure_type": kind, "platform_state": kind, "auth_state": kind,
            "manual_action_required": manual, "error": format_search_error("脉脉", error)["error"], "content": "", "items": [],
            "recommended_action": "health_check(mode='request_browser_interaction:maimai:manual_login')" if manual else ""}


def _is_maimai(url):
    parsed = urlsplit(url)
    return parsed.scheme in {"https", "http"} and parsed.hostname == "maimai.cn" and not parsed.username and not parsed.password


def _is_discussion(url):
    parsed = urlsplit(url)
    return _is_maimai(url) and (parsed.path == "/article/detail" or parsed.path.startswith(
        ("/web/gossip_detail", "/content/", "/community/feed-detail/", "/community/gossip-detail/")))


def _auth_from_probe(probe, url="", text=""):
    data = probe.get("data") if isinstance(probe.get("data"), dict) else {}
    if probe.get("http_status") == 200 and data.get("result") == "ok" and isinstance(data.get("user"), dict) and data["user"]:
        return {"platform": "maimai", "status": "ok", "auth_state": "community_api_authenticated",
                "manual_action_required": False, "authenticated": True}
    if probe.get("http_status") == 429:
        return _failure("rate_limited", "脉脉限制了访问频率，请稍后重试。")
    if data.get("error_type") == "CSRF_TOKEN_MISMATCH":
        return _failure("csrf_handshake_failed", "脉脉请求校验握手失败，请检查采集协议；这不能证明账号已退出。")
    if any(marker in text for marker in ("确认您是真人", "安全验证", "滑动验证", "访问被拦截")):
        return _failure("platform_verification_required", "脉脉需要本人完成安全验证。", manual=True)
    if "/platform/login" in url or probe.get("http_status") == 401 or data.get("error_code") in {401, "401", "not_login"}:
        return _failure("login_required", "请在日常Chrome的脉脉工作标签完成手机号验证码或扫码登录。", manual=True)
    return _failure("auth_probe_inconclusive", "未取得脉脉社区登录成功证据；不会因 Cookie 存在而判为成功。")


def _search_items(data, limit):
    """仅接受讨论分区；联系人不作为研究帖子，保留详情必需的 efid/egid。"""
    items, seen = [], set()
    for section, kind in (("feeds", "feed"), ("gossips", "gossip")):
        rows = data.get(section)
        if isinstance(rows, dict):
            rows = rows.get(section, rows.get("list", rows.get("data")))
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            row = _discussion_row(row, kind)
            identifier = str(row.get("id") or "")
            encrypted_key = "efid" if kind == "feed" else "egid"
            encrypted = row.get(encrypted_key)
            if not identifier or not encrypted or (kind, identifier) in seen:
                continue
            seen.add((kind, identifier))
            text = re.sub(r"<[^>]+>", "", str(row.get("text") or row.get("content") or row.get("title") or "")).strip()
            if not text:
                continue
            url = "https://maimai.cn/community/" + kind + "-detail/" + quote(identifier, safe="") + "?" + urlencode({encrypted_key: encrypted, "fid" if kind == "feed" else "gid": identifier})
            items.append({"title": text[:100], "desc": text[:500], "url": url, "platform": "脉脉",
                          "content_type": "community_discussion_candidate", "body_verified": False, "job_verified": False,
                          "published_at": row.get("create_time") or row.get("time") or "",
                          "metadata": {"kind": kind, "id": identifier, encrypted_key: encrypted}})
    return items[:limit]


def _discussion_row(row, kind):
    nested = next((row[key] for key in ("feed", "gossip", "style44", "style35", "style1") if isinstance(row.get(key), dict)), {})
    merged = {**row, **nested}
    merged["id"] = merged.get("fid") or merged.get("gid") or merged.get("id")
    if kind == "gossip" and merged.get("id"):
        merged["id"] = str(merged["id"]).removeprefix("gossip_")
    if not merged.get("egid"):
        target = str(row.get("target") or "")
        merged["egid"] = (parse_target_query(target).get("egid") or [""])[0]
    merged["text"] = merged.get("text") or merged.get("content") or merged.get("summary") or merged.get("title") or merged.get("t") or ""
    return merged


def parse_target_query(target):
    from urllib.parse import parse_qs
    return parse_qs(urlsplit(target).query)


def _flight_objects(html, key):
    """读取页面中已到达的 Next Flight 数据，不执行页面脚本。"""
    decoder = json.JSONDecoder()
    chunks = []
    for match in re.finditer(r"self\.__next_f\.push\(", html):
        try:
            value, _ = decoder.raw_decode(html[match.end():])
            if isinstance(value, list) and len(value) > 1 and isinstance(value[1], str):
                chunks.append(value[1])
        except ValueError:
            continue
    text = "".join(chunks)
    for match in re.finditer('"' + re.escape(key) + r'"\s*:\s*', text):
        try:
            value, _ = decoder.raw_decode(text[match.end():])
            yield value
        except ValueError:
            continue


def _detail_data(html, url):
    path = urlsplit(url).path
    identifier = path.rstrip("/").split("/")[-1]
    key = "feedData" if "/feed-detail/" in path else "data"
    for data in _flight_objects(html, key):
        if not isinstance(data, dict):
            continue
        data = _discussion_row(data, "feed" if key == "feedData" else "gossip")
        row_id = str(data.get("id") or "")
        if row_id == identifier:
            return data
    return {}


def public_discussion_detail(url: str) -> Dict:
    """Read an anonymous share, never turn a login shell into discussion evidence."""
    from urllib.parse import urlsplit
    from generic_web import collect_url, GenericWebRequest
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or
            not (parsed.hostname == "maimai.cn" or (parsed.hostname or "").endswith(".maimai.cn")) or
            not (parsed.path == "/article/detail" or parsed.path.startswith(("/web/gossip_detail", "/content/")))):
        return {"platform": "脉脉", "url": url, "error": "仅支持公开社区分享链接；社区时间线需要用户授权登录。",
                "failure_type": "unsupported_url", "content": ""}
    response = collect_url(GenericWebRequest(url=url, use_jina=False, timeout=20))
    result = response.to_mcp_dict()
    target = urlsplit(response.final_url or url)
    text = response.content.strip()
    if "/login" in target.path or (len(text) < 300 and any(s in text for s in ("验证码登录", "扫码登录", "登录脉脉"))):
        return {"platform": "脉脉", "url": url, "content": "", "status": "needs_interaction",
                "error": "该讨论需要手机号验证码或扫码登录；未取得讨论正文。", "failure_type": "login_required",
                "manual_action_required": True}
    if response.error or len(text) < 80:
        return {**result, "platform": "脉脉", "content": "", "status": "failed",
                "error": response.error or "公开分享正文不足", "failure_type": (response.error or {}).get("type", "empty_detail")}
    return {**result, "platform": "脉脉", "status": "ok", "content_type": "community_discussion",
            "body_verified": True, "job_verified": False, "comments_collected": False,
            "note": "公开分享正文；封闭社区时间线与评论未采集，内容时效和相关性由 Codex 判断。"}


def _authenticated(data):
    api_verified = _auth_from_probe(data.get('probe') or {}).get('status') == 'ok' if 'probe' in data else False
    return bool(not data.get('risk') and not data.get('login') and
        (api_verified or data.get('authenticated_api')))


def _site():
    from runtime.native_site import NativeSite
    return NativeSite('maimai', '脉脉', COMMUNITY_HOME, COMMUNITY_HOME, _authenticated)


def admission(): return _site().admission()
def runtime_identity(): return _site().runtime_identity()
def interaction_status(): return _site().interaction_status()
def request_login(*,activate=False): return _site().request_login(activate=activate)
def complete_login(): return _site().complete_login()
def cancel_login(): return _site().cancel_login()
def probe_auth(): return _site().probe_auth()
def probe_maimai_auth_state(*,existing_only=False): return probe_auth()


def _response_failure(probe):
    data=probe.get('data') if isinstance(probe.get('data'),dict) else {}
    status=probe.get('http_status',0)
    if status==429:
        from runtime.state_file import write_state
        from runtime.file_write_lock import file_write_lock
        with file_write_lock(_site().state_path()):
            write_state(_site().state_path(),{**_site().state(),'risk':True,'reason':'rate_limited_remote'})
        return _failure('rate_limited','脉脉限制访问频率，停止采集并交人工。',manual=True)
    if status==401 or data.get('error_code') in {401,'401','not_login'}:
        result=_failure('login_required','请在日常Chrome完成脉脉登录。',manual=True)
        result['interaction']=request_login();return result
    if data.get('error_type')=='CSRF_TOKEN_MISMATCH':return _failure('csrf_handshake_failed','同源校验握手失败；不代表退出登录。')
    if status==403:return _failure('access_denied','脉脉拒绝访问；没有证据证明重新登录即可解决。')
    if status!=200:return _failure('search_response_unparsed','脉脉未返回有效社区数据。')
    return None


def _search(keyword,limit):
    response=_site().invoke('search', url=COMMUNITY_HOME, keyword=keyword, limit=max(1,min(limit,20)))
    if not response.get('ok'):
        if response.get('failure_type')=='login_required':response['interaction']=request_login()
        return response
    probe=response['data'].get('probe',{})
    failure=_response_failure(probe)
    if failure:return failure
    data=probe.get('data',{})
    if not isinstance(data,dict) or data.get('result')!='ok' or not any(k in data for k in ('feeds','gossips')):
        return _failure('search_response_unparsed','社区检索业务错误或结构变化，不能当作零讨论。')
    sections=[data[k] for k in ('feeds','gossips') if k in data]
    if not all(isinstance(v,(dict,list)) for v in sections):return _failure('search_response_unparsed','讨论分区结构异常。')
    items=_search_items(data,limit)
    has_rows=any(v if isinstance(v,list) else (v.get('feeds') or v.get('gossips') or v.get('list') or v.get('data')) for v in sections)
    if has_rows and not items:return _failure('search_response_unparsed','返回讨论缺少正文或可定位标识。')
    return {'platform':'脉脉','status':'ok' if items else 'empty','items':items,'total':len(items),
        'metadata':{**response.get('metadata',{}),'strategy':'borrowed_community_search','pagination_supported':False,
            'note':'仅当前账号可见讨论第一页；非职位列表，不承诺全量。'}}


def legacy_search_maimai(keyword,limit=10):
    from runtime.recruitment_governance import execute_operation
    return execute_operation('maimai','search',lambda:_search(keyword,limit),keyword=keyword)


def _detail(url):
    if not urlsplit(url).path.startswith('/community/'):
        public=public_discussion_detail(url)
        if public.get('status')=='ok':return public
        if public.get('failure_type') not in {'login_required','empty_detail'}:return public
    from runtime.recruitment_governance import execute_browser_operation
    response=execute_browser_operation('maimai',lambda:_site().invoke('detail',url=url))
    if not response.get('ok'):
        if response.get('failure_type')=='login_required':response['interaction']=request_login()
        return {**response,'url':url}
    payload=response['data']
    if urlsplit(payload.get('url','')).path!=urlsplit(url).path:return _failure('page_identity_changed','帖子跳转至其他页面，停止取数。')
    data=_detail_data(payload.get('html',''),url)
    text=str(data.get('text') or data.get('content') or payload.get('dom_content') or '').strip()
    if not text:return _failure('detail_response_unparsed','未取得帖子正文：结构变化、不可见或帖子失效；不能当作零内容。')
    return {'platform':'脉脉','status':'ok','url':url,'title':str(data.get('title') or text[:100]),'content':text,
        'content_type':'community_discussion','body_verified':True,'job_verified':False,'comments_collected':False,
        'metadata':{**response.get('metadata',{}),'html_truncated':bool(payload.get('html_truncated')),
            'body_source':'matched_flight' if data else 'visible_dom','coverage':'visible_discussion_not_full_archive'}}


def _comments(url,limit):
    # 沿用本次详情标签，不重新导航；每次原生展开前后均执行站点风险观察。
    response=_site().invoke('comments', url=url, limit=limit)
    if not response.get('ok'):return response
    if urlsplit(response['data'].get('url','')).path!=urlsplit(url).path:return _failure('page_identity_changed','评论工作页已变更，停止。')
    comments=[];seen=set()
    for row in response['data'].get('comments',[]):
        if row.get('content') and row['content'] not in seen:comments.append(row);seen.add(row['content'])
    data=response['data']; empty=bool(data.get('comments_empty_confirmed'))
    return {'status':'ok','comments':comments[:limit],'comments_collected':bool(comments) or empty,'comments_complete':empty,
        'comments_status':'visible_comments_partial' if comments else 'empty_confirmed' if empty else
            'comments_loading_incomplete' if data.get('comments_loading') else 'comments_unavailable_or_schema_changed',
        'comments_note':'仅本人可见且已展开评论；滚动触发懒加载，最多两次继续展开，不把未识别当作零评论。','metadata':response.get('metadata',{})}


def discussion_detail(url,*,include_comments=False,comment_limit=20):
    if not _is_discussion(url):return _failure('unsupported_url','请提供脉脉讨论详情链接。')
    from runtime.recruitment_governance import execute_operation
    result=execute_operation('maimai','detail',lambda:_detail(url),resource='http')
    if include_comments and result.get('status')=='ok':
        from runtime.resource_concurrency import ResourceDeferred
        try:
            comments=execute_operation('maimai','comments',lambda:_comments(url,max(1,min(comment_limit,50))))
        except ResourceDeferred as exc:
            # The body has already succeeded. Keep it instead of replaying collection for optional comments.
            result.update(comments=[],comments_collected=False,comments_complete=False,
                comments_status='resource_busy',comments_failure={'failure_type':'resource_busy',
                    'waiting_resource':exc.resource_kind,'request_executed':False})
            return result
        if comments.get('error'):
            result.update(comments=[],comments_collected=False,comments_complete=False,
                comments_status=comments.get('failure_type','comments_request_failed'),comments_failure=comments)
        else:result.update({k:v for k,v in comments.items() if k not in {'status','metadata'}})
    return result
