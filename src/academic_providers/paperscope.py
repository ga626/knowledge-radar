"""PaperScope discovery provider."""

from __future__ import annotations

from urllib.parse import quote

from .chinese_open_access import ChineseOpenAccessProvider, OpenAccessPlatformConfig
from .models import AcademicSearchRequest


class PaperScopeProvider(ChineseOpenAccessProvider):
    name = "paperscope"
    config = OpenAccessPlatformConfig(
        name="paperscope",
        display_name="PaperScope",
        homepage="https://www.paperscope.ai/",
        status="degraded",
        available=False,
        auto_enabled=False,
        full_text_access="discovery_and_ai_reading_not_chinese_fulltext_source",
        coverage="AI paper discovery and reading layer",
        stable=False,
        degraded_reason="current paperscope.ai probes redirect to a domain-sale landing page, so it is not a usable Chinese academic full-text provider",
        failure_category="domain_retired_or_unavailable_for_academic_fulltext",
    )

    def search_urls(self, request: AcademicSearchRequest):
        query = quote(str(request.query or "").strip())
        if query:
            return (f"https://www.paperscope.ai/search?q={query}", self.config.homepage)
        return (self.config.homepage,)
