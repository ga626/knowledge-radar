"""Bilibili platform adapter."""

from __future__ import annotations

from typing import Optional

from kr_core.models import PlatformCapability, SearchRequest, SearchResponse

from .common import LegacySearchAdapterMixin, LegacySearchCallable


class BilibiliAdapter(LegacySearchAdapterMixin):
    platform = "B站"
    capabilities = PlatformCapability(
        platform=platform,
        search=True,
        detail=True,
        comments=True,
        media_extract=True,
        login_required=False,
        strategies=["http_api", "wbi_signature"],
        notes="Video search and detail extraction are optimized for tutorials and demos.",
    )

    def __init__(self, search_func: Optional[LegacySearchCallable] = None) -> None:
        self._search_func = search_func

    def search(self, request: SearchRequest) -> SearchResponse:
        return self._call_legacy(request)
