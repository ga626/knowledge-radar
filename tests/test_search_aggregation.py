from search_providers.aggregation import aggregate_results, canonical_url, coverage_decision
from search_providers.models import SearchProviderResult, WebSearchRequest


def test_canonical_url_strips_tracking_query():
    assert canonical_url("HTTPS://Example.COM/a/?utm_source=x&b=1#frag") == "https://example.com/a?b=1"


def test_aggregate_results_deduplicates_with_provenance():
    item_a = SearchProviderResult(title="A", url="https://example.com/a?utm_source=x", source_provider="searxng")
    item_b = SearchProviderResult(title="A copy", url="https://example.com/a", source_provider="anysearch")

    rows = aggregate_results({"searxng": [item_a], "anysearch": [item_b]}, limit=10)

    assert len(rows) == 1
    assert rows[0].raw["provider_provenance"] == ["searxng", "anysearch"]


def test_coverage_decision_triggers_when_unique_results_are_low():
    decision = coverage_decision(
        WebSearchRequest(query="ai", limit=5, options={"min_results": 3}),
        [SearchProviderResult(title="A", url="https://example.com/a")],
        ["searxng"],
    )

    assert decision.sufficient is False
    assert "unique_results_below_threshold" in decision.triggers
