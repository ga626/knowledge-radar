"""ChinaXiv open preprint provider."""

from __future__ import annotations

from urllib.parse import quote

from .chinese_open_access import ChineseOpenAccessProvider, OpenAccessPlatformConfig
from .models import AcademicSearchRequest


class ChinaXivProvider(ChineseOpenAccessProvider):
    name = "chinaxiv"
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
    )

    def search_urls(self, request: AcademicSearchRequest):
        query = quote(str(request.query or "").strip())
        urls = []
        if query:
            urls.extend(
                [
                    f"https://chinaxiv.org/user/search.htm?field=title&value={query}",
                    f"https://chinaxiv.org/user/search.htm?field=keywords&value={query}",
                ]
            )
        urls.append(self.config.homepage)
        return urls
