"""IVY Publisher provider."""

from __future__ import annotations

from urllib.parse import quote

from .chinese_open_access import ChineseOpenAccessProvider, OpenAccessPlatformConfig
from .models import AcademicSearchRequest


class IvyPublisherProvider(ChineseOpenAccessProvider):
    name = "ivy_publisher"
    config = OpenAccessPlatformConfig(
        name="ivy_publisher",
        display_name="IVY Publisher",
        homepage="http://www.ivypub.org/",
        status="available",
        available=True,
        auto_enabled=False,
        full_text_access="direct_pdf_confirmed",
        coverage="IVY open journal pages, mostly English journals with Chinese UI",
        stable=True,
        degraded_reason="not in default Chinese auto chain because broad Chinese-language coverage is limited",
        pdf_url_markers=(".pdf", "full paper"),
    )

    def search_urls(self, request: AcademicSearchRequest):
        query = quote(str(request.query or "").strip())
        if query:
            return (f"http://www.ivypub.org/search?key={query}", "http://www.ivypub.org/sjeb")
        return ("http://www.ivypub.org/sjeb", self.config.homepage)
