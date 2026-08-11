from search_providers.models import WebSearchRequest
from runtime.degradation import DegradationPolicy
from search_providers.quota import SearchQuotaLedger
from search_providers.service import _stale_config_breaker_can_probe
from search_providers.service import provider_status
from search_providers.service import search_web
from search_providers.providers import BaseSearchProvider, BraveSearchProvider, ExaSearchProvider


class _AvailableProvider(BaseSearchProvider):
    name = "available"

    def __init__(self, name: str | None = None):
        if name:
            self.name = name

    def status(self):
        return {"configured": True, "available": True}

    def available(self):
        return True

    def search(self, request):
        from search_providers.models import SearchProviderResult

        return [
            SearchProviderResult(
                title=f"{self.name} result",
                url=f"https://example.com/{self.name}",
                snippet="ok",
                source_provider=self.name,
            )
        ]


class _UnavailableProvider(BaseSearchProvider):
    name = "unavailable"

    def status(self):
        return {"configured": False, "available": False}

    def available(self):
        return False


class _EmptyOptionalProvider(BaseSearchProvider):
    name = "searxng"

    def status(self):
        return {"configured": True, "available": True, "degraded_ok": True}

    def available(self):
        return True

    def search(self, request):
        return []


def test_web_search_default_order_includes_brave_and_exa(monkeypatch):
    monkeypatch.setenv("KR_WEB_SEARCH_PROVIDERS", "")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.setenv("ANYSEARCH_SEARCH_ENDPOINT", "http://127.0.0.1:1/unreachable")
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://127.0.0.1:1")

    response = search_web(WebSearchRequest(query="provider order test", limit=1, provider="auto"))

    assert response.provider == "none"
    assert response.error is not None
    attempted = [row["provider"] for row in response.error["details"]]
    assert "anysearch" in attempted
    assert "tavily" not in attempted


