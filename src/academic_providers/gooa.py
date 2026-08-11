"""GoOA open access discovery provider."""

from __future__ import annotations

from urllib.parse import quote

from .chinese_open_access import ChineseOpenAccessProvider, OpenAccessPlatformConfig
from .models import AcademicSearchRequest


class GoOaProvider(ChineseOpenAccessProvider):
    name = "gooa"
    config = OpenAccessPlatformConfig(
        name="gooa",
        display_name="GoOA",
        homepage="http://gooa.las.ac.cn/",
        status="degraded",
        available=False,
        auto_enabled=False,
        full_text_access="oa_discovery_unconfirmed",
        coverage="Chinese Academy of Sciences OA discovery service",
        stable=False,
        degraded_reason=(
            "the public help page documents an OAI-PMH metadata endpoint at /dspace-oai/request, but current Identify, "
            "ListSets, and ListMetadataFormats probes return 404; no stable search/PDF API or direct download link was captured"
        ),
        failure_category="metadata_oai_endpoint_unavailable_fulltext_unconfirmed",
        pdf_url_markers=(".pdf", "download"),
    )

    def search_urls(self, request: AcademicSearchRequest):
        query = quote(str(request.query or "").strip())
        if query:
            return (f"http://gooa.las.ac.cn/search?keyword={query}", self.config.homepage)
        return (self.config.homepage,)
