"""NSSD / National Center for Philosophy and Social Sciences Documentation provider."""

from __future__ import annotations

from .chinese_open_access import ChineseOpenAccessProvider, OpenAccessPlatformConfig, encoded_nssd_search_url
from .models import AcademicSearchRequest


class NssdProvider(ChineseOpenAccessProvider):
    name = "nssd"
    config = OpenAccessPlatformConfig(
        name="nssd",
        display_name="国家哲学社会科学文献中心",
        homepage="https://www.ncpssd.org/",
        status="available",
        available=True,
        auto_enabled=True,
        full_text_access="direct_pdf_confirmed",
        coverage="Chinese philosophy and social sciences OA papers",
        stable=True,
        direct_pdf_samples=("https://ft.ncpssd.cn/pdf/getComm/npssd/pageStyle?url=pdf/1779066653843.pdf",),
        pdf_url_markers=(".pdf", "getcomm", "pagestyle"),
    )

    def search_urls(self, request: AcademicSearchRequest):
        query = str(request.query or "").strip()
        urls = []
        if query:
            urls.append(encoded_nssd_search_url(query))
        urls.append(self.config.homepage)
        return urls
