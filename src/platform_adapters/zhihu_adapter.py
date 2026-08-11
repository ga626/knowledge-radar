"""Zhihu platform adapter."""

from __future__ import annotations

from typing import Optional

from kr_core.models import PlatformCapability, SearchRequest, SearchResponse

from .common import LegacySearchAdapterMixin, LegacySearchCallable


class ZhihuAdapter(LegacySearchAdapterMixin):
    platform = "知乎"
    capabilities = PlatformCapability(
        platform=platform,
        search=True,
        detail=True,
        comments=True,
        media_extract=False,
        login_required=True,
        strategies=["signed_api", "chrome_cdp_page_fallback", "persistent_profile"],
        notes="Uses port 12734 and zhihu_user_data_dir for authenticated search.",
    )

    def __init__(self, search_func: Optional[LegacySearchCallable] = None) -> None:
        self._search_func = search_func

    def search(self, request: SearchRequest) -> SearchResponse:
        return self._call_legacy(request)
