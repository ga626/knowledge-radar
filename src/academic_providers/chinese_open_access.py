"""Chinese open full-text academic provider helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
import base64
import html
import re
from typing import Iterable, List, Sequence
from urllib.parse import quote, urljoin

import httpx

from .models import AcademicSearchRequest, AcademicWork


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
}


class ChineseOpenAccessError(RuntimeError):
    def __init__(self, diagnostics):
        super().__init__('provider_fetch_failed')
        self.diagnostics = diagnostics


class OpenAccessResults(list):
    def __init__(self, items, diagnostics):
        super().__init__(items)
        self.diagnostics = diagnostics


def _fetch_diagnostic(exc):
    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
    kind = ('rate_limited' if status == 429 else 'access_denied' if status in {401, 403}
            else 'provider_http_error' if status else 'timeout' if isinstance(exc, httpx.TimeoutException)
            else 'transport_error' if isinstance(exc, httpx.RequestError) else 'parse_error')
    diagnostic = {'type': kind, 'http_status': status, 'message': type(exc).__name__,
                  'retryable': status not in {400, 401, 402, 403, 404}, 'backoff_suggested': status == 429}
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        # 不保留query、Authorization、响应全文或任意上游message。
        diagnostic['request_origin'] = str(response.request.url.copy_with(path='/', query=None, fragment=None))
        retry = response.headers.get('Retry-After', '').strip()
        if retry.isdigit() and len(retry) <= 10:
            diagnostic['retry_after_seconds'] = int(retry)
        elif retry:
            from email.utils import parsedate_to_datetime
            from datetime import datetime, timezone
            try:
                deadline = parsedate_to_datetime(retry)
                if deadline.tzinfo is not None:
                    diagnostic['retry_after_seconds'] = max(0, int((deadline - datetime.now(timezone.utc)).total_seconds()))
            except (ValueError, TypeError, OverflowError):
                pass
        try:
            payload = response.json() if len(response.content) <= 65536 else {}
        except (ValueError, httpx.ResponseNotRead):
            payload = {}
        code = str(payload.get('code') or payload.get('error_code') or '') if isinstance(payload, dict) else ''
        if re.fullmatch(r'[A-Z0-9_.:-]{1,80}', code):
            diagnostic['provider_code'] = code
    return diagnostic


def public_json_search(provider, request, fetch_records, make_work):
    """Run a verified public search API; missing schema is never a zero hit."""
    from runtime.proxy_config import get_httpx_proxy
    try:
        with httpx.Client(timeout=provider.timeout_s, follow_redirects=True, headers=DEFAULT_HEADERS,
                          proxy=get_httpx_proxy(), trust_env=False) as client:
            records = fetch_records(client)
        if not isinstance(records, list):
            return OpenAccessResults([], [{'type': 'search_response_unparsed', 'retryable': False,
                                          'message': '检索响应缺少已验证的记录列表'}])
        works = [make_work(row) for row in records if isinstance(row, dict)]
        works = [work for work in works if work and work.title and work.url]
        if records and not works:
            return OpenAccessResults([], [{'type': 'search_response_unparsed', 'retryable': False,
                                          'message': '检索返回记录，但文章字段不符合合同'}])
        return OpenAccessResults(works[:max(1, min(request.limit, 20))], [] if works else [
            {'type': 'no_match', 'retryable': False, 'message': '已执行主题检索，接口返回空记录列表'}])
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        return OpenAccessResults([], [_fetch_diagnostic(exc)])


def json_response(response):
    response.raise_for_status()
    return response.json()


def clean_api_text(value):
    return html.unescape(re.sub(r'<[^>]+>', '', str(value or ''))).strip()


@dataclass(frozen=True)
class OpenAccessPlatformConfig:
    name: str
    display_name: str
    homepage: str
    status: str = "degraded"
    available: bool = False
    auto_enabled: bool = False
    access_mode: str = "open_full_text"
    full_text_access: str = "unknown"
    coverage: str = ""
    stable: bool = False
    degraded_reason: str = ""
    failure_category: str = ""
    requires_login: bool = False
    login_url: str = ""
    manual_action: str = ""
    direct_pdf_samples: Sequence[str] = field(default_factory=tuple)
    landing_samples: Sequence[str] = field(default_factory=tuple)
    pdf_url_markers: Sequence[str] = field(default_factory=lambda: (".pdf", "type=pdf", "attachtype=pdf", "download"))
    article_url_markers: Sequence[str] = field(default_factory=tuple)


class ChineseOpenAccessProvider:
    """Best-effort HTML/PDF provider for open Chinese academic sources."""

    name = ""
    config: OpenAccessPlatformConfig
    timeout_s = 8.0
    max_pages = 3

    def status(self) -> dict:
        config = self.config
        return {
            "configured": True,
            "available": config.available,
            "requires_api_key": False,
            "network": "public_web",
            "status": config.status,
            "auto_enabled": config.auto_enabled,
            "access_mode": config.access_mode,
            "full_text_access": config.full_text_access,
            "coverage": config.coverage,
            "stable": config.stable,
            "homepage": config.homepage,
            "degraded_reason": config.degraded_reason,
            "failure_category": config.failure_category,
            "requires_login": config.requires_login,
            "login_url": config.login_url,
            "manual_action": config.manual_action,
        }

    def search(self, request: AcademicSearchRequest) -> List[AcademicWork]:
        limit = max(1, min(int(request.limit or 5), 20))
        works: List[AcademicWork] = []
        seen = set()
        diagnostics = []
        completed = 0
        from runtime.proxy_config import get_httpx_proxy
        with httpx.Client(timeout=self.timeout_s, follow_redirects=True, headers=DEFAULT_HEADERS,
                          proxy=get_httpx_proxy(), trust_env=False) as client:
            for url in list(self.search_urls(request))[: self.max_pages]:
                try:
                    collected = self._collect_from_url(client, url, request, seen, limit)
                    works.extend(collected)
                    diagnostics.extend(getattr(collected, 'diagnostics', []))
                    completed += 1
                except (httpx.HTTPError, ValueError) as exc:
                    diagnostics.append({**_fetch_diagnostic(exc), 'url': url})
                if len(works) >= limit:
                    break
        if diagnostics and not completed:
            raise ChineseOpenAccessError(diagnostics)
        return OpenAccessResults(works[:limit], diagnostics)

    def search_urls(self, request: AcademicSearchRequest) -> Sequence[str]:
        return (self.config.homepage,)

    def _collect_from_url(
        self,
        client: httpx.Client,
        url: str,
        request: AcademicSearchRequest,
        seen: set,
        limit: int,
    ) -> List[AcademicWork]:
        response = client.get(url)
        return self._collect_response(response, url, request, seen, limit)

    def _collect_response(self, response, url, request, seen, limit):
        response.raise_for_status()

        content_type = response.headers.get("content-type", "").lower()
        if "pdf" in content_type or response.content[:5] == b"%PDF-":
            if response.url and str(response.url) not in seen:
                seen.add(str(response.url))
                return [
                    self._make_work(
                        title=f"{self.config.display_name} PDF",
                        url=str(response.url),
                        request=request,
                        full_text_status="direct_pdf",
                        confidence=0.9,
                        raw={"source_url": url, "content_type": content_type},
                    )
                ]
            return []

        text = response.text
        if any(marker in text for marker in ('initAliyunCaptcha', 'denied by UA ACL', 'Forbid_code:', 'Security Verification')) or re.search(r"var\s+arg1\s*=\s*['\"][A-Fa-f0-9]+['\"]", text):
            return OpenAccessResults([], [{'type': 'access_verification_required', 'url': str(response.url),
                'http_status': response.status_code, 'retryable': False,
                'message': '检索页返回安全验证、脚本 Cookie 挑战或访问控制页面，未到文章解析层'}])
        if re.search(r'<title[^>]*>[^<]*(?:维护|暂停服务)[^<]*</title>', text, re.I):
            return OpenAccessResults([], [{'type': 'provider_maintenance', 'url': str(response.url),
                'http_status': response.status_code, 'retryable': False, 'message': '站点标题明确为维护或暂停服务公告'}])
        if re.search(r'<title[^>]*>\s*404\s*</title>', text, re.I):
            return OpenAccessResults([], [{'type': 'search_route_unavailable', 'url': str(response.url),
                'http_status': response.status_code, 'retryable': False, 'message': 'HTTP 200 但页面标题为 404；检索路由不可用'}])
        results: List[AcademicWork] = []
        for link_url, label in self._extract_links(text, str(response.url)):
            if link_url in seen:
                continue
            if not self._looks_like_full_text_link(link_url, label):
                continue
            seen.add(link_url)
            results.append(
                self._make_work(
                    title=label or f"{self.config.display_name} full-text link",
                    url=link_url,
                    request=request,
                    full_text_status="direct_pdf" if self._looks_like_pdf_url(link_url, label) else "article_landing_unverified",
                    confidence=0.78,
                    raw={"source_url": url, "link_label": label},
                )
            )
            if len(results) >= limit:
                break
        if not results:
            visible = re.sub(r'<(script|style)\b.*?</\1>', '', text, flags=re.I | re.S)
            visible = re.sub(r'<[^>]+>', ' ', visible)
            explicit_empty = bool(re.search(r'没有找到相关结果|未找到.*(?:文献|论文|结果)|暂无(?:搜索)?结果|no\s+(?:search\s+)?results\s*(?:found)?', visible, re.I))
            homepage_only = str(url).rstrip('/') == self.config.homepage.rstrip('/')
            kind = 'search_not_implemented' if homepage_only else 'no_match' if explicit_empty else 'search_response_unparsed'
            return OpenAccessResults([], [{'type': kind, 'url': str(response.url), 'http_status': response.status_code,
                'retryable': False, 'message': '仅扫描首页，未执行主题检索' if homepage_only else '检索页明确报告无匹配' if explicit_empty else 'HTTP 成功但未解析出文章；可能是脚本壳、路由或解析规则不适配，不能判定无内容'}])
        if str(url).rstrip('/') == self.config.homepage.rstrip('/'):
            return OpenAccessResults(results, [{'type': 'search_not_implemented', 'url': str(response.url),
                'retryable': False, 'message': '候选来自首页，未执行主题检索；需要 Codex 另行判断相关性'}])
        return results

    def _extract_links(self, text: str, base_url: str) -> Iterable[tuple[str, str]]:
        pattern = re.compile(r"<a\b[^>]*?href=[\"'](?P<href>[^\"']+)[\"'][^>]*>(?P<label>.*?)</a>", re.I | re.S)
        for match in pattern.finditer(text):
            href = html.unescape(match.group("href")).strip()
            if not href or href.startswith(("javascript:", "#", "mailto:")):
                continue
            label = re.sub(r"<[^>]+>", " ", match.group("label"))
            label = html.unescape(re.sub(r"\s+", " ", label)).strip()
            yield urljoin(base_url, href), label

    def _looks_like_full_text_link(self, url: str, label: str) -> bool:
        if len(label.strip()) > 8 and not url.split('#', 1)[-1].lower() == 'references' and any(
            marker.lower() in url.lower() for marker in self.config.article_url_markers
        ):
            return True
        lowered_label = label.lower()
        if any(marker in lowered_label for marker in ["pdf", "full paper", "全文", "下载"]):
            return True
        if re.search(r"\bdownload\b", lowered_label):
            return True
        return self._looks_like_pdf_url(url, label)

    def _looks_like_pdf_url(self, url: str, label: str = "") -> bool:
        lowered = f"{url} {label}".lower()
        return any(marker.lower() in lowered for marker in self.config.pdf_url_markers)

    def _make_work(
        self,
        *,
        title: str,
        url: str,
        request: AcademicSearchRequest,
        full_text_status: str,
        confidence: float,
        raw: dict,
    ) -> AcademicWork:
        return AcademicWork(
            title=_clean_title(title) or f"{self.config.display_name} result",
            url=url,
            source=self.config.display_name,
            oa_status="open" if full_text_status in {"direct_pdf", "open_landing_page"} else "",
            source_database=self.config.name,
            document_type={'chinaxiv': 'preprint', 'nssd': 'journal-article',
                           'hkjo': 'journal-article', 'toaj': 'journal-article'}.get(self.config.name, 'unknown'),
            access_mode=self.config.access_mode,
            full_text_status=full_text_status,
            provider_confidence=confidence,
            verification_status="provider_link_extracted",
            license_scope="unknown",
            degraded_reason="" if self.config.available else self.config.degraded_reason,
            raw={"query": request.query, **raw},
        )


def encoded_nssd_search_url(query: str) -> str:
    q = str(query or "").strip()
    expression = f'(IKTE="{q}" OR IKPYTE="{q}" OR IKST="{q}" OR IKET="{q}" OR IKSE="{q}")'
    search = base64.b64encode(expression.encode("utf-8")).decode("ascii")
    search_name = base64.b64encode(f'题名/关键词="{q}"'.encode("utf-8")).decode("ascii")
    return (
        "https://www.ncpssd.org/Literature/articlelist?"
        f"sType=0&search={quote(search)}&searchname={quote(search_name)}&nav=0&showBack=true"
    )


def _clean_title(value: str) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"\s+", " ", text).strip(" -|")
    if len(text) > 180:
        return text[:177].rstrip() + "..."
    return text
