import json
import logging

import pytest

from academic_providers.models import AcademicSearchRequest, AcademicWork
from academic_providers.cnki_authorized_browser import _normalize_probe_result
from academic_providers.citation_import import CitationImportError, CitationImportProvider, parse_citation_text
from academic_providers.serpapi_scholar import SerpApiScholarAuthError, SerpApiScholarProvider
from academic_providers.baidu_scholar import BaiduScholarAuthError, BaiduScholarProvider
from academic_providers.service import (
    _CACHE,
    _dedupe,
    _normalize_provider_name,
    _provider_order,
    academic_provider_status,
    search_academic_metadata,
)


def test_serpapi_scholar_status_is_optional_without_key(monkeypatch) -> None:
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.delenv("KR_ACADEMIC_ENABLE_SERPAPI", raising=False)

    status = academic_provider_status()["serpapi_scholar"]

    assert status["configured"] is False
    assert status["available"] is False
    assert status["requires_api_key"] is True
    assert status["monthly_limit"] == 250


def test_academic_provider_aliases_are_normalized() -> None:
    assert _normalize_provider_name("semantic_scholar") == "semanticscholar"
    assert _normalize_provider_name("semantic-scholar") == "semanticscholar"
    assert _normalize_provider_name("google_scholar") == "serpapi_scholar"
    assert _normalize_provider_name("baidu_qianfan") == "baidu_scholar"


def test_auto_order_does_not_spend_serpapi_by_default(monkeypatch) -> None:
    monkeypatch.setenv("SERPAPI_API_KEY", "dummy")
    monkeypatch.setenv("KR_ACADEMIC_ENABLE_SERPAPI", "false")

    assert "serpapi_scholar" not in _provider_order(AcademicSearchRequest(query="knowledge graph", provider="auto"), "auto")


