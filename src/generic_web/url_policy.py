"""通用网页目标策略：公开地址、逐跳检查、连接IP固定与有限正文。"""
from __future__ import annotations

import ipaddress
import re
import socket
import time
import threading
import ssl
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import proxy_bypass_environment
from dataclasses import dataclass

import httpx

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_DNS_CACHE = {}
_DNS_LOCK = threading.Lock()


def _public_dns_for_fake_ip(host):
    """仅Fake-IP域名走固定HTTPS解析器；绝不直接放行保留地址。"""
    from runtime.proxy_config import get_httpx_proxy
    with _DNS_LOCK:
        old = _DNS_CACHE.get(host)
        if old and old[0] > time.monotonic():
            return old[1]
    addresses = []
    # 固定两个解析器沿同一代理路线；首个网络不可达不再挡住全部Fake-IP网页。
    # 任一解析器返回私网或明确NXDOMAIN仍立即拒绝，不能挑一个方便的答案放行。
    for endpoint in ("https://cloudflare-dns.com/dns-query", "https://dns.alidns.com/resolve"):
        try:
            with httpx.Client(proxy=get_httpx_proxy(), trust_env=False, timeout=5, follow_redirects=False) as client:
                response = client.get(endpoint, params={"name": host, "type": "A"}, headers={"Accept": "application/dns-json"})
                response.raise_for_status()
                body = response.json()
            addresses = sorted({r["data"] for r in body.get("Answer", []) if r.get("type") in {1, 28}})
            if body.get("Status") == 3:
                raise URLPolicyError("target DNS name not found")
            if any(not ipaddress.ip_address(a).is_global for a in addresses):
                raise URLPolicyError("non-public target address forbidden")
            if body.get("Status") == 0 and addresses:
                break
            addresses = []
        except URLPolicyError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            addresses = []
    if not addresses:
        raise URLPolicyError("public DNS validation unavailable for proxy Fake-IP target")
    with _DNS_LOCK:
        if len(_DNS_CACHE) >= 256:
            _DNS_CACHE.clear()
        _DNS_CACHE[host] = (time.monotonic() + 30, addresses)
    return addresses


class URLPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class ReadOnlyPost:
    """适配器随代码声明的公开查询接口；不是 Agent 可随意指定的写入权限。"""
    endpoint: str
    fields: frozenset[str]
    max_body_bytes: int = 16 * 1024
    redirect_to_get: bool = False

    def encode(self, url, form, headers):
        parsed, _, _ = public_addresses(url, resolve=False)
        if (url != self.endpoint or parsed.scheme != 'https' or parsed.query or parsed.fragment
                or not isinstance(form, dict) or not form or set(form) - self.fields
                or any(not isinstance(k, str) or type(v) not in (str, int, bool) for k, v in form.items())
                or any(k.lower() in {'authorization','cookie','proxy-authorization'} for k in headers)):
            raise URLPolicyError('read_only_post_declaration_mismatch')
        content = urlencode(form).encode('utf-8')
        if not 0 < len(content) <= min(self.max_body_bytes, 64*1024):
            raise URLPolicyError('read_only_post_body_budget_exceeded')
        return content


