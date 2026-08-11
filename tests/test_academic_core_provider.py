import json

import httpx
import pytest

from academic_providers.core import CoreAuthError, CoreProvider, CoreRateLimitError
from academic_providers.models import AcademicSearchRequest
from academic_providers.service import _CACHE, _provider_order, search_academic_metadata


class _FixedDate:
    @classmethod
    def now(cls):
        return cls()

    def strftime(self, fmt):
        assert fmt == "%Y-%m-%d"
        return "2099-01-01"


def test_core_requires_api_key(monkeypatch) -> None:
    monkeypatch.delenv("KR_CORE_API_KEY", raising=False)

    with pytest.raises(CoreAuthError):
        CoreProvider().search(AcademicSearchRequest(query="machine learning", provider="core"))


def test_core_reports_local_daily_quota(monkeypatch, tmp_path) -> None:
    usage_path = tmp_path / "core-usage.json"
    usage_path.write_text(json.dumps({"2099-01-01": 2}), encoding="utf-8")
    monkeypatch.setenv("KR_CORE_API_KEY", "dummy")
    monkeypatch.setenv("KR_ACADEMIC_CORE_DAILY_LIMIT", "2")
    monkeypatch.setenv("KR_ACADEMIC_CORE_USAGE_PATH", str(usage_path))
    monkeypatch.setattr("academic_providers.quota.datetime", _FixedDate)

    provider = CoreProvider()
    status = provider.status()

    assert status["configured"] is True
    assert status["available"] is False
    assert status["daily_exhausted"] is True
    with pytest.raises(CoreRateLimitError):
        provider.search(AcademicSearchRequest(query="machine learning", provider="core"))


def test_core_maps_results_and_consumes_quota(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KR_CORE_API_KEY", "dummy")
    monkeypatch.setenv("KR_ACADEMIC_CORE_DAILY_LIMIT", "5")
    monkeypatch.setenv("KR_ACADEMIC_CORE_USAGE_PATH", str(tmp_path / "core-usage.json"))
    monkeypatch.setattr("academic_providers.quota.datetime", _FixedDate)

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            headers={
                "X-RateLimit-Limit": "10",
                "X-RateLimit-Remaining": "9",
                "X-RateLimit-Retry-After": "2099-01-01T00:00:00+0000",
            },
            json={
                "totalHits": 1,
                "results": [
                    {
                        "id": 123,
                        "title": "Evidence-Based Detection of Pancreatic Cancer",
                        "abstract": "A study of pancreatic cancer detection.",
                        "authors": [{"name": "Ada Lovelace"}],
                        "yearPublished": 2025,
                        "doi": "10.1234/core.test",
                        "publisher": "Example Repository",
                        "downloadUrl": "https://example.org/paper.pdf",
                        "sourceFulltextUrls": ["https://example.org/fulltext"],
                        "fullText": "Full text preview.",
                        "links": [
                            {"type": "reader", "url": "https://core.ac.uk/reader/123"},
                            {"type": "display", "url": "https://core.ac.uk/works/123"},
                        ],
                    }
                ],
            },
        )

    real_client = httpx.Client
    transport = httpx.MockTransport(handler)

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            self.kwargs = kwargs

        def __enter__(self):
            self.client = real_client(transport=transport, follow_redirects=self.kwargs.get("follow_redirects", False))
            return self.client

        def __exit__(self, exc_type, exc, tb):
            self.client.close()
            return False

    monkeypatch.setattr("academic_providers.core.httpx.Client", FakeClient)

    result = CoreProvider(endpoint="https://api.core.test/v3/search/works/").search(
        AcademicSearchRequest(query="pancreatic cancer", provider="core", limit=1)
    )

    assert "q=pancreatic+cancer" in captured["url"]
    assert result[0].title == "Evidence-Based Detection of Pancreatic Cancer"
    assert result[0].doi == "10.1234/core.test"
    assert result[0].url == "https://core.ac.uk/works/123"
    assert result[0].full_text_status == "parsed_fulltext_available"
    assert result[0].raw["rate_limit"]["X-RateLimit-Remaining"] == "9"
    assert json.loads((tmp_path / "core-usage.json").read_text(encoding="utf-8")) == {"2099-01-01": 1}


def test_core_explicit_call_is_classified_as_rate_limited_when_local_quota_exhausted(monkeypatch, tmp_path) -> None:
    _CACHE.clear()
    usage_path = tmp_path / "core-usage.json"
    usage_path.write_text(json.dumps({"2099-01-01": 1}), encoding="utf-8")
    monkeypatch.setenv("KR_CORE_API_KEY", "dummy")
    monkeypatch.setenv("KR_ACADEMIC_CORE_DAILY_LIMIT", "1")
    monkeypatch.setenv("KR_ACADEMIC_CORE_USAGE_PATH", str(usage_path))
    monkeypatch.setattr("academic_providers.quota.datetime", _FixedDate)

    result = search_academic_metadata(AcademicSearchRequest(query="machine learning", provider="core", limit=1))

    assert result.error
    assert result.error["details"][0]["type"] == "rate_limited"


def test_core_auto_route_requires_configured_key_and_quota(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("KR_CORE_API_KEY", raising=False)
    monkeypatch.setenv("KR_ACADEMIC_CORE_USAGE_PATH", str(tmp_path / "core-usage.json"))

    assert "core" not in _provider_order(AcademicSearchRequest(query="machine learning", provider="auto"), "auto")

    monkeypatch.setenv("KR_CORE_API_KEY", "dummy")

    assert "core" in _provider_order(AcademicSearchRequest(query="machine learning", provider="auto"), "auto")
