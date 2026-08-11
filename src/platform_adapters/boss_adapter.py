"""BOSS直聘平台适配器 - 隔离试验方案。"""

from __future__ import annotations

from typing import Optional

from kr_core.models import PlatformCapability, SearchRequest, SearchResponse

from .common import LegacySearchAdapterMixin, LegacySearchCallable


class BossAdapter(LegacySearchAdapterMixin):
    platform = "BOSS直聘"
    capabilities = PlatformCapability(
        platform=platform,
        search=True,
        detail=True,
        comments=False,
        media_extract=False,
        login_required=True,
        strategies=["stealth_cdp_page", "persistent_profile"],
        notes="Uses port 12737 with stealth.js anti-debug injection for search and job detail extraction.",
    )

    def __init__(self, search_func: Optional[LegacySearchCallable] = None) -> None:
        self._search_func = search_func

    def search(self, request: SearchRequest) -> SearchResponse:
        return self._call_legacy(request)
