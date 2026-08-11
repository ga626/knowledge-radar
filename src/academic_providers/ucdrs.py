"""National Library Reference Consultation Alliance provider."""

from __future__ import annotations

from urllib.parse import quote

from .chinese_open_access import ChineseOpenAccessProvider, OpenAccessPlatformConfig
from .models import AcademicSearchRequest


class UcdrsProvider(ChineseOpenAccessProvider):
    name = "ucdrs"
    config = OpenAccessPlatformConfig(
        name="ucdrs",
        display_name="全国图书馆参考咨询联盟",
        homepage="http://www.ucdrs.superlib.net/",
        status="degraded",
        available=False,
        auto_enabled=False,
        access_mode="document_delivery",
        full_text_access="registered_document_delivery_not_direct_pdf",
        coverage="books, journals, theses, conference papers and reports via document delivery",
        stable=False,
        degraded_reason="free registration and document-delivery workflow; not suitable for fully automatic direct full-text retrieval",
        failure_category="document_delivery_not_direct_fulltext",
        requires_login=True,
        login_url="http://www.ucdrs.superlib.net/",
        manual_action="Complete user registration/login only for manual document-delivery requests; do not enable automatic full-text retrieval.",
    )

    def search_urls(self, request: AcademicSearchRequest):
        query = quote(str(request.query or "").strip())
        if query:
            return (f"http://www.ucdrs.superlib.net/search?sw={query}", self.config.homepage)
        return (self.config.homepage,)
