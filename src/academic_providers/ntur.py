"""National Taiwan University Repository provider."""

from __future__ import annotations

from urllib.parse import quote

from .chinese_open_access import ChineseOpenAccessProvider, OpenAccessPlatformConfig
from .models import AcademicSearchRequest


class NturProvider(ChineseOpenAccessProvider):
    name = "ntur"
    config = OpenAccessPlatformConfig(
        name="ntur",
        display_name="NTU Scholars / NTUR",
        homepage="https://scholars.lib.ntu.edu.tw/",
        status="degraded",
        available=False,
        auto_enabled=False,
        full_text_access="repository_pdf_unconfirmed_timeout_observed",
        coverage="National Taiwan University repository records",
        stable=False,
        degraded_reason="repository may expose item files, but current host/proxy could not open TCP 443 to scholars.lib.ntu.edu.tw / 140.112.113.38 during smoke tests",
        failure_category="current_environment_tcp443_route_timeout",
        pdf_url_markers=(".pdf", "bitstream", "download"),
    )

    def search_urls(self, request: AcademicSearchRequest):
        query = quote(str(request.query or "").strip())
        if query:
            return (f"https://scholars.lib.ntu.edu.tw/simple-search?query={query}", self.config.homepage)
        return (self.config.homepage,)