def public_addresses(url: str, *, resolve: bool = True):
    if not url or re.search(r"[\s\\\x00-\x1f\x7f]", url):
        raise URLPolicyError("URL contains forbidden characters")
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (ValueError, UnicodeError) as exc:
        raise URLPolicyError("invalid URL authority") from exc
    if parsed.scheme not in {"http", "https"} or not host or parsed.username is not None or parsed.password is not None:
        raise URLPolicyError("only unauthenticated public HTTP(S) URLs are allowed")
    if port not in {80, 443, 8080, 8443}:
        raise URLPolicyError("target port not permitted")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan")) or "." not in host and ":" not in host:
        raise URLPolicyError("local hostname forbidden")
    try:
        ip = ipaddress.ip_address(host)
        addresses = [str(ip)]
    except ValueError:
        if not resolve:
            return parsed, host, []
        try:
            addresses = sorted({row[4][0] for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
        except OSError as exc:
            raise URLPolicyError("target DNS unavailable") from exc
        fake_network = ipaddress.ip_network("198.18.0.0/15")
        if addresses and all(ipaddress.ip_address(a).version == 4 and ipaddress.ip_address(a) in fake_network for a in addresses):
            addresses = _public_dns_for_fake_ip(host)
    if not addresses:
        raise URLPolicyError("target has no resolved address")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        underlying = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip
        if not underlying.is_global:
            raise URLPolicyError("non-public target address forbidden")
    return parsed, host, addresses


from kr_core.source_urls import SENSITIVE, redact_url, canonical_url, discovery_url, public_evidence_payload


DETAIL_DOMAINS = {"微信": ("mp.weixin.qq.com",), "小红书": ("xiaohongshu.com", "xhslink.com"), "知乎": ("zhihu.com",),
                  "BOSS直聘": ("zhipin.com",), "猎聘": ("liepin.com",),
                  "智联招聘": ("zhaopin.com",), "脉脉": ("maimai.cn",), "V2EX": ("v2ex.com",),
                  "B站": ("bilibili.com", "b23.tv"), "YouTube": ("youtube.com", "youtu.be")}


def validate_detail_target(url, platform=""):
    if platform == "B站" and re.fullmatch(r"(?:BV[a-zA-Z0-9]{10,12}|av\d+)", url):
        return
    _, host, _ = public_addresses(url, resolve=False)
    domains = DETAIL_DOMAINS.get(platform)
    if domains and not any(host == domain or host.endswith("." + domain) for domain in domains):
        raise URLPolicyError("detail platform and target host mismatch")




def reader_allowed(url):
    parsed, _, _ = public_addresses(url)
    # 带查询或凭据形态的地址留在本地获取链，不能发给第三方Reader。
    return not parsed.query and not parsed.fragment and not SENSITIVE.search(parsed.path)


class _TargetTLSContext(ssl.SSLContext):
    """CONNECT连已验IP时仍按原域名发送SNI并做证书主机校验。"""
    def __new__(cls, host):
        return super().__new__(cls, ssl.PROTOCOL_TLS_CLIENT)

    def __init__(self, host):
        import certifi
        self.load_verify_locations(cafile=certifi.where())
        self._target_host = host

    def wrap_socket(self, *args, server_hostname=None, **kwargs):
        return super().wrap_socket(*args, server_hostname=self._target_host, **kwargs)


def guarded_fetch(url, *, timeout=20, max_bytes=MAX_RESPONSE_BYTES, headers=None, follow_redirects=True,
                  readonly_post=None, form=None):
    original = url
    headers = dict(headers or {})
    method, body = 'GET', None
    if readonly_post is not None or form is not None:
        if not isinstance(readonly_post, ReadOnlyPost):
            raise URLPolicyError('read_only_post_declaration_required')
        body = readonly_post.encode(url, form, headers)
        method = 'POST'
        headers = {k:v for k,v in headers.items() if k.lower() not in {'content-type','content-length'}}
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    deadline = time.monotonic() + float(timeout)
    from runtime.execution_context import check_execution
    from runtime.proxy_config import get_httpx_proxy
    for _ in range(6):
        check_execution()
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise httpx.TimeoutException('acquisition deadline exceeded')
        parsed, host, addresses = public_addresses(url)
        check_execution()
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise httpx.TimeoutException('acquisition deadline exceeded during DNS')
        ip = addresses[0]
        ip_host = f"[{ip}]" if ":" in ip else ip
        port = parsed.port
        netloc = ip_host + (f":{port}" if port else "")
        pinned = urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))
        authority = f"[{host}]" if ":" in host else host
        if port:
            authority += f":{port}"
        # URL使用已验IP，TLS SNI与证书校验仍使用原主机名。
        # 代理选择按原域名处理 NO_PROXY，再连接已验证 IP；每次重定向都重算，
        # 不能因为固定 IP 而丢失域名的直连声明，也不放宽 DNS/重定向护栏。
        proxy = None if proxy_bypass_environment(authority) else get_httpx_proxy()
        with httpx.Client(timeout=remaining, follow_redirects=False, headers=headers, proxy=proxy, trust_env=False, verify=_TargetTLSContext(host)) as client:
            with client.stream(method, pinned, content=body, headers={"Host": authority}, extensions={"sni_hostname": host}) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    if method == 'POST':
                        target = urljoin(url, response.headers.get('location', ''))
                        # Some public search forms use POST/Redirect/GET. Only
                        # an opted-in same-origin 302/303 may discard the body;
                        # 307/308 never replay it, even on the same origin.
                        if (not readonly_post.redirect_to_get or response.status_code not in {302,303}
                                or not response.headers.get('location')
                                or urlsplit(target)[:2] != urlsplit(url)[:2]):
                            raise URLPolicyError('read_only_post_redirect_forbidden')
                        method, body = 'GET', None
                    location = response.headers.get("location")
                    if not location:
                        raise URLPolicyError("redirect without location")
                    target = urljoin(url, location)
                    public_addresses(target)
                    if urlsplit(url).scheme == "https" and urlsplit(target).scheme == "http":
                        raise URLPolicyError("HTTPS downgrade redirect forbidden")
                    if not follow_redirects:
                        return httpx.Response(response.status_code, headers={"location": target},
                                              request=httpx.Request(method, url))
                    if (urlsplit(url).hostname, urlsplit(url).port) != (urlsplit(target).hostname, urlsplit(target).port):
                        headers = {key: value for key, value in headers.items() if key.lower() not in {"authorization", "cookie", "proxy-authorization", "referer", "if-none-match", "if-modified-since"}}
                    url = target
                    continue
                body = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    check_execution()
                    if time.monotonic() >= deadline:
                        raise httpx.TimeoutException('acquisition deadline exceeded')
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise URLPolicyError("response exceeds bounded extraction size")
                decoded_headers = {k: v for k, v in response.headers.items() if k.lower() not in {"content-encoding", "content-length"}}
                return httpx.Response(response.status_code, headers=decoded_headers, content=bytes(body),
                                      request=httpx.Request(method, url), extensions={"original_url": redact_url(original)})
    raise URLPolicyError("redirect limit exceeded")
