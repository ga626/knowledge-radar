"""猎聘：公开HTTP详情优先，动态页面借用日常Chrome。"""

from __future__ import annotations

import json
import logging
import os
import re
from urllib.parse import unquote
from typing import Dict, List, Optional

from generic_web import GenericWebRequest, collect_url
from runtime.recruitment_collection import CollectionTrace, format_search_error, format_search_response
from runtime.page_access_state import classify_page_access_state, merge_page_state
from runtime.recruitment_city import resolve_recruitment_city

log = logging.getLogger("mcp-server")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SELECTORS_PATH = os.path.join(PROJECT_ROOT, "config", "selectors.json")

def _format_search_response(platform: str, items: List[Dict], *, trace: CollectionTrace | None = None) -> Dict:
    return format_search_response(platform, items, trace=trace)


def _format_search_error(platform: str, error_item: Dict, *, trace: CollectionTrace | None = None, strategy: str = "") -> Dict:
    return format_search_error(platform, error_item, trace=trace, strategy=strategy)


def _finalize_liepin_search_state(data: Dict, city: str = "", limit: int = 15) -> Dict:
    raw_items = list(data.get("items", []) or [])
    items, city_filter = _apply_city_filter(raw_items, city)
    data["city_filter"] = city_filter

    page_state = dict(data.get("page_state") or {})
    blocked_marker = bool(data.get("blocked")) or bool(page_state.get("blocked_marker"))
    login_marker = bool(data.get("loginRequired")) or bool(page_state.get("login_marker"))
    card_count = int(data.get("cardCount") or page_state.get("card_count") or 0)
    job_link_count = int(data.get("jobLinkCount") or page_state.get("job_link_count") or 0)
    captcha_element_count = int(data.get("captchaElementCount") or page_state.get("captcha_element_count") or 0)
    blocking_modal_count = int(data.get("blockingModalCount") or page_state.get("blocking_modal_count") or 0)
    login_modal_count = int(data.get("loginModalCount") or page_state.get("login_modal_count") or 0)
    url_signal = str(data.get("urlSignal") or page_state.get("url_signal") or "")
    wait_state = data.get("waitState") if isinstance(data.get("waitState"), dict) else {}
    keyword = str(data.get("keyword") or "")
    if keyword:
        query_reflected = keyword in "\n".join(
            [
                unquote(str(data.get("url") or "")),
                str(data.get("title") or ""),
                str(data.get("textSample") or ""),
            ]
        )
    else:
        query_reflected = None
    security_strength = str(page_state.get("security_evidence_strength") or "")
    login_strength = str(page_state.get("login_evidence_strength") or "")
    if not security_strength:
        security_strength = (
            "strong"
            if captcha_element_count or blocking_modal_count or url_signal == "verification_redirect"
            else "weak" if blocked_marker else "none"
        )
    if not login_strength:
        login_strength = (
            "strong"
            if login_modal_count or url_signal == "login_redirect"
            else "weak" if login_marker else "none"
        )
    route_decision = (
        "network_items"
        if any(str(item.get("source") or "").endswith("_network_search") for item in raw_items)
        else "dom_items" if raw_items else "needs_route_repair"
    )
    classified = classify_page_access_state(
        platform="liepin",
        operation="search",
        blocked_marker=blocked_marker,
        login_marker=login_marker,
        captcha_element_count=captcha_element_count,
        result_item_count=len(raw_items),
        card_count=card_count,
        link_count=job_link_count,
        url_signal=url_signal,
        empty_marker=bool(wait_state.get("empty")),
        structured_list_expected=True,
        query_reflected=query_reflected,
        security_evidence_strength=security_strength,
        login_evidence_strength=login_strength,
        blocking_modal_count=blocking_modal_count,
        login_modal_count=login_modal_count,
        extra_signals={
            "raw_item_count": len(raw_items),
            "kept_item_count": len(items),
            "route_probe": {
                "query_reflected": query_reflected,
                "card_count": card_count,
                "job_link_count": job_link_count,
                "wait_empty": bool(wait_state.get("empty")),
                "route_decision": route_decision,
                "network_item_count": len([item for item in raw_items if str(item.get("source") or "").endswith("_network_search")]),
            },
            **page_state,
        },
    )
    data = merge_page_state(data, classified)

    if items:
        log.info(f"猎聘 CDP 搜索返回 {len(items)} 条结果")
        return {**data, "items": items[:limit], "status": "ok", "failure_type": ""}

    if raw_items and city and city_filter.get("mismatch_count"):
        return {
            **data,
            "items": [],
            "status": "empty",
            "failure_type": "city_mismatch",
            "manual_action_required": False,
        }

    if data.get("status") == "needs_interaction" and data.get("failure_type") == "platform_verification_required":
        log.warning("猎聘安全验证触发")
        return {**data, "items": []}

    if data.get("status") == "needs_interaction" and data.get("failure_type") == "login_required":
        log.info("猎聘需要登录")
        return {**data, "items": []}

    return {**data, "items": []}


from collectors.platform.job_domain import job_detail_quality as _job_detail_quality, _apply_city_filter


