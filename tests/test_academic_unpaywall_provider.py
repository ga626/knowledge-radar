import httpx
import pytest

from academic_providers.models import AcademicSearchRequest
from academic_providers.unpaywall import UnpaywallAuthError, UnpaywallProvider


def test_unpaywall_non_doi_query_is_noop() -> None:
    provider = UnpaywallProvider()

    assert provider.search(AcademicSearchRequest(query="retrieval augmented generation", provider="unpaywall")) == []


def test_unpaywall_requires_email_for_doi_lookup(monkeypatch) -> None:
    monkeypatch.delenv("KR_UNPAYWALL_EMAIL", raising=False)
    monkeypatch.delenv("UNPAYWALL_EMAIL", raising=False)
    provider = UnpaywallProvider()

    with pytest.raises(UnpaywallAuthError):
        provider.search(AcademicSearchRequest(query="10.3390/ai6010017", provider="unpaywall"))


def test_unpaywall_maps_best_oa_pdf_location(monkeypatch) -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "doi": "10.3390/ai6010017",
                "title": "Gesture Recognition for Smart Homes",
                "year": 2025,
                "is_oa": True,
                "oa_status": "gold",
                "journal_name": "AI",
                "z_authors": [{"given": "Ada", "family": "Lovelace"}],
                "best_oa_location": {
                    "url": "https://www.mdpi.com/2673-2688/6/1/17",
                    "url_for_pdf": "https://www.mdpi.com/2673-2688/6/1/17/pdf",
                    "license": "cc-by",
                    "host_type": "publisher",
                    "version": "publishedVersion",
                },
            },
        )

    real_client = httpx.Client
    transport = httpx.MockTransport(handler)

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, params=None):
            with real_client(transport=transport) as client:
                return client.get(url, params=params)

    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: FakeClient())
    provider = UnpaywallProvider(endpoint="https://api.unpaywall.test/v2")

    results = provider.search(
        AcademicSearchRequest(query="doi:10.3390/ai6010017", provider="unpaywall", options={"mailto": "research@example.org"})
    )

    assert len(results) == 1
    work = results[0]
    assert work.title == "Gesture Recognition for Smart Homes"
    assert work.doi == "10.3390/ai6010017"
    assert work.url.endswith("/pdf")
    assert work.full_text_status == "pdf_text_extractable"
    assert work.license_scope == "open"
    assert "email=research%40example.org" in captured["url"]
