"""Hans Publishers open full-text provider."""

from __future__ import annotations

from urllib.parse import quote

from .chinese_open_access import ChineseOpenAccessProvider, OpenAccessPlatformConfig
from .models import AcademicSearchRequest


class HansPubProvider(ChineseOpenAccessProvider):
    name = "hanspub"
    config = OpenAccessPlatformConfig(
        name="hanspub",
        display_name="汉斯出版社",
        homepage="https://www.hanspub.org/",
        status="available",
        available=True,
        auto_enabled=True,
        full_text_access="direct_pdf_confirmed",
        coverage="Hans open-access Chinese journals",
        stable=True,
        direct_pdf_samples=("https://pdf.hanspub.org/wer_2350658.pdf",),
        pdf_url_markers=(".pdf", "pdf.hanspub.org"),
    )

    def search_urls(self, request: AcademicSearchRequest):
        query = quote(str(request.query or "").strip())
        urls = []
        if query:
            urls.append(f"https://www.hanspub.org/search?kw={query}")
        urls.append(self.config.homepage)
        return urls
