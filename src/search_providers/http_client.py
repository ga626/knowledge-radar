"""有界连接池；鉴权逐请求传递，不保存响应 Cookie，不随 timeout 生成无限客户端。"""
from __future__ import annotations
import atexit
from collections import OrderedDict
from contextlib import contextmanager
from http.cookiejar import CookieJar, DefaultCookiePolicy
import threading
from urllib.parse import urlsplit
from urllib.request import proxy_bypass_environment
import httpx

_CLIENTS = OrderedDict()
_LOCK = threading.RLock()
MAX_CLIENTS = 12

class _RejectCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False

@contextmanager
def _lease(url):
    from runtime.search_configuration import current_configuration
    from runtime.proxy_config import get_httpx_proxy
    parsed = urlsplit(url)
    proxy = None if proxy_bypass_environment(parsed.netloc) else get_httpx_proxy()
    key = (current_configuration().generation, parsed.scheme, parsed.hostname, parsed.port, proxy)
    temporary = False
    with _LOCK:
        row = _CLIENTS.get(key)
        if row is None:
            if len(_CLIENTS) >= MAX_CLIENTS:
                idle = next((k for k,v in _CLIENTS.items() if v['users'] == 0), None)
                if idle is not None:
                    _CLIENTS.pop(idle)['client'].close()
                else:
                    temporary = True
            client = httpx.Client(timeout=15, follow_redirects=False, proxy=proxy, trust_env=False,
                cookies=httpx.Cookies(CookieJar(policy=_RejectCookies())),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4, keepalive_expiry=20))
            # HTTPX copies a supplied Cookies instance into its own default jar.
            # Set policy on the actual client jar, not just the constructor input.
            client.cookies.jar.set_policy(_RejectCookies())
            row = {'client':client,'users':0}
            if not temporary:
                _CLIENTS[key] = row
        row['users'] += 1
    try:
        yield row['client']
    finally:
        with _LOCK:
            row['users'] -= 1
            if temporary:
                row['client'].close()


def request_json(method, url, *, timeout, headers=None, **kwargs):
    from runtime.search_configuration import check_search_send
    from runtime.execution_context import current_execution
    import time
    check_search_send()
    execution = current_execution()
    if execution:
        timeout = min(timeout, max(.01, execution['deadline_at']-time.time()))
    with _lease(url) as client:
        # build_request avoids a mutable default header/cookie identity in the pool.
        request = client.build_request(method.upper(),url,headers=headers,timeout=timeout,**kwargs)
        response = client.send(request,follow_redirects=False)
        response.raise_for_status()
        if 300 <= response.status_code < 400:
            raise ValueError('provider_redirect_not_permitted')
        check_search_send()
        return response.json()


def close_shared_clients():
    with _LOCK:
        clients = [row['client'] for row in _CLIENTS.values()]
        _CLIENTS.clear()
    for client in clients:
        client.close()


def shared_client_summary():
    with _LOCK:
        return {'schema':'knowledgeradar-shared-http-clients/v1','status':'ok',
                'client_count':len(_CLIENTS),'max_clients':MAX_CLIENTS,
                'active_leases':sum(row['users'] for row in _CLIENTS.values()),'cookie_storage':False}

atexit.register(close_shared_clients)