def test_auto_order_does_not_enable_unprofiled_serpapi_when_quota_available(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SERPAPI_API_KEY", "dummy")
    monkeypatch.setenv("KR_ACADEMIC_ENABLE_SERPAPI", "true")
    monkeypatch.setenv("KR_ACADEMIC_SERPAPI_DAILY_LIMIT", "8")
    monkeypatch.setenv("KR_ACADEMIC_SERPAPI_USAGE_PATH", str(tmp_path / "usage.json"))

    assert "serpapi_scholar" not in _provider_order(AcademicSearchRequest(query="knowledge graph", provider="auto"), "auto")


def test_auto_order_skips_serpapi_when_daily_limit_exhausted(monkeypatch, tmp_path) -> None:
    usage_path = tmp_path / "usage.json"
    usage_path.write_text(json.dumps({"2099-01-01": 8}), encoding="utf-8")
    monkeypatch.setenv("SERPAPI_API_KEY", "dummy")
    monkeypatch.setenv("KR_ACADEMIC_ENABLE_SERPAPI", "true")
    monkeypatch.setenv("KR_ACADEMIC_SERPAPI_DAILY_LIMIT", "8")
    monkeypatch.setenv("KR_ACADEMIC_SERPAPI_USAGE_PATH", str(usage_path))
    monkeypatch.setattr("academic_providers.quota.datetime", _FixedDate)

    assert "serpapi_scholar" not in _provider_order(AcademicSearchRequest(query="knowledge graph", provider="auto"), "auto")
    status = academic_provider_status()["serpapi_scholar"]
    assert status["daily_exhausted"] is True
    assert status["daily_used"] == 8


@pytest.mark.parametrize(
    ("env_var", "provider_cls", "query", "expected_error"),
    [
        ("SERPAPI_API_KEY", SerpApiScholarProvider, "知识图谱 医疗", SerpApiScholarAuthError),
        ("BAIDU_QIANFAN_BEARER_TOKEN", BaiduScholarProvider, "人工智能", BaiduScholarAuthError),
    ],
)
def test_optional_commercial_academic_providers_require_credentials(
    monkeypatch,
    env_var: str,
    provider_cls,
    query: str,
    expected_error: type[Exception],
) -> None:
    monkeypatch.delenv(env_var, raising=False)

    with pytest.raises(expected_error):
        provider_cls().search(AcademicSearchRequest(query=query))


def test_serpapi_scholar_maps_results_and_consumes_daily_quota(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SERPAPI_API_KEY", "dummy")
    monkeypatch.setenv("KR_ACADEMIC_SERPAPI_DAILY_LIMIT", "8")
    monkeypatch.setenv("KR_ACADEMIC_SERPAPI_USAGE_PATH", str(tmp_path / "usage.json"))
    monkeypatch.setattr("academic_providers.quota.datetime", _FixedDate)

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "organic_results": [
                    {
                        "position": 1,
                        "title": "医疗知识图谱综述",
                        "link": "https://example.org/paper",
                        "snippet": "中文医疗知识图谱研究综述。",
                        "publication_info": {
                            "summary": "张三, 李四 - 情报学报, 2024",
                            "authors": [{"name": "张三"}, {"name": "李四"}],
                        },
                        "inline_links": {"cited_by": {"total": 12}},
                        "result_id": "abc123",
                    }
                ]
            }

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            self.params = None

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def get(self, endpoint, params):
            self.params = params
            assert params["engine"] == "google_scholar"
            assert params["q"] == "医疗知识图谱"
            assert params["num"] == 1
            return FakeResponse()

    monkeypatch.setattr("academic_providers.serpapi_scholar.httpx.Client", FakeClient)

    result = search_academic_metadata(AcademicSearchRequest(query="医疗知识图谱", limit=1, provider="serpapi_scholar"))

    assert result.provider == "serpapi_scholar"
    assert result.items[0].title == "医疗知识图谱综述"
    assert result.items[0].authors == ["张三", "李四"]
    assert result.items[0].year == 2024
    assert result.items[0].source == "Google Scholar via SerpAPI"
    assert result.items[0].source_database == "serpapi_scholar"
    assert result.items[0].access_mode == "serp_metadata"
    assert result.items[0].full_text_status == "metadata_only"
    assert json.loads((tmp_path / "usage.json").read_text(encoding="utf-8")) == {"2099-01-01": 1}


def test_serpapi_scholar_explicit_call_returns_rate_limited_when_daily_exhausted(monkeypatch, tmp_path) -> None:
    _CACHE.clear()
    usage_path = tmp_path / "usage.json"
    usage_path.write_text(json.dumps({"2099-01-01": 8}), encoding="utf-8")
    monkeypatch.setenv("SERPAPI_API_KEY", "dummy")
    monkeypatch.setenv("KR_ACADEMIC_SERPAPI_DAILY_LIMIT", "8")
    monkeypatch.setenv("KR_ACADEMIC_SERPAPI_USAGE_PATH", str(usage_path))
    monkeypatch.setattr("academic_providers.quota.datetime", _FixedDate)

    result = search_academic_metadata(AcademicSearchRequest(query="医疗知识图谱 耗尽", limit=1, provider="serpapi_scholar"))

    assert result.error is not None
    assert result.error["type"] == "all_providers_rate_limited"
    assert result.metadata["provider_status"]["serpapi_scholar"]["daily_exhausted"] is True


def test_openalex_status_reports_api_key(monkeypatch) -> None:
    monkeypatch.setenv("OPENALEX_API_KEY", "dummy")

    status = academic_provider_status()["openalex"]

    assert status["api_key_configured"] is True
    assert status["quota"]["free_credit_per_day_usd"] == 1.0


def test_auto_order_does_not_include_unprofiled_baidu_for_chinese_query_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("BAIDU_QIANFAN_BEARER_TOKEN", "dummy")
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)

    order = _provider_order(AcademicSearchRequest(query="中文 医学 知识图谱", provider="auto"), "auto")

    assert "baidu_scholar" not in order
    assert "arxiv" not in order


