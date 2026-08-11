import json
from pathlib import Path

from academic_providers.models import AcademicSearchRequest, AcademicWork
from academic_providers.fulltext import extract_academic_fulltext, provider_supports_direct_read
from academic_providers.planner import analyze_academic_query, plan_academic_search
from academic_providers.profile import PROFILE_SCHEMA_VERSION, load_academic_provider_profiles
from academic_providers.relevance import score_metadata_relevance, score_metadata_relevance_with_profiles, select_fulltext_candidates
from academic_providers.registry import academic_provider_registry, instantiate_academic_providers
from academic_providers.service import _CACHE, _provider_order, _providers, academic_provider_status, search_academic_metadata


def test_builtin_academic_provider_profiles_are_valid_and_complete() -> None:
    profiles = load_academic_provider_profiles()
    providers = _providers()

    assert set(profiles).issubset(set(providers))
    assert "coaj" not in profiles
    assert "baidu_scholar" not in profiles
    assert "serpapi_scholar" not in profiles
    assert {"coaj", "baidu_scholar", "serpapi_scholar"}.issubset(set(providers))
    assert profiles["pubscholar"].content.direct_read_preferred is True
    assert profiles["vip_oa"].access.login_required is True
    assert profiles["openalex"].runtime.speed == "fast"
    assert profiles["europepmc"].content.pdf_fulltext is True
    assert "biomed" in profiles["europepmc"].disciplines
    assert profiles["core"].quota.default_daily == 50
    assert profiles["unpaywall"].role == "doi_oa_fulltext_lookup"
    assert profiles["ar5iv"].role == "arxiv_html_fulltext"


def test_academic_registry_instantiates_same_provider_ids_as_compat_layer() -> None:
    registry = academic_provider_registry()
    providers = instantiate_academic_providers()

    assert set(registry) == set(providers)
    assert set(registry).issubset(set(_providers()))
    assert registry["openalex"].provider.__class__.__name__ == "OpenAlexProvider"
    assert registry["vip_oa"].profile.role == "default_chinese_fulltext"


def test_academic_provider_status_exposes_capability_profile() -> None:
    status = academic_provider_status()

    profile = status["pubscholar"]["capability_profile"]
    assert profile["id"] == "pubscholar"
    assert profile["content"]["html_fulltext"] is True
    assert profile["content"]["pdf_fulltext"] is True
    assert profile["access"]["login_required"] is False
    assert profile["runtime"]["priority"]["fulltext"] >= 80


