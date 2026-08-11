"""Hong Kong Journals Online provider."""

from __future__ import annotations

from urllib.parse import quote

from .chinese_open_access import ChineseOpenAccessProvider, OpenAccessPlatformConfig
from .models import AcademicSearchRequest


class HkjoProvider(ChineseOpenAccessProvider):
    name = "hkjo"
    config = OpenAccessPlatformConfig(
        name="hkjo",
        display_name="Hong Kong Journals Online",
        homepage="https://hkjo.lib.hku.hk/",
        status="available",
        available=True,
        auto_enabled=False,
        full_text_access="direct_pdf_confirmed",
        coverage="Hong Kong journals, including Chinese-language publications",
        stable=True,
        degraded_reason=(
            "available but kept out of default Chinese auto order because coverage is Hong Kong journal specific"
        ),
        failure_category="",
        login_url="https://hkjo.lib.hku.hk/exhibits/show/hkjo/home",
        manual_action="Use explicit provider when Hong Kong journal coverage is desired.",
        pdf_url_markers=(".pdf", "/article/view/", "/article/download/"),
    )

    def search_urls(self, request: AcademicSearchRequest):
        query = quote(str(request.query or "").strip())
        if query:
            return (
                f"https://hkjo.lib.hku.hk/exhibits/show/hkjo/home?key={query}&type=aw&fryear=1872&toyear=2026&frmonth=01&tomonth=12&jtype=&norec=20&sortby=ti",
                self.config.homepage,
            )
        return (self.config.homepage,)