def test_auto_order_includes_arxiv_for_stem_english_query(monkeypatch) -> None:
    monkeypatch.delenv("BAIDU_QIANFAN_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)

    order = _provider_order(AcademicSearchRequest(query="LLM retrieval augmented generation", provider="auto"), "auto")

    assert "arxiv" in order
    assert "ar5iv" in order
    assert order.index("arxiv") < order.index("ar5iv")
    assert "baidu_scholar" not in order


def test_baidu_scholar_maps_results(monkeypatch) -> None:
    monkeypatch.setenv("BAIDU_QIANFAN_BEARER_TOKEN", "dummy")

    class FakeResponse:
        status_code = 200
        text = ""

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "code": "0",
                "requestId": "req-1",
                "hasMore": False,
                "data": [
                    {
                        "title": "中文知识图谱研究综述",
                        "url": "https://xueshu.baidu.com/ndscholar/browse/detail?paperid=abc",
                        "doi": "10.1234/example",
                        "publishYear": 2024,
                        "abstract": "摘要",
                        "aiAbstract": "AI 摘要",
                        "paperId": "abc",
                        "keyword": "知识图谱",
                        "publishInfo": {"journalName": "情报学报"},
                    }
                ],
            }

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            self.headers = kwargs.get("headers") or {}

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def get(self, endpoint, params):
            assert "baidu_scholar/search" in endpoint
            assert params["wd"] == "中文知识图谱"
            assert "enable_abstract" in params
            assert "enable_ai_abstract" not in params
            assert self.headers["Authorization"].startswith("Bearer ")
            return FakeResponse()

    monkeypatch.setattr("academic_providers.baidu_scholar.httpx.Client", FakeClient)

    result = BaiduScholarProvider().search(AcademicSearchRequest(query="中文知识图谱", limit=1))

    assert result[0].title == "中文知识图谱研究综述"
    assert result[0].abstract == "AI 摘要"
    assert result[0].source == "情报学报"
    assert result[0].source_database == "baidu_scholar"
    assert result[0].access_mode == "official_api"
    assert result[0].full_text_status == "metadata_only"


def test_academic_work_outputs_extended_access_and_verification_fields() -> None:
    work = AcademicWork(
        title="中文知识图谱研究综述",
        url="https://example.org/paper",
        authors=["张三"],
        year=2024,
        doi="https://doi.org/10.1234/Example",
        source="情报学报",
        source_database="cnki",
        access_mode="authorized_browser",
        full_text_status="licensed_visible",
        provider_confidence=0.91,
        title_similarity=0.98,
        verification_status="cross_provider_matched",
        citation_export_formats=["ris", "bibtex"],
        license_scope="institution",
        degraded_reason="",
    )

    data = work.to_dict()

    assert data["doi"] == "10.1234/example"
    assert data["source_database"] == "cnki"
    assert data["access_mode"] == "authorized_browser"
    assert data["full_text_status"] == "licensed_visible"
    assert data["provider_confidence"] == 0.91
    assert data["title_similarity"] == 0.98
    assert data["verification_status"] == "cross_provider_matched"
    assert data["citation_export_formats"] == ["ris", "bibtex"]
    assert data["license_scope"] == "institution"


def test_academic_dedupe_uses_doi_then_title_year_first_author() -> None:
    same_title_a = AcademicWork(title="  Knowledge   Graphs in Medicine! ", url="https://a", authors=["Alice Smith"], year=2024)
    same_title_b = AcademicWork(title="Knowledge Graphs in Medicine", url="https://b", authors=["Alice Smith"], year=2024)
    different_author = AcademicWork(title="Knowledge Graphs in Medicine", url="https://c", authors=["Bob Smith"], year=2024)
    same_doi = AcademicWork(title="Different title", url="https://d", authors=["Nobody"], year=2025, doi="10.1000/ABC")
    same_doi_again = AcademicWork(title="Another title", url="https://e", authors=["Nobody"], year=2026, doi="https://doi.org/10.1000/abc")

    deduped = _dedupe([same_title_a, same_title_b, different_author, same_doi, same_doi_again])

    assert [item.url for item in deduped] == ["https://a", "https://c", "https://d"]


def test_citation_import_parses_ris_export_text() -> None:
    text = """TY  - JOUR
TI  - 中文知识图谱研究综述
AU  - 张三
AU  - 李四
PY  - 2024
JO  - 情报学报
DO  - https://doi.org/10.1234/example
AB  - 这是一段摘要。
ER  -
"""

    works = parse_citation_text(text, import_source="cnki-export.ris")

    assert len(works) == 1
    assert works[0].title == "中文知识图谱研究综述"
    assert works[0].authors == ["张三", "李四"]
    assert works[0].year == 2024
    assert works[0].doi == "10.1234/example"
    assert works[0].source_database == "cnki"
    assert works[0].access_mode == "user_import"
    assert works[0].verification_status == "user_supplied"
    assert works[0].citation_export_formats == ["ris"]


def test_citation_import_parses_bibtex_export_text() -> None:
    text = """@article{kg2024,
  title = {Knowledge Graphs in Medicine},
  author = {Alice Smith and Bob Lee},
  year = {2024},
  journal = {Journal of Medical Informatics},
  doi = {10.5678/kg.2024},
  url = {https://example.org/kg}
}"""

    works = parse_citation_text(text)

    assert works[0].title == "Knowledge Graphs in Medicine"
    assert works[0].authors == ["Alice Smith", "Bob Lee"]
    assert works[0].source == "Journal of Medical Informatics"
    assert works[0].citation_export_formats == ["bibtex"]


