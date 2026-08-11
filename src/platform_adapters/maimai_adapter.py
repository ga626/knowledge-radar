"""脉脉平台适配器。"""

from __future__ import annotations

from typing import Optional

from kr_core.models import PlatformCapability, SearchRequest, SearchResponse

from .common import LegacySearchAdapterMixin, LegacySearchCallable


class MaimaiAdapter(LegacySearchAdapterMixin):
    platform = "脉脉"
    capabilities = PlatformCapability(
        platform=platform,
        search=True,
        detail=False,
        comments=False,
        media_extract=False,
        login_required=False,
        strategies=["web_search_fallback"],
        notes="Maimai web job search page is retired; use open-web search coverage instead of Chrome CDP.",
    )

    def __init__(self, search_func: Optional[LegacySearchCallable] = None) -> None:
        self._search_func = search_func

    def search(self, request: SearchRequest) -> SearchResponse:
        return self._call_legacy(request)
