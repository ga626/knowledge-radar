"""COAJ provider."""

from __future__ import annotations

from .chinese_open_access import ChineseOpenAccessProvider, OpenAccessPlatformConfig


class CoajProvider(ChineseOpenAccessProvider):
    name = "coaj"
    config = OpenAccessPlatformConfig(
        name="coaj",
        display_name="COAJ",
        homepage="https://www.coaj.cn/home",
        status="degraded",
        available=False,
        auto_enabled=False,
        full_text_access="official_api_requires_known_doi_unconfirmed",
        coverage="Chinese open-access journal aggregation",
        stable=False,
        degraded_reason=(
            "official API docs expose public basic article metadata at /api/v1/open/article/basic, but search/detail "
            "routes can be gated and no public PDF/full-text API endpoint was confirmed"
        ),
        failure_category="metadata_available_fulltext_endpoint_unconfirmed",
        login_url="https://www.coaj.cn/login",
        manual_action="Treat COAJ as a discovery/metadata source until a public full-text URL or authenticated detail flow is validated.",
        landing_samples=("https://www.coaj.cn/api/v1/docs",),
    )

    def search_urls(self, request):
        return (self.config.homepage, "https://www.coaj.cn/api/v1/docs")
