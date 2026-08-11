from academic_providers.ar5iv import Ar5ivError, Ar5ivProvider
from academic_providers.models import AcademicSearchRequest, AcademicWork
from academic_providers.planner import plan_academic_search
from academic_providers.profile import load_academic_provider_profiles


class FakeArxivProvider:
    def __init__(self, items=None, error=None):
        self.items = items or []
        self.error = error

    def search(self, request):
        if self.error:
            raise self.error
        return self.items


def _arxiv_work() -> AcademicWork:
    return AcademicWork(
        title="Attention Is All You Need",
        url="https://arxiv.org/abs/1706.03762",
        authors=["A. Author"],
        year=2017,
        abstract="Transformer architecture for sequence modeling.",
        source="arXiv",
        source_database="arxiv",
        provider_confidence=0.82,
        raw={"pdf_url": "https://arxiv.org/pdf/1706.03762"},
    )


def test_ar5iv_returns_html_enhanced_arxiv_work(monkeypatch) -> None:
    monkeypatch.setattr("academic_providers.ar5iv._html_available", lambda url, timeout: True)
    provider = Ar5ivProvider(endpoint="https://ar5iv.test/html", arxiv_provider=FakeArxivProvider([_arxiv_work()]))

    results = provider.search(AcademicSearchRequest(query="transformer preprint", provider="ar5iv", limit=1))

    assert len(results) == 1
    assert results[0].source_database == "ar5iv"
    assert results[0].url == "https://ar5iv.test/html/1706.03762"
    assert results[0].full_text_status == "html_fulltext"
    assert results[0].access_mode == "public_html_enhancer"
    assert results[0].raw["source_provider"] == "arxiv"


def test_ar5iv_accepts_direct_arxiv_id_without_arxiv_api(monkeypatch) -> None:
    monkeypatch.setattr("academic_providers.ar5iv._html_available", lambda url, timeout: True)
    provider = Ar5ivProvider(endpoint="https://ar5iv.test/html", arxiv_provider=FakeArxivProvider(error=AssertionError("unused")))

    results = provider.search(AcademicSearchRequest(query="arXiv:1706.03762", provider="ar5iv", limit=1))

    assert len(results) == 1
    assert results[0].title == "arXiv:1706.03762"
    assert results[0].url == "https://ar5iv.test/html/1706.03762"
    assert results[0].verification_status == "arxiv_id_matched"


def test_ar5iv_skips_arxiv_work_when_html_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr("academic_providers.ar5iv._html_available", lambda url, timeout: False)
    provider = Ar5ivProvider(arxiv_provider=FakeArxivProvider([_arxiv_work()]))

    assert provider.search(AcademicSearchRequest(query="transformer preprint", provider="ar5iv", limit=1)) == []


def test_ar5iv_wraps_arxiv_errors() -> None:
    from academic_providers.arxiv import ArxivError

    provider = Ar5ivProvider(arxiv_provider=FakeArxivProvider(error=ArxivError("arxiv unavailable")))

    try:
        provider.search(AcademicSearchRequest(query="transformer preprint", provider="ar5iv", limit=1))
    except Ar5ivError as exc:
        assert "arxiv unavailable" in str(exc)
    else:
        raise AssertionError("expected Ar5ivError")


def test_ar5iv_is_intent_only_after_arxiv_for_stem_queries() -> None:
    profiles = load_academic_provider_profiles()

    plan = plan_academic_search(AcademicSearchRequest(query="LLM RAG preprint", provider="auto"), "auto", profiles)

    assert "arxiv" in plan.provider_order
    assert "ar5iv" in plan.provider_order
    assert plan.provider_order.index("arxiv") < plan.provider_order.index("ar5iv")
