"""SciOpen and CJSTP open full-text provider."""

from __future__ import annotations

from urllib.parse import quote

from .chinese_open_access import ChineseOpenAccessProvider, OpenAccessPlatformConfig
from .models import AcademicSearchRequest


class SciOpenProvider(ChineseOpenAccessProvider):
    name = "sciopen"
    config = OpenAccessPlatformConfig(
        name="sciopen",
        display_name="SciOpen/CJSTP",
        homepage="https://www.sciopen.com/",
        status="available",
        available=True,
        auto_enabled=True,
        full_text_access="direct_pdf_confirmed",
        coverage="Chinese scientific journals on SciOpen and CJSTP",
        stable=True,
        direct_pdf_samples=(
            "https://www.sciopen.com/local/article_pdf/10.16510/j.cnki.kjycb.20260311.002.pdf",
            "https://www.cjstp.cn/CN/article/downloadArticleFile.do?attachType=PDF&id=9276",
        ),
        pdf_url_markers=(".pdf", "downloadarticlefile", "article_pdf"),
    )

    def search_urls(self, request: AcademicSearchRequest):
        query = quote(str(request.query or "").strip())
        urls = []
        if query:
            urls.append(f"https://www.sciopen.com/search?keyword={query}")
        urls.append(self.config.homepage)
        return urls
