"""Taiwan Open Access Journals provider."""

from __future__ import annotations

from .chinese_open_access import ChineseOpenAccessProvider, OpenAccessPlatformConfig


class ToajProvider(ChineseOpenAccessProvider):
    name = "toaj"
    config = OpenAccessPlatformConfig(
        name="toaj",
        display_name="臺灣學術期刊開放取用平台",
        homepage="https://toaj.stpi.niar.org.tw/",
        status="degraded",
        available=False,
        auto_enabled=False,
        full_text_access="official_fulltext_download_claim_current_route_timeout",
        coverage="Taiwan open-access Chinese journals",
        stable=False,
        degraded_reason=(
            "official pages describe free article search and full-text download, but DNS resolves to 203.145.193.121 "
            "and this host plus the configured proxy both time out on TCP/TLS 443, so the current route cannot validate PDFs"
        ),
        failure_category="current_environment_tcp443_route_timeout",
        pdf_url_markers=(".pdf", "download"),
    )
