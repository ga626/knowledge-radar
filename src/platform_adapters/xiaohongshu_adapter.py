"""Xiaohongshu platform adapter."""

from __future__ import annotations

from typing import Optional

from kr_core.models import PlatformCapability, SearchRequest, SearchResponse
from runtime.chrome_manager import XHS_CHROME_DEBUG_PORT

from .common import LegacySearchAdapterMixin, LegacySearchCallable


class XiaohongshuAdapter(LegacySearchAdapterMixin):
    platform = "小红书"
    capabilities = PlatformCapability(
        platform=platform,
        search=True,
        detail=True,
        comments=False,
        media_extract=True,
        login_required=True,
        strategies=[
            "scrapling_cdp_primary",
            "chrome_cdp_page_fallback",
            "bridge_fallback_diagnostic_only",
            "persistent_profile",
            "force_probe_diagnostic",
            "raw_cdp_diagnostic",
            "nodriver_diagnostic_candidate",
        ],
        notes=f"Uses port {XHS_CHROME_DEBUG_PORT} and xhs_user_data_dir; Scrapling remains primary. Diagnostic tools are not search fallbacks.",
    )

    def __init__(self, search_func: Optional[LegacySearchCallable] = None) -> None:
        self._search_func = search_func

    def search(self, request: SearchRequest) -> SearchResponse:
        return self._call_legacy(request)
