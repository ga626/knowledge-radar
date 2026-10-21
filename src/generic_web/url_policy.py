"""通用网页目标策略：公开地址、逐跳检查、连接IP固定与有限正文。"""
from __future__ import annotations

import ipaddress
import re
import socket
import time
import threading
import ssl
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

SENSITIVE = re.compile(r"token|secret|password|passwd|authorization|credential|signature|api[_-]?key|access[_-]?key|session|cookie|auth|^code$", re.I)
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
    try:
        with httpx.Client(proxy=get_httpx_proxy(), trust_env=False, timeout=8, follow_redirects=False) as client:
            response = client.get("https://cloudflare-dns.com/dns-query", params={"name": host, "type": "A"}, headers={"Accept": "application/dns-json"})
            response.raise_for_status()
            body = response.json()
        addresses = sorted({r["data"] for r in body.get("Answer", []) if r.get("type") in {1, 28}})
        if body.get("Status") != 0 or not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
            raise ValueError("resolver returned no public address")
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise URLPolicyError("public DNS validation unavailable for proxy Fake-IP target") from exc
    with _DNS_LOCK:
        if len(_DNS_CACHE) >= 256:
            _DNS_CACHE.clear()
        _DNS_CACHE[host] = (time.monotonic() + 30, addresses)
    return addresses


class URLPolicyError(ValueError):
    pass


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


def redact_url(url):
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parsed.port:
            host += f":{parsed.port}"
        query = urlencode([(k, "[redacted]" if SENSITIVE.search(k) else v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)])
        return urlunsplit((parsed.scheme, host, parsed.path, query, ""))
    except ValueError:
        return "[invalid_url]"


def canonical_url(url: str) -> str:
    """公开身份不含访问令牌；原始 fetch URL 只留在取数调用内。"""
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"}:
            return url
        safe = urlsplit(redact_url(url))
        query = urlencode([(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if not SENSITIVE.search(k)])
        return urlunsplit((safe.scheme, safe.netloc, safe.path, query, ""))
    except (ValueError, TypeError):
        return "[invalid_url]"


DETAIL_DOMAINS = {"小红书": ("xiaohongshu.com", "xhslink.com"), "知乎": ("zhihu.com",),
                  "BOSS直聘": ("zhipin.com",), "猎聘": ("liepin.com",),
                  "B站": ("bilibili.com", "b23.tv"), "YouTube": ("youtube.com", "youtu.be")}


def validate_detail_target(url, platform=""):
    if platform == "B站" and re.fullmatch(r"(?:BV[a-zA-Z0-9]{10,12}|av\d+)", url):
        return
    _, host, _ = public_addresses(url, resolve=False)
    domains = DETAIL_DOMAINS.get(platform)
    if domains and not any(host == domain or host.endswith("." + domain) for domain in domains):
        raise URLPolicyError("detail platform and target host mismatch")


def public_evidence_payload(value):
    """在持久证据边界递归移除访问凭据，并清理文本内的受保护链接。"""
    if isinstance(value, dict):
        return {k: public_evidence_payload(v) for k, v in value.items()
                if not re.fullmatch(r"(?:fetch_url|xsec_token|access_token|refresh_token|api_key|authorization|cookie|cookies|password|secret)", str(k), re.I)}
    if isinstance(value, (tuple, list)):
        return [public_evidence_payload(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"https?://[^\s<>\"']+", lambda match: canonical_url(match.group()), value)
    return value


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


def guarded_fetch(url, *, timeout=20, max_bytes=MAX_RESPONSE_BYTES, headers=None, follow_redirects=True):
    original = url
    headers = dict(headers or {})
    from runtime.proxy_config import get_httpx_proxy
    for _ in range(6):
        parsed, host, addresses = public_addresses(url)
        ip = addresses[0]
        ip_host = f"[{ip}]" if ":" in ip else ip
        port = parsed.port
        netloc = ip_host + (f":{port}" if port else "")
        pinned = urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))
        authority = f"[{host}]" if ":" in host else host
        if port:
            authority += f":{port}"
        # URL使用已验IP，TLS SNI与证书校验仍使用原主机名。
        with httpx.Client(timeout=timeout, follow_redirects=False, headers=headers, proxy=get_httpx_proxy(), trust_env=False, verify=_TargetTLSContext(host)) as client:
            with client.stream("GET", pinned, headers={"Host": authority}, extensions={"sni_hostname": host}) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise URLPolicyError("redirect without location")
                    target = urljoin(url, location)
                    public_addresses(target)
                    if urlsplit(url).scheme == "https" and urlsplit(target).scheme == "http":
                        raise URLPolicyError("HTTPS downgrade redirect forbidden")
                    if not follow_redirects:
                        return httpx.Response(response.status_code, headers={"location": target},
                                              request=httpx.Request("GET", url))
                    if (urlsplit(url).hostname, urlsplit(url).port) != (urlsplit(target).hostname, urlsplit(target).port):
                        headers = {key: value for key, value in headers.items() if key.lower() not in {"authorization", "cookie", "proxy-authorization", "referer"}}
                    url = target
                    continue
                body = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise URLPolicyError("response exceeds bounded extraction size")
                decoded_headers = {k: v for k, v in response.headers.items() if k.lower() not in {"content-encoding", "content-length"}}
                return httpx.Response(response.status_code, headers=decoded_headers, content=bytes(body),
                                      request=httpx.Request("GET", url), extensions={"original_url": redact_url(original)})
    raise URLPolicyError("redirect limit exceeded")
