"""小红书兼容入口与短期私有签名交接；唯一采集实现为OpenCLI。"""
from collections import OrderedDict
import threading
import time
from urllib.parse import parse_qs, urlsplit, urlunsplit, urlencode

_PROTECTED_FETCH = OrderedDict()
_PROTECTED_FETCH_LOCK = threading.RLock()


def _remember_xhs_fetch(note_id, token, note_type="unknown"):
    from runtime.xhs_opencli import browser
    if not note_id:
        return
    with _PROTECTED_FETCH_LOCK:
        key=(browser().context_id,str(note_id))
        _PROTECTED_FETCH[key]=(time.monotonic()+600,str(token or ""),note_type)
        _PROTECTED_FETCH.move_to_end(key)
        while len(_PROTECTED_FETCH)>256:
            _PROTECTED_FETCH.popitem(last=False)


def _cached_xhs_record(note_id):
    from runtime.xhs_opencli import browser
    with _PROTECTED_FETCH_LOCK:
        key=(browser().context_id,str(note_id))
        record=_PROTECTED_FETCH.get(key)
        if record and record[0]>time.monotonic():
            return record
        _PROTECTED_FETCH.pop(key,None)
    return (0,"","unknown")


def _cached_xhs_token(note_id):
    return _cached_xhs_record(note_id)[1]


def _protect_xhs_search_fetch(items):
    for item in items:
        note_id=item.get("note_id") or item.get("metadata",{}).get("note_id")
        url=item.get("fetch_url") or item.get("url") or ""
        token=parse_qs(urlsplit(url).query).get("xsec_token",[""])[0]
        _remember_xhs_fetch(note_id,token,item.get("type") or item.get("content_type") or "unknown")
    return items


def protected_detail_url(url):
    import re
    parsed=urlsplit(url)
    match=re.fullmatch(r"/(?:explore|discovery/item)/([a-f0-9]{24})",parsed.path)
    query=parse_qs(parsed.query)
    if not match or query.get("xsec_token"):
        return url
    token=_cached_xhs_token(match[1])
    if not token:
        return url
    query.update(xsec_token=[token],xsec_source=["pc_search"])
    return urlunsplit(parsed._replace(query=urlencode(query,doseq=True)))


def legacy_search_xiaohongshu(keyword, limit=10, search_type="all", probe_mode=False):
    from runtime.xhs_opencli import search
    return search(keyword,limit,search_type)


def xiaohongshu_account_state(*_args, **_kwargs):
    from runtime.xhs_opencli import invoke
    result=invoke("session_probe")
    authenticated=bool(result.get("ok") and result.get("data",{}).get("authenticated_ui"))
    return {"authenticated":authenticated,"login_state":"authenticated" if authenticated else "unknown",
        "status":"ok" if authenticated else "needs_interaction","source":"opencli_daily_browser",
        "probe":result}
