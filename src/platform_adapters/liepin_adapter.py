"""猎聘平台适配器。"""

from __future__ import annotations

from typing import Optional

from kr_core.models import PlatformCapability, SearchRequest, SearchResponse

from .common import LegacySearchAdapterMixin, LegacySearchCallable


class LiepinAdapter(LegacySearchAdapterMixin):
    platform = "猎聘"
    capabilities = PlatformCapability(
        platform=platform,
        search=True,
        detail=True,
        comments=False,
        media_extract=False,
        login_required=False,
        strategies=["chrome_cdp_page", "persistent_profile"],
        notes="Uses port 12738 with persistent Chrome profile for Liepin search and public job detail pages.",
    )

    def __init__(self, search_func: Optional[LegacySearchCallable] = None) -> None:
        self._search_func = search_func

    def search(self, request: SearchRequest) -> SearchResponse:
        return self._call_legacy(request)
