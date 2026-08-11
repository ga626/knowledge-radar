"""National Science and Technology Report Service provider."""

from __future__ import annotations

from urllib.parse import quote

from .chinese_open_access import ChineseOpenAccessProvider, OpenAccessPlatformConfig
from .models import AcademicSearchRequest


class NstrsProvider(ChineseOpenAccessProvider):
    name = "nstrs"
    config = OpenAccessPlatformConfig(
        name="nstrs",
        display_name="国家科技报告服务系统",
        homepage="https://www.nstrs.cn/index",
        status="degraded",
        available=False,
        auto_enabled=False,
        access_mode="registered_online_view",
        full_text_access="registered_online_view_no_download",
        coverage="national science and technology reports",
        stable=False,
        degraded_reason=(
            "default TLS verification fails on this host due certificate-chain validation, while verify-disabled REST "
            "metadata works and reports 542440 records; platform guidance says实名注册 professional users can browse full "
            "reports online but cannot download/save full text"
        ),
        failure_category="tls_cert_chain_and_registered_online_view_only",
        requires_login=True,
        login_url="https://www.nstrs.cn/cas",
        manual_action=(
            "Register at https://www.nstrs.cn/nstrs/sys/register/termsOfRegister if online viewing is needed; "
            "do not route as fully automatic downloadable full text."
        ),
    )

    def search_urls(self, request: AcademicSearchRequest):
        query = quote(str(request.query or "").strip())
        if query:
            return (f"https://www.nstrs.cn/search?keyword={query}", self.config.homepage)
        return (self.config.homepage,)
