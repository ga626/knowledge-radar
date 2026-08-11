"""nssd.cn mirror/canonicality probe provider."""

from __future__ import annotations

from .chinese_open_access import ChineseOpenAccessProvider, OpenAccessPlatformConfig


class NssdCnProvider(ChineseOpenAccessProvider):
    name = "nssd_cn"
    config = OpenAccessPlatformConfig(
        name="nssd_cn",
        display_name="NSSD.cn",
        homepage="https://www.nssd.cn/",
        status="degraded",
        available=False,
        auto_enabled=False,
        full_text_access="paused_domain_confirmed_source_elsewhere",
        coverage="possible alternate NSSD domain",
        stable=False,
        degraded_reason="www.nssd.cn currently serves a pause notice; ncpssd.org is the confirmed NSSD full-text source and remains the registered automatic provider",
        failure_category="paused_legacy_domain_confirmed_canonical_elsewhere",
    )
