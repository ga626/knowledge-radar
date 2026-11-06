"""ChinaXiv open preprint provider."""

from __future__ import annotations


from .chinese_open_access import ChineseOpenAccessProvider, OpenAccessPlatformConfig, DEFAULT_HEADERS
from .models import AcademicSearchRequest
from generic_web.url_policy import ReadOnlyPost, guarded_fetch


class ChinaXivProvider(ChineseOpenAccessProvider):
    name = "chinaxiv"
    search_post = ReadOnlyPost('https://chinaxiv.org/user/search.htm', frozenset({'keywordbanner'}), redirect_to_get=True)
    config = OpenAccessPlatformConfig(
        name="chinaxiv",
        display_name="ChinaXiv",
        homepage="https://chinaxiv.org/home.htm",
        status="available",
        available=True,
        auto_enabled=True,
        full_text_access="direct_pdf_confirmed",
        coverage="Chinese and English preprints hosted by ChinaXiv",
        stable=True,
        direct_pdf_samples=("https://chinaxiv.org/user/download.htm?uuid=5dc4324c4aa645f3b43ecc6b2d784ba3",),
        pdf_url_markers=(".pdf", "download.htm"),
        article_url_markers=("/abs/", "/user/article.htm?"),
    )

    def search_urls(self, request: AcademicSearchRequest):
        return ('https://chinaxiv.org/user/search.htm',)

    def _collect_from_url(self, client, url, request, seen, limit):
        response = guarded_fetch(url, readonly_post=self.search_post,
            form={'keywordbanner':request.query.strip()}, timeout=self.timeout_s,
            max_bytes=4*1024*1024, headers={**DEFAULT_HEADERS,'Referer':self.config.homepage})
        return self._collect_response(response, url, request, seen, limit)

    def _extract_links(self, text, base_url):
        # Download buttons have generic labels. Use the sibling article title
        # instead so keyword relevance does not throw away valid preprints.
        from bs4 import BeautifulSoup
        from urllib.parse import urljoin
        soup = BeautifulSoup(text, 'html.parser')
        for anchor in soup.select('a[href]'):
            href = anchor.get('href', '')
            if '/abs/' in href or 'article.htm?' in href:
                yield urljoin(base_url, href), anchor.get_text(' ', strip=True)
