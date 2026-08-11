"""China Sciencepaper Online provider."""

from __future__ import annotations

from .chinese_open_access import ChineseOpenAccessProvider, OpenAccessPlatformConfig
from .models import AcademicSearchRequest


class PaperEduProvider(ChineseOpenAccessProvider):
    name = "paper_edu"
    config = OpenAccessPlatformConfig(
        name="paper_edu",
        display_name="中国科技论文在线",
        homepage="https://www.paper.edu.cn/",
        status="degraded",
        available=False,
        auto_enabled=False,
        full_text_access="site_maintenance_or_section_auth_split",
        coverage="China Sciencepaper Online historical open papers",
        stable=False,
        degraded_reason=(
            "official and library pages describe open-access paper sections, but current www.paper.edu.cn pages return "
            "a maintenance-upgrade notice and highlights.paper.edu.cn returns 502; no live article PDF extraction path is confirmed"
        ),
        failure_category="site_maintenance_fulltext_path_unavailable",
    )

    def search(self, request: AcademicSearchRequest):
        return []
