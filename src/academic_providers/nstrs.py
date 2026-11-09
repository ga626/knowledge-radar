"""National Science and Technology Report Service provider."""

from __future__ import annotations

from generic_web.browser_runtime import owned_sync_browser

from urllib.parse import quote

from .chinese_open_access import ChineseOpenAccessProvider, OpenAccessPlatformConfig, clean_api_text
from .models import AcademicSearchRequest, AcademicWork


class NstrsProvider(ChineseOpenAccessProvider):
    name = "nstrs"
    config = OpenAccessPlatformConfig(
        name="nstrs",
        display_name="国家科技报告服务系统",
        homepage="https://www.nstrs.cn/index",
        status="degraded",
        available=True,
        auto_enabled=True,
        access_mode="public_abstract_search_registered_fulltext",
        full_text_access="registered_online_view_no_download",
        coverage="national science and technology reports",
        stable=False,
        degraded_reason=(
            "公众检索通过官网浏览器页面请求；实名与文章权限逐项核验。KR当前只提供摘要/在线浏览线索，不自动下载或持久归档全文；这不是全平台统一收费/下载政策声明。"
        ),
        failure_category="",
        requires_login=False,
        login_url="https://www.nstrs.cn/cas",
        manual_action=(
            "Register at https://www.nstrs.cn/nstrs/sys/register/termsOfRegister if online viewing is needed; "
            "do not route as fully automatic downloadable full text."
        ),
    )

    def search_urls(self, request: AcademicSearchRequest):
        query = str(request.query or "").strip()
        if query:
            # Match the official UI's double encodeURI, preserving its colon.
            return (f"https://www.nstrs.cn/kjbg/SearchResult?wd={quote(query)}&q=All:{quote(quote(query))}",)
        return (self.config.homepage,)

    def search(self, request):
        from .chinese_open_access import OpenAccessResults, _fetch_diagnostic
        from runtime.asyncio_isolation import run_sync_in_worker_if_asyncio
        try:
            payload = run_sync_in_worker_if_asyncio(lambda: self._browser_search(request), timeout_s=65,
                                                     thread_name_prefix="kr-nstrs")
        except Exception as exc:
            from runtime.execution_context import ExecutionStopped
            if isinstance(exc, ExecutionStopped):
                raise
            diagnostic = _fetch_diagnostic(exc)
            diagnostic["type"] = "browser_search_timeout" if "Timeout" in type(exc).__name__ else "browser_search_failed"
            return OpenAccessResults([], [diagnostic])
        result = payload.get('RESULT')
        rows = result.get('list') if isinstance(result, dict) else payload.get('list')
        if not isinstance(rows, list):
            return OpenAccessResults([], [{'type': 'search_response_unparsed', 'retryable': False}])
        def work(row):
            if not row.get('id') or not row.get('title'):
                return None
            return AcademicWork(title=clean_api_text(row['title']),
                url='https://www.nstrs.cn/kjbg/detail?id=' + quote(str(row['id']), safe=''),
                abstract=clean_api_text(row.get('abstractCn') or row.get('abstract')),
                authors=[part.strip() for part in str(row.get('creator') or '').split(';') if part.strip()],
                year=int(row['proposalDate']) if str(row.get('proposalDate') or '').isdigit() else None,
                source=self.config.display_name,
                source_database=self.name, access_mode=self.config.access_mode,
                document_type='report',
                full_text_status='delayed_publication' if row.get('publicScope') == 2 else 'registered_online_view_no_download',
                verification_status='public_search_record',
                raw={'query': request.query, 'id': row['id'], 'public_scope': row.get('publicScope'),
                     'fulltext_download_allowed': False, 'download_allowed': False,
                     'evidence_storage_allowed': False, 'cloud_processing_allowed': False})
        works = [work(row) for row in rows if isinstance(row, dict)]
        works = [item for item in works if item]
        return OpenAccessResults(works[:max(1, min(request.limit, 20))], [] if works else [
            {'type': 'search_response_unparsed' if rows else 'no_match', 'retryable': False}])

    def _browser_search(self, request):
        """让官网执行其检索请求，仅接受同一查询的官方响应。"""
        from playwright.sync_api import sync_playwright
        from urllib.parse import parse_qs
        query = str(request.query or '').strip()
        if not query:
            raise ValueError('NSTRS requires a search query')
        url = self.search_urls(request)[0]
        def matches(response):
            return (response.url.split('?')[0] == 'https://www.nstrs.cn/rest/kjbg/wfKjbg/search'
                    and parse_qs(response.request.post_data or '').get('q') == ['All:' + quote(query)])
        with sync_playwright() as p, owned_sync_browser(p) as browser:
            page = browser.new_page(locale='zh-CN')
            # 官网真实响应约 25 秒，单次有界等待；不循环重试慢接口。
            with page.expect_response(matches, timeout=45000) as observed:
                page.goto(url, wait_until='domcontentloaded', timeout=20000)
            response = observed.value
            if not response.ok:
                raise RuntimeError(f'NSTRS HTTP {response.status}')
            return response.json()
