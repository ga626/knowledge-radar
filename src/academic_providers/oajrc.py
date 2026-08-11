"""OAJRC open journal provider."""

from __future__ import annotations

from .chinese_open_access import ChineseOpenAccessProvider, OpenAccessPlatformConfig


class OajrcProvider(ChineseOpenAccessProvider):
    name = "oajrc"
    config = OpenAccessPlatformConfig(
        name="oajrc",
        display_name="OAJRC",
        homepage="https://www.oajrc.org/",
        status="available",
        available=True,
        auto_enabled=True,
        full_text_access="direct_pdf_confirmed",
        coverage="OAJRC open journal papers",
        stable=True,
        direct_pdf_samples=("https://ije.oajrc.org/ArticleDetail.aspx?cid=4257&type=PDF",),
        pdf_url_markers=(".pdf", "type=pdf", "fileupload/pdffile"),
    )
