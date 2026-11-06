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
        comments=True,
        media_extract=True,
        login_required=False,
        strategies=["http_api", "youtube_data_api_v3", "youtube_transcript_api_fallback", "qwen_vl_pipeline"],
        notes="YouTube Data API v3 adapter using search.list/videos.list/paged commentThreads/comments and transcript fallback.",
    )

    def __init__(self, search_func: Optional[LegacySearchCallable] = None) -> None:
        self._search_func = search_func or search_from_request

    def search(self, request: SearchRequest) -> SearchResponse:
        return self._call_legacy(request)

    @staticmethod
    def enhance_detail(request, collected):
        from detail_strategies.video import enhance_video_detail
        return enhance_video_detail(request, collected)


def search_from_request(request: SearchRequest):
    from collectors.platform.youtube import search_youtube
    return search_youtube(request.keyword, limit=request.limit)