def test_citation_import_parses_endnote_export_text() -> None:
    text = """%0 Journal Article
%T 医学人工智能综述
%A 王五
%D 2023
%J 中国数字医学
%R 10.9999/test
"""

    works = parse_citation_text(text, import_source="wanfang.enw")

    assert works[0].title == "医学人工智能综述"
    assert works[0].authors == ["王五"]
    assert works[0].year == 2023
    assert works[0].source_database == "wanfang"
    assert works[0].citation_export_formats == ["endnote"]


def test_citation_import_provider_reads_local_file(tmp_path) -> None:
    path = tmp_path / "cnki-export.ris"
    path.write_text("TY  - JOUR\nTI  - 本地导入论文\nPY  - 2025\nER  -\n", encoding="utf-8")

    works = CitationImportProvider().search(AcademicSearchRequest(query=f"file:{path}", provider="citation_import"))

    assert works[0].title == "本地导入论文"
    assert works[0].source_database == "cnki"


def test_citation_import_provider_rejects_unsupported_file(tmp_path) -> None:
    path = tmp_path / "paper.pdf"
    path.write_text("not a citation export", encoding="utf-8")

    with pytest.raises(CitationImportError):
        CitationImportProvider().search(AcademicSearchRequest(query=f"file:{path}", provider="citation_import"))


def test_cnki_authorized_browser_status_is_explicit_and_not_auto(monkeypatch) -> None:
    monkeypatch.delenv("BAIDU_QIANFAN_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)

    status = academic_provider_status()["cnki_authorized_browser"]
    order = _provider_order(AcademicSearchRequest(query="中文 学术 检索", provider="auto"), "auto")

    assert status["configured"] is True
    assert status["available"] is False
    assert status["access_mode"] == "authorized_browser"
    assert status["auto_enabled"] is False
    assert status["validation_status"] == "EXPECTED_DEGRADED"
    assert status["provider_tier"] == "expected_degraded_authorized_browser_only"
    assert "CAPTCHA_REQUIRED" in status["status_values"]
    assert "cnki_authorized_browser" not in order


def test_chinese_academic_provider_status_is_layered(monkeypatch) -> None:
    monkeypatch.delenv("BAIDU_QIANFAN_BEARER_TOKEN", raising=False)

    status = academic_provider_status()

    assert status["nssd"]["provider_tier"] == "p2_4_default_chinese_fulltext"
    assert status["chinaxiv"]["provider_tier"] == "p2_4_default_chinese_fulltext"
    assert status["baidu_scholar"]["validation_status"] == "EXPECTED_DEGRADED"
    assert status["baidu_scholar"]["provider_tier"] == "expected_degraded_official_api_when_unprovisioned"
    assert status["wanfang"]["validation_status"] == "EXPECTED_DEGRADED"
    assert status["wanfang"]["provider_tier"] == "expected_degraded_external_login_or_subscription"


def test_cnki_probe_result_normalizes_security_verification() -> None:
    result = _normalize_probe_result(
        {
            "status": "CAPTCHA_REQUIRED",
            "url": "https://kns.cnki.net/verify/home",
            "title": "安全验证",
            "items": [],
            "selectors": {"captchaVisible": True},
            "reason": "CNKI security verification page is visible",
        }
    )

    assert result["ok"] is False
    assert result["platform"] == "cnki"
    assert result["status"] == "CAPTCHA_REQUIRED"
    assert result["next_action"].startswith("ask user")
    assert "captcha bypass" in result["legal_boundary"]


def test_auto_order_prefers_citation_import_for_export_text(monkeypatch) -> None:
    monkeypatch.setenv("SERPAPI_API_KEY", "dummy")
    monkeypatch.setenv("KR_ACADEMIC_ENABLE_SERPAPI", "true")
    query = "TY  - JOUR\nTI  - 导入论文\nER  -\n"

    order = _provider_order(AcademicSearchRequest(query=query, provider="auto"), "auto")

    assert order[0] == "citation_import"


def test_transport_loggers_do_not_emit_info_urls() -> None:
    import server  # noqa: F401

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


class _FixedDate:
    @classmethod
    def now(cls):
        return cls()

    def strftime(self, fmt):
        assert fmt == "%Y-%m-%d"
        return "2099-01-01"
