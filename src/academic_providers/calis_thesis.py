"""CALIS thesis provider."""

from __future__ import annotations

from .chinese_open_access import ChineseOpenAccessProvider, OpenAccessPlatformConfig


class CalisThesisProvider(ChineseOpenAccessProvider):
    name = "calis_thesis"
    config = OpenAccessPlatformConfig(
        name="calis_thesis",
        display_name="CALIS高校学位论文库",
        homepage="https://etd2.calis.edu.cn/",
        status="degraded",
        available=False,
        auto_enabled=False,
        access_mode="document_delivery",
        full_text_access="institution_or_document_delivery",
        coverage="Chinese university theses and dissertations",
        stable=False,
        degraded_reason="search is public in some deployments, but full text is normally obtained through CALIS document delivery or institution authorization",
        failure_category="institution_or_document_delivery_required",
        requires_login=True,
        login_url="https://etd2.calis.edu.cn/",
        manual_action="Use only when the user has institutional authorization or document-delivery rights; not suitable for unattended full-text retrieval.",
    )
