"""Adapter protocol shared by all platform integrations."""

from __future__ import annotations

from typing import Protocol

from .models import DetailRequest, DetailResponse, PlatformCapability, SearchRequest, SearchResponse


class PlatformAdapter(Protocol):
    platform: str
    capabilities: PlatformCapability

    def search(self, request: SearchRequest) -> SearchResponse:
        """Search one platform and return normalized results."""
        ...


class DetailStrategy(Protocol):
    platform: str

    def extract(self, request: DetailRequest) -> DetailResponse:
        """Extract one platform detail URL and return a normalized response."""
        ...
