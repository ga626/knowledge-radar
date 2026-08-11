"""YouTube platform adapter."""

from __future__ import annotations

from typing import Optional

from kr_core.models import PlatformCapability, SearchRequest, SearchResponse

from .common import LegacySearchAdapterMixin, LegacySearchCallable


class YouTubeAdapter(LegacySearchAdapterMixin):
    platform = "YouTube"
    capabilities = PlatformCapability(
        platform=platform,
        search=True,
        detail=True,
        comments=False,
        media_extract=True,
        login_required=False,
        strategies=["http_api", "youtube_data_api_v3", "youtube_transcript_api_fallback", "qwen_vl_pipeline"],
        notes="YouTube Data API v3 adapter using search.list/videos.list/captions.list with transcript fallback.",
    )

    def __init__(self, search_func: Optional[LegacySearchCallable] = None) -> None:
        self._search_func = search_func

    def search(self, request: SearchRequest) -> SearchResponse:
        return self._call_legacy(request)