def test_academic_strategy_samples_cover_profiled_providers() -> None:
    path = Path("tests/fixtures/academic_strategy_samples.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    profiles = load_academic_provider_profiles()

    assert data["schema_version"] == "knowledgeradar-academic-strategy-samples/v1"
    assert len(data["samples"]) >= 5
    for sample in data["samples"]:
        for provider_id in sample["expected_wave_a"] + sample["expected_fulltext_candidates"]:
            assert provider_id in profiles


def test_profile_layer_keeps_current_auto_order_compatible(monkeypatch) -> None:
    monkeypatch.delenv("BAIDU_QIANFAN_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)

    assert _provider_order(AcademicSearchRequest(query="人工智能 教育", provider="auto"), "auto")[:8] == [
        "nssd",
        "chinaxiv",
        "hanspub",
        "oajrc",
        "sciopen",
        "pubscholar",
        "sciengine",
        "vip_oa",
    ]
    assert _provider_order(AcademicSearchRequest(query="LLM retrieval augmented generation", provider="auto"), "auto")[:3] == [
        "openalex",
        "crossref",
        "semanticscholar",
    ]


def test_profile_driven_planner_reports_query_intent_and_waves(monkeypatch) -> None:
    monkeypatch.delenv("BAIDU_QIANFAN_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    profiles = load_academic_provider_profiles()

    request = AcademicSearchRequest(query="人工智能 教育", provider="auto")
    plan = plan_academic_search(request, "auto", profiles)

    assert plan.intent.chinese_like is True
    assert plan.intent.language == "zh"
    assert plan.provider_order[:3] == ["nssd", "chinaxiv", "hanspub"]
    assert "pubscholar" in plan.waves["A"]
    assert "vip_oa" in plan.waves["B"]


def test_academic_query_intent_detects_citation_import_and_arxiv() -> None:
    citation = analyze_academic_query(AcademicSearchRequest(query="TY  - JOUR\nTI  - Test\nER  -\n", provider="auto"))
    arxiv = analyze_academic_query(AcademicSearchRequest(query="LLM RAG preprint", provider="auto"))
    doi = analyze_academic_query(AcademicSearchRequest(query="doi:10.3390/ai6010017", provider="auto"))
    biomed = analyze_academic_query(AcademicSearchRequest(query="clinical decision support 医疗 知识图谱", provider="auto"))
    social = analyze_academic_query(AcademicSearchRequest(query="人工智能 教育治理 社会科学", provider="auto"))

    assert citation.citation_import is True
    assert arxiv.arxiv_like is True
    assert doi.doi_like is True
    assert arxiv.chinese_like is False
    assert "biomed" in biomed.disciplines
    assert "cs" in biomed.disciplines
    assert "social_science" in social.disciplines


def test_doi_like_auto_route_uses_unpaywall_without_affecting_keyword_route(monkeypatch) -> None:
    monkeypatch.delenv("BAIDU_QIANFAN_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)

    keyword_order = _provider_order(AcademicSearchRequest(query="retrieval augmented generation", provider="auto"), "auto")
    doi_order = _provider_order(AcademicSearchRequest(query="doi:10.3390/ai6010017", provider="auto"), "auto")

    assert "unpaywall" not in keyword_order
    assert doi_order[0] == "unpaywall"


def test_metadata_relevance_selects_top_k_fulltext_candidates() -> None:
    relevant = AcademicWork(
        title="Retrieval augmented generation evaluation",
        url="https://example.org/relevant",
        abstract="This paper evaluates retrieval augmented generation systems for answer quality.",
        year=2025,
        doi="10.1/relevant",
        provider_confidence=0.8,
        full_text_status="pdf_text_extractable",
    )
    unrelated = AcademicWork(
        title="Ocean forecasting",
        url="https://example.org/other",
        abstract="A paper about ocean phenomena.",
        year=2026,
        provider_confidence=0.9,
    )

    assert score_metadata_relevance("retrieval augmented generation", relevant) > score_metadata_relevance(
        "retrieval augmented generation", unrelated
    )
    assert select_fulltext_candidates("retrieval augmented generation", [unrelated, relevant], top_k=1) == [relevant]


def test_search_main_flow_ranks_results_by_metadata_relevance(monkeypatch) -> None:
    _CACHE.clear()

    class FakeProvider:
        def search(self, request):
            assert request.query == "retrieval augmented generation"
            return [
                AcademicWork(
                    title="Ocean forecasting",
                    url="https://example.org/ocean",
                    abstract="A paper about ocean models.",
                    year=2026,
                    provider_confidence=0.95,
                ),
                AcademicWork(
                    title="Retrieval augmented generation evaluation",
                    url="https://example.org/rag",
                    abstract="This paper evaluates retrieval augmented generation for answer quality.",
                    year=2024,
                    provider_confidence=0.7,
                ),
            ]

        def status(self):
            return {"status": "available", "available": True}

    monkeypatch.setattr("academic_providers.service._providers", lambda: {"openalex": FakeProvider()})
    monkeypatch.setattr("academic_providers.service.academic_provider_status", lambda: {"openalex": {"status": "available"}})

    result = search_academic_metadata(AcademicSearchRequest(query="retrieval augmented generation", provider="openalex", limit=2))

    assert [item.url for item in result.items] == ["https://example.org/rag", "https://example.org/ocean"]
    assert result.metadata["relevance_ranking"]["applied"] is True
    assert result.metadata["relevance_ranking"]["top_score"] > 0


def test_search_main_flow_resolves_top_direct_read_fulltext_candidate(monkeypatch) -> None:
    _CACHE.clear()

    class FakeProvider:
        def search(self, request):
            return [
                AcademicWork(
                    title="Retrieval augmented generation evaluation",
                    url="https://pubscholar.cn/articles/test",
                    abstract="This paper evaluates retrieval augmented generation systems.",
                    year=2025,
                    source_database="pubscholar",
                    provider_confidence=0.9,
                    full_text_status="open_access_article_detail",
                )
            ]

        def status(self):
            return {"status": "available", "available": True}

    class FakeFullTextProvider:
        def verify_article_fulltext(self, url: str):
            assert url == "https://pubscholar.cn/articles/test"
            return {
                "status": "PASS",
                "file_url": "https://pubscholar.cn/files/test.pdf",
                "pdf_bytes_confirmed": True,
                "text_extractable": True,
                "text_probe": {"extractable": True, "text_length": 2048, "page_count": 8, "sample": "RAG full text sample"},
            }

    monkeypatch.setattr("academic_providers.service._providers", lambda: {"pubscholar": FakeProvider()})
    monkeypatch.setattr("academic_providers.service.academic_provider_status", lambda: {"pubscholar": {"status": "available"}})
    monkeypatch.setattr("academic_providers.fulltext.instantiate_academic_providers", lambda profiles=None: {"pubscholar": FakeFullTextProvider()})

    result = search_academic_metadata(AcademicSearchRequest(query="retrieval augmented generation", provider="pubscholar", limit=1))

    assert result.items[0].full_text_status == "direct_read_text_extractable"
    assert result.items[0].verification_status == "fulltext_verified"
    assert result.items[0].raw["fulltext_resolution"]["status"] == "PASS"
    assert result.items[0].raw["fulltext_text_length"] == 2048
    assert result.metadata["fulltext_resolution"]["applied"] is True
    assert result.metadata["fulltext_resolution"]["resolved_count"] == 1


def test_search_main_flow_can_disable_direct_read_fulltext_resolution(monkeypatch) -> None:
    _CACHE.clear()

    class FakeProvider:
        def search(self, request):
            return [
                AcademicWork(
                    title="Retrieval augmented generation evaluation",
                    url="https://pubscholar.cn/articles/test",
                    abstract="This paper evaluates retrieval augmented generation systems.",
                    source_database="pubscholar",
                    provider_confidence=0.9,
                    full_text_status="open_access_article_detail",
                )
            ]

        def status(self):
            return {"status": "available", "available": True}

    def fail_if_called(*args, **kwargs):
        raise AssertionError("direct-read fulltext extraction should be disabled")

    monkeypatch.setattr("academic_providers.service._providers", lambda: {"pubscholar": FakeProvider()})
    monkeypatch.setattr("academic_providers.service.academic_provider_status", lambda: {"pubscholar": {"status": "available"}})
    monkeypatch.setattr("academic_providers.service.extract_academic_fulltext", fail_if_called)

    result = search_academic_metadata(
        AcademicSearchRequest(
            query="retrieval augmented generation",
            provider="pubscholar",
            limit=1,
            options={"resolve_fulltext": False},
        )
    )

    assert result.items[0].full_text_status == "open_access_article_detail"
    assert result.metadata["fulltext_resolution"]["applied"] is False
    assert result.metadata["fulltext_resolution"]["reason"] == "disabled_by_request"


def test_overlap_supplement_profile_receives_small_relevance_penalty() -> None:
    profiles = load_academic_provider_profiles()
    primary = AcademicWork(
        title="Gesture recognition smart home disabilities",
        url="https://example.org/primary",
        abstract="Gesture recognition smart home disabilities",
        source_database="openalex",
        provider_confidence=0.8,
    )
    supplement = AcademicWork(
        title="Gesture recognition smart home disabilities",
        url="https://example.org/socolar",
        abstract="Gesture recognition smart home disabilities",
        source_database="socolar",
        provider_confidence=0.8,
    )

    assert score_metadata_relevance_with_profiles("gesture recognition smart home disabilities", primary, profiles) > score_metadata_relevance_with_profiles(
        "gesture recognition smart home disabilities", supplement, profiles
    )


def test_direct_read_support_is_profile_driven() -> None:
    profiles = load_academic_provider_profiles()

    assert provider_supports_direct_read("pubscholar", profiles) is True
    assert provider_supports_direct_read("sciengine", profiles) is True
    assert provider_supports_direct_read("vip_oa", profiles) is True
    assert provider_supports_direct_read("ar5iv", profiles) is True
    assert provider_supports_direct_read("openalex", profiles) is False
    assert provider_supports_direct_read("socolar", profiles) is False


def test_extract_academic_fulltext_normalizes_provider_status(monkeypatch) -> None:
    class FakeProvider:
        def verify_article_fulltext(self, url: str):
            return {
                "status": "PASS",
                "article_url": url,
                "file_url": "https://pubscholar.cn/files?fastdfspath=test.pdf",
                "pdf_bytes_confirmed": True,
                "text_extractable": True,
                "text_probe": {
                    "extractable": True,
                    "text_length": 1234,
                    "page_count": 6,
                    "sample": "sample text",
                },
            }

    monkeypatch.setattr("academic_providers.fulltext.instantiate_academic_providers", lambda profiles=None: {"pubscholar": FakeProvider()})

    result = extract_academic_fulltext("pubscholar", "https://pubscholar.cn/articles/test", timeout_s=1)

    assert result.status == "PASS"
    assert result.mode == "pdf_viewer_text"
    assert result.text_extractable is True
    assert result.text_length == 1234
    assert result.page_count == 6
    assert result.sample == "sample text"
    assert result.provenance["disk_persisted"] is False
    assert result.provenance["download_button_used"] is False


def test_extract_academic_fulltext_reports_unsupported_provider_without_network() -> None:
    result = extract_academic_fulltext("openalex", "https://openalex.org/W1")

    assert result.status == "EXPECTED_DEGRADED"
    assert result.text_extractable is False
    assert result.degraded_reason == "unsupported_direct_read_provider"


def test_profile_file_schema_version_is_explicit() -> None:
    profiles = load_academic_provider_profiles()

    assert PROFILE_SCHEMA_VERSION == "knowledgeradar-academic-provider-profiles/v1"
    assert all(profile.enabled for profile in profiles.values())
