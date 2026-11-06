"""Generic web search provider layer for KnowledgeRadar."""

from .models import SearchProviderResult, WebSearchRequest, WebSearchResponse
from .profile import provider_profiles
from .quota import quota_summary

def provider_status(*args, **kwargs):
    from .service import provider_status as implementation
    return implementation(*args, **kwargs)


def search_web(*args, **kwargs):
    from .service import search_web as implementation
    return implementation(*args, **kwargs)

__all__ = [
    "SearchProviderResult",
    "WebSearchRequest",
    "WebSearchResponse",
    "provider_status",
    "provider_profiles",
    "quota_summary",
    "search_web",
]