def test_generic_provider_status_excludes_platform_specific_github(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("ANYSEARCH_SEARCH_ENDPOINT", raising=False)
    monkeypatch.delenv("SEARXNG_BASE_URL", raising=False)

    status = provider_status()

    assert "github" not in status
    assert "_quota" in status
    assert "_host_search_cards" in status


def test_github_provider_alias_is_deprecated_in_generic_search(monkeypatch, tmp_path):
    policy = DegradationPolicy(db_path=str(tmp_path / "degradation.sqlite3"), event_path=str(tmp_path / "events.jsonl"))
    monkeypatch.setattr("search_providers.service.get_degradation_policy", lambda: policy)

    response = search_web(WebSearchRequest(query="knowledge radar", limit=1, provider="github"))

    assert response.provider == "none"
    assert response.metadata["preferred_tool"] == "search_github_repositories"
    assert response.error["details"][0]["type"] == "deprecated_provider_alias"


def test_missing_brave_and_exa_are_expected_degraded(monkeypatch, tmp_path):
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    policy = DegradationPolicy(db_path=str(tmp_path / "degradation.sqlite3"), event_path=str(tmp_path / "events.jsonl"))
    monkeypatch.setattr("search_providers.service.get_degradation_policy", lambda: policy)

    brave = search_web(WebSearchRequest(query="knowledge radar", limit=1, provider="brave"))
    exa = search_web(WebSearchRequest(query="knowledge radar", limit=1, provider="exa"))

    assert BraveSearchProvider().status()["degraded_ok"] is True
    assert ExaSearchProvider().status()["degraded_ok"] is True
    assert brave.error["expected_degraded"] is True
    assert brave.error["details"][0]["expected_degraded"] is True
    assert exa.error["expected_degraded"] is True
    assert exa.error["details"][0]["expected_degraded"] is True


def test_optional_empty_results_are_expected_degraded_not_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("KR_WEB_SEARCH_PROVIDERS", "searxng")
    policy = DegradationPolicy(db_path=str(tmp_path / "degradation.sqlite3"), event_path=str(tmp_path / "events.jsonl"))
    monkeypatch.setattr("search_providers.service.get_degradation_policy", lambda: policy)
    monkeypatch.setattr("search_providers.service._providers", lambda: {"searxng": _EmptyOptionalProvider()})
    monkeypatch.setattr("search_providers.service.provider_status", lambda: {"searxng": {"configured": True, "available": True}})

    response = search_web(WebSearchRequest(query="query with no backend hits", limit=1, provider="auto"))

    assert response.provider == "none"
    assert response.error["expected_degraded"] is True
    assert response.error["details"][0]["type"] == "empty_results"
    assert response.error["details"][0]["expected_degraded"] is True


def test_web_search_auto_runs_free_wave_before_tavily(monkeypatch, tmp_path):
    monkeypatch.setenv("KR_WEB_SEARCH_PROVIDERS", "")
    policy = DegradationPolicy(db_path=str(tmp_path / "degradation.sqlite3"), event_path=str(tmp_path / "events.jsonl"))
    monkeypatch.setattr("search_providers.service.get_degradation_policy", lambda: policy)
    monkeypatch.setattr(
        "search_providers.service._providers",
        lambda: {"anysearch": _AvailableProvider("anysearch"), "tavily": _AvailableProvider("tavily")},
    )
    monkeypatch.setattr(
        "search_providers.service.provider_status",
        lambda: {
            "anysearch": {"configured": True, "available": True},
            "tavily": {"configured": True, "available": True},
        },
    )

    response = search_web(WebSearchRequest(query="provider order test", limit=1, provider="auto"))

    assert response.provider == "multi"
    assert response.attempted_providers == ["anysearch"]
    assert response.metadata["tavily_supplement_used"] is False


def test_web_search_uses_tavily_as_supplement_when_coverage_is_insufficient(monkeypatch, tmp_path):
    class _Any(_AvailableProvider):
        name = "anysearch"

    class _Tavily(_AvailableProvider):
        name = "tavily"

    monkeypatch.setenv("KR_WEB_SEARCH_PROVIDERS", "")
    policy = DegradationPolicy(db_path=str(tmp_path / "degradation.sqlite3"), event_path=str(tmp_path / "events.jsonl"))
    monkeypatch.setattr("search_providers.service.get_degradation_policy", lambda: policy)
    monkeypatch.setattr("search_providers.planner.SearchQuotaLedger", lambda: SearchQuotaLedger(tmp_path / "quota.json"))
    monkeypatch.setattr("search_providers.service.SearchQuotaLedger", lambda: SearchQuotaLedger(tmp_path / "quota.json"))
    monkeypatch.setattr("search_providers.service._providers", lambda: {"anysearch": _Any(), "tavily": _Tavily()})
    monkeypatch.setattr(
        "search_providers.service.provider_status",
        lambda: {
            "anysearch": {"configured": True, "available": True},
            "tavily": {"configured": True, "available": True},
        },
    )

    response = search_web(WebSearchRequest(query="provider order test", limit=5, provider="auto", options={"min_results": 2}))

    assert response.provider == "multi"
    assert response.attempted_providers == ["anysearch", "tavily"]
    assert response.metadata["tavily_supplement_used"] is True
    assert response.metadata["wave_concurrency"][0]["selected_workers"] == 1
    assert response.metadata["wave_concurrency"][1]["wave"] == ["tavily"]


def test_web_search_allowlist_can_exclude_tavily_supplement(monkeypatch, tmp_path):
    monkeypatch.setenv("KR_WEB_SEARCH_PROVIDERS", "anysearch")
    policy = DegradationPolicy(db_path=str(tmp_path / "degradation.sqlite3"), event_path=str(tmp_path / "events.jsonl"))
    monkeypatch.setattr("search_providers.service.get_degradation_policy", lambda: policy)
    monkeypatch.setattr(
        "search_providers.service._providers",
        lambda: {"anysearch": _AvailableProvider("anysearch"), "tavily": _AvailableProvider("tavily")},
    )
    monkeypatch.setattr(
        "search_providers.service.provider_status",
        lambda: {
            "anysearch": {"configured": True, "available": True},
            "tavily": {"configured": True, "available": True},
        },
    )

    response = search_web(WebSearchRequest(query="provider order test", limit=5, provider="auto", options={"min_results": 2}))

    assert response.attempted_providers == ["anysearch"]
    assert response.metadata["tavily_supplement_used"] is False


def test_web_search_wave_concurrency_is_adaptive(monkeypatch, tmp_path):
    monkeypatch.setenv("KR_WEB_SEARCH_PROVIDERS", "")
    monkeypatch.setenv("KR_WEB_SEARCH_MAX_WORKERS", "1")
    policy = DegradationPolicy(db_path=str(tmp_path / "degradation.sqlite3"), event_path=str(tmp_path / "events.jsonl"))
    monkeypatch.setattr("search_providers.service.get_degradation_policy", lambda: policy)
    monkeypatch.setattr(
        "search_providers.service._providers",
        lambda: {"anysearch": _AvailableProvider("anysearch"), "searxng": _AvailableProvider("searxng")},
    )
    monkeypatch.setattr(
        "search_providers.service.provider_status",
        lambda: {
            "anysearch": {"configured": True, "available": True},
            "searxng": {"configured": True, "available": True},
        },
    )

    response = search_web(WebSearchRequest(query="provider order test", limit=2, provider="auto"))

    assert response.metadata["wave_concurrency"][0]["wave"] == ["searxng", "anysearch"]
    assert response.metadata["wave_concurrency"][0]["selected_workers"] == 1
    assert "hard_cap_applied" in response.metadata["wave_concurrency"][0]["reasons"]


def test_stale_not_configured_breaker_allows_reprobe_when_provider_now_available():
    breaker = {"open": True, "last_reason": "TAVILY_API_KEY is not configured"}

    assert _stale_config_breaker_can_probe(_AvailableProvider(), breaker) is True
    assert _stale_config_breaker_can_probe(_UnavailableProvider(), breaker) is False
    assert _stale_config_breaker_can_probe(_AvailableProvider(), {"open": True, "last_reason": "connection refused"}) is False