def _static_liepin_detail(url: str) -> Optional[Dict]:
    """Try open-web extraction before browser automation.

    Some Liepin enterprise job pages expose a complete static JD, while
    headhunter-style or personalized pages may not. The quality gate prevents
    navigation chrome from being treated as a successful detail.
    """
    try:
        from generic_web.url_policy import guarded_fetch
        from generic_web.collector import collect_rendered_html
        from collectors.platform.jobposting import parse_jobposting
        raw = guarded_fetch(url, timeout=20, headers={'User-Agent':'Mozilla/5.0','Accept':'text/html'})
        if raw.status_code == 429:
            gate = _site().record_rate_limit(raw.headers.get('retry-after'))
            return _site().failure('rate_limited', retry_after_s=gate['retry_after_s'])
        if raw.status_code >= 400:return None
        from urllib.parse import urlsplit
        if urlsplit(str(raw.url)).path != urlsplit(url).path:return None
        typed = parse_jobposting(raw.text)
        if typed:
            return {**typed,'status':'ok','platform':'猎聘','url':url,'content_type':'recruitment_job_detail',
                'metadata':{'transport':'http','external_reader':False,'extraction_source':'jobposting_jsonld'}}
        response = collect_rendered_html(GenericWebRequest(url=url,timeout=20,use_jina=False),
            html=raw.text,final_url=str(raw.url)).to_mcp_dict()
    except Exception as exc:
        log.debug(f"猎聘静态详情抽取异常: {exc}")
        return None

    content = str(response.get("content") or "")
    quality = _job_detail_quality(content)
    if not quality["ok"]:
        return None

    return {
        "platform": "猎聘",
        "url": str(response.get("final_url") or url),
        "title": str(response.get("title") or ""),
        "salary": "",
        "jd": content,
        "content": content,
        "status": "ok",
        "source": "liepin_static_web_detail",
        "detail_quality": quality,
        "collector": response.get("collector"),
        "metadata": {
            "transport": "http", "external_reader": False,
            "static_metadata": response.get("metadata") or {},
        },
    }



HOME = 'https://www.liepin.com/zhaopin/'
WAIT_SELECTOR = '.job-card-pc-container,[class*="job-card"],.job-intro-container,.job-description,.empty-content,[class*="captcha"],[class*="login-modal"]'
def _authenticated(data):
    return bool(not data.get('risk') and not data.get('login') and (data.get('authenticated_ui') or data.get('items') or data.get('content')))


def _site():
    from runtime.native_site import NativeSite
    return NativeSite('liepin', '猎聘', HOME, HOME, _authenticated)


def request_login(*,activate=False): return _site().request_login(activate=activate)
def complete_login(): return _site().complete_login()
def cancel_login(): return _site().cancel_login()
def probe_auth(): return _site().probe_auth()
def admission(): return _site().admission()
def runtime_identity(): return _site().runtime_identity()
def interaction_status(): return _site().interaction_status()


def search_url(keyword, city=''):
    from urllib.parse import urlencode
    resolution=resolve_recruitment_city('liepin',city)
    if city and resolution.get('status')=='missing':raise ValueError('city_mapping_missing')
    params={'key':keyword}
    if resolution.get('param_value'):params['dq']=resolution['param_value']
    return HOME+'?'+urlencode(params)


def _search(keyword,city='',limit=10):
    try:url=search_url(keyword,city)
    except ValueError:return {**_site().failure('city_mapping_missing'),'request_executed':False}
    response=_site().invoke('search',url=url)
    if not response.get('ok'):
        if response.get('failure_type')=='login_required':response['interaction']=request_login()
        return response
    data=response['data']
    from urllib.parse import parse_qs, urlsplit
    if parse_qs(urlsplit(data.get('url','')).query).get('key', [''])[0] != keyword:
        return _site().failure('search_query_not_applied')
    classified=_finalize_liepin_search_state({**data,"keyword":keyword,"waitState":{"empty":data.get("empty",False)}},city,limit)
    items=classified.get("items",[])
    city_filter=classified.get("city_filter",{})
    if not data.get('items') and not data.get('empty'):return _site().failure('page_structure_or_not_ready')
    items=items[:max(1,min(20,limit))]
    return {'platform':'猎聘','status':'ok' if items else 'empty','items':items,'total':len(items),
        'metadata':{**response.get('metadata',{}),'city_filter':city_filter,'pagination':{'page':1,'complete':False}}}


def legacy_search_liepin(keyword,city='',limit=10):
    from runtime.recruitment_governance import execute_operation
    return execute_operation('liepin','search',lambda:_search(keyword,city,limit),keyword=keyword,city=city)


def _detail(url):
    from urllib.parse import urlsplit
    target=urlsplit(url)
    if target.scheme!='https' or target.hostname not in {'www.liepin.com','liepin.com'} or not re.match(r'^/(job|a)/\d+(\.shtml)?/?$',target.path):
        return {**_site().failure('unsupported_url'),'request_executed':False}
    static=_static_liepin_detail(url)
    if static:return static
    from runtime.recruitment_governance import execute_browser_operation
    response=execute_browser_operation('liepin', lambda: _site().invoke('detail',url=url))
    if not response.get('ok'):
        if response.get('failure_type')=='login_required':response['interaction']=request_login()
        return response
    data=response['data']
    if urlsplit(data.get('url','')).path!=target.path:return _site().failure('page_identity_changed')
    quality=_job_detail_quality(data.get('content',''),extraction_source=data.get('extraction_source',''),title=data.get('title',''))
    if not quality['ok']:return {**_site().failure('empty_detail'),'detail_quality':quality}
    return {**data,'status':'ok','platform':'猎聘','content_type':'recruitment_job_detail','detail_quality':quality,'metadata':response.get('metadata',{})}


def detail(url):
    from runtime.recruitment_governance import execute_operation
    return execute_operation('liepin','detail',lambda:_detail(url), resource='http')
