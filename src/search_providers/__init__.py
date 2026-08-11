"""Generic web search provider layer for KnowledgeRadar."""

from .models import SearchProviderResult, WebSearchRequest, WebSearchResponse
from .profile import provider_profiles
from .quota import quota_summary
from .service import provider_status, search_web

__all__ = [
    "SearchProviderResult",
    "WebSearchRequest",
    "WebSearchResponse",
    "provider_status",
    "provider_profiles",
    "quota_summary",
    "search_web",
]
