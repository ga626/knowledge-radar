"""Runtime proxy discovery and safe health reporting."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Optional
from urllib.parse import urlsplit, urlunsplit


PROXY_ENV_KEYS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY")
NO_PROXY_KEYS = ("NO_PROXY", "no_proxy")


@dataclass(frozen=True)
class ProxyConfig:
    source: str = ""
    url: str = ""
    no_proxy: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def httpx_proxy(self) -> Optional[str]:
        return self.url or None

    def playwright_proxy(self) -> Optional[Dict[str, str]]:
        if not self.url:
            return None
        parsed = urlsplit(self.url)
        if not parsed.scheme or not parsed.hostname:
            return {"server": self.url}
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        if parsed.port:
            host = f"{host}:{parsed.port}"
        payload: Dict[str, str] = {"server": f"{parsed.scheme}://{host}"}
        if parsed.username:
            payload["username"] = parsed.username
        if parsed.password:
            payload["password"] = parsed.password
        payload["bypass"] = ",".join(dict.fromkeys(["localhost", "127.0.0.0/8", "[::1]", *[s.strip() for s in self.no_proxy.split(",") if s.strip()]]))
        return payload

    def summary(self) -> Dict[str, object]:
        return {
            "status": "ok" if self.configured else "not_configured",
            "configured": self.configured,
            "source": self.source,
            "proxy": _redact_proxy_url(self.url),
            "no_proxy_configured": bool(self.no_proxy),
            "no_proxy": _redact_no_proxy(self.no_proxy),
            "formats": {
                "httpx": self.configured,
                "playwright": self.configured,
            },
        }


def get_runtime_proxy(url: str = '') -> ProxyConfig:
    keys = ('HTTP_PROXY', 'ALL_PROXY') if urlsplit(url).scheme == 'http' else PROXY_ENV_KEYS
    if urlsplit(url).scheme == 'https':
        keys = ('HTTPS_PROXY', 'ALL_PROXY')
    for key in keys:
        value = os.environ.get(key) or os.environ.get(key.lower())
        if value:
            return ProxyConfig(source=key, url=value.strip(), no_proxy=_read_no_proxy())
    windows_proxy = _read_windows_user_proxy()
    if windows_proxy:
        return ProxyConfig(source="windows_user_proxy", url=windows_proxy, no_proxy=_read_no_proxy())
    return ProxyConfig(no_proxy=_read_no_proxy())


def get_httpx_proxy() -> Optional[str]:
    return get_runtime_proxy().httpx_proxy()


def get_yt_dlp_proxy() -> Optional[str]:
    return get_runtime_proxy().httpx_proxy()


def proxy_health_summary() -> Dict[str, object]:
    return get_runtime_proxy().summary()


def _read_no_proxy() -> str:
    for key in NO_PROXY_KEYS:
        value = os.environ.get(key)
        if value:
            return value.strip()
    return ""


def _read_windows_user_proxy() -> str:
    if os.name != "nt":
        return ""
    if os.environ.get("KR_DISABLE_WINDOWS_PROXY_FALLBACK", "").strip().lower() in {"1", "true", "yes", "on"}:
        return ""
    try:
        import winreg

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        enabled = bool(winreg.QueryValueEx(key, "ProxyEnable")[0])
        if not enabled:
            return ""
        proxy_server = str(winreg.QueryValueEx(key, "ProxyServer")[0] or "").strip()
        if not proxy_server:
            return ""
        if "=" in proxy_server:
            per_scheme = {}
            for item in proxy_server.split(";"):
                if "=" not in item:
                    continue
                name, value = item.split("=", 1)
                per_scheme[name.strip().lower()] = value.strip()
            proxy_server = per_scheme.get("https") or per_scheme.get("http") or next(iter(per_scheme.values()), "")
        if not proxy_server:
            return ""
        return proxy_server if "://" in proxy_server else f"http://{proxy_server}"
    except Exception:
        return ""


def _redact_proxy_url(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme or "proxy"
        if parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
            return f"{scheme}://<local-proxy>"
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:<port>"
        netloc = f"<proxy-host>" if not host else host
        if parsed.username or parsed.password:
            netloc = f"***:***@{netloc}"
        return urlunsplit((parsed.scheme, netloc, "", "", ""))
    except Exception:
        return "***"


def _redact_no_proxy(value: str) -> str:
    if not value:
        return ""
    parts = [item.strip() for item in value.split(",") if item.strip()]
    if len(parts) <= 8:
        return ", ".join(parts)
    return ", ".join(parts[:8]) + f", ...(+{len(parts) - 8})"


def _route(url, config=None):
    """选择配置不探测出口；只对显式NO_PROXY/回环直连，不猜国家或重试换路。"""
    import ipaddress
    from urllib.request import proxy_bypass_environment
    parsed = urlsplit(str(url))
    host = (parsed.hostname or '').rstrip('.').lower()
    if parsed.scheme not in {'http', 'https'} or not host:
        raise ValueError('proxy_route_requires_http_url')
    config = config or get_runtime_proxy(str(url))
    try:
        ip = ipaddress.ip_address(host)
        loopback = ip.is_loopback or bool(getattr(ip, 'ipv4_mapped', None) and ip.ipv4_mapped.is_loopback)
    except ValueError:
        loopback = host == 'localhost' or host.endswith('.localhost')
    bypass = proxy_bypass_environment(parsed.netloc, {'no': config.no_proxy})
    reason = 'loopback' if loopback else 'no_proxy' if bypass else 'configured_proxy' if config.url else 'no_proxy_configured'
    proxy = config.url if not (loopback or bypass) and config.url else None
    return proxy, {'schema': 'knowledgeradar-network-route/v1', 'decision': 'proxy' if proxy else 'direct',
                   'reason': reason, 'source': config.source or 'none', 'target_host': host,
                   'scope': 'selection_not_verified_egress', 'rules': 'advisory_only'}


def proxy_decision(url):
    return _route(url)[1]


def _record_route(decision, adapter):
    from runtime.tool_trace import record_trace_child
    record_trace_child('network_route', metadata={**decision, 'adapter': adapter})


def httpx_options(url, *, adapter='httpx'):
    """适用于已按跳校验URL的调用方；不能用固定IP替代原域名作分流。"""
    proxy, decision = _route(url)
    _record_route(decision, adapter)
    return {'proxy': proxy, 'trust_env': False}


def http_client(**kwargs):
    """非固定IP客户端逐请求/重定向分流；禁止失败后自动直连。"""
    import httpx
    if any(key in kwargs for key in ('proxy', 'transport', 'mounts', 'trust_env')):
        raise ValueError('proxy_transport_owned_by_runtime')
    verify = kwargs.pop('verify', True)
    limits = kwargs.pop('limits', httpx.Limits())
    class RoutingTransport(httpx.BaseTransport):
        def __init__(self):
            self.configs = {scheme: get_runtime_proxy(scheme + '://route.invalid') for scheme in ('http', 'https')}
            self.transports = {}

        def handle_request(self, request):
            proxy, decision = _route(str(request.url), self.configs[request.url.scheme])
            _record_route(decision, 'httpx-routed')
            if proxy not in self.transports:
                self.transports[proxy] = httpx.HTTPTransport(proxy=proxy, verify=verify, limits=limits, trust_env=False)
            response = self.transports[proxy].handle_request(request)
            response.extensions['kr_network_route'] = decision
            return response

        def close(self):
            for transport in self.transports.values():
                transport.close()
    return httpx.Client(transport=RoutingTransport(), trust_env=False, **kwargs)
