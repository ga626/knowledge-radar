"""公开检索字段、路由、故障分类和本地配置链合同。"""

import httpx
import pytest

from academic_providers.models import AcademicSearchRequest
from academic_providers.napstic_oa import NapsticOaProvider
from academic_providers.planner import plan_academic_search
from academic_providers.profile import load_academic_provider_profiles
from academic_providers.semanticscholar import SemanticScholarProvider


@pytest.fixture(autouse=True)
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("KR_SEMANTIC_RATE_DB_PATH", str(tmp_path / "semantic.sqlite3"))
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    monkeypatch.setattr("runtime.proxy_config.get_httpx_proxy", lambda: None)


def mock_http(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))


def napstic_payload(records):
    return {"status": 200, "result": {"searchResult": {"total": len(records), "list": records}}}


def record():
    return {"identifier": {"id": "0120200404151190"}, "title": {"zh": "森林<em>生物量</em>模型研究综述"},
        "creators": [{"name": "欧光龙"}, {"name": "胥辉"}], "date": {"issue": 2020},
        "abstract": {"zh": "森林生物量模型及构建方法。"},
        "publication": {"name": {"zh": "西南林业大学学报"}, "year": 2020, "isOA": 1, "hasFulltext": 1},
        "url": {"doi": "10.11929/J.SWFU.201907029", "detail": "https://search.napstic.cn/other"}}


def test_napstic_real_search_contract_and_metadata(monkeypatch):
    def respond(req):
        assert req.url.path == "/search/literature"
        assert req.url.params["q"] == "生物量"
        assert req.url.params["size"] == "20" and req.url.params["pageNo"] == "1"
        assert "api-key" not in req.headers and "cookie" not in req.headers
        return httpx.Response(200, json=napstic_payload([record()]))
    mock_http(monkeypatch, respond)
    work = NapsticOaProvider().search(AcademicSearchRequest(query="生物量", limit=50))[0]
    assert work.title == "森林生物量模型研究综述"
    assert work.authors == ["欧光龙", "胥辉"] and work.year == 2020
    assert work.doi == "10.11929/j.swfu.201907029"
    assert work.abstract == "森林生物量模型及构建方法。"
    assert work.url == "https://opaj.napstic.cn/periodicalArticle/0120200404151190"
    assert work.full_text_status == "article_landing_unverified"
    assert work.raw["fulltext_verified"] is False and work.license_scope == "unknown"


@pytest.mark.parametrize("payload,kind", [
    (napstic_payload([]), "no_match"), ({"status": 200}, "search_response_unparsed"),
    (napstic_payload([{"identifier": {"id": "../../bad"}, "title": "bad"}]), "search_response_unparsed"),
    ({"status": 401}, "auth_required"), ({"status": 429}, "rate_limited"),
    ({"status": 500}, "provider_api_error"),
])
def test_napstic_business_failure_is_not_no_match(monkeypatch, payload, kind):
    mock_http(monkeypatch, lambda req: httpx.Response(200, json=payload))
    result = NapsticOaProvider().search(AcademicSearchRequest(query="生物量"))
    assert not result and result.diagnostics[0]["type"] == kind


@pytest.mark.parametrize("status,kind", [(403, "access_denied"), (404, "provider_http_error"), (429, "rate_limited")])
def test_napstic_http_failure(monkeypatch, status, kind):
    mock_http(monkeypatch, lambda req: httpx.Response(status))
    assert NapsticOaProvider().search(AcademicSearchRequest(query="生物量")).diagnostics[0]["type"] == kind


def test_napstic_empty_query_never_calls_network(monkeypatch):
    monkeypatch.setattr(httpx, "Client", lambda **kw: pytest.fail("空输入不得请求"))
    assert NapsticOaProvider().search(AcademicSearchRequest(query=" ")).diagnostics[0]["request_executed"] is False


def test_napstic_chinese_route_and_fulltext_boundary():
    profiles = load_academic_provider_profiles()
    for query in ("生物量", "教育政策", "人工智能"):
        assert "napstic_oa" in plan_academic_search(AcademicSearchRequest(query=query), "auto", profiles).required_providers
    for query in ("forest biomass", "10.1234/paper", "TY  - JOUR\nTI  - 中文\nER  -\n"):
        assert "napstic_oa" not in plan_academic_search(AcademicSearchRequest(query=query), "auto", profiles).provider_order
    assert profiles["napstic_oa"].content.abstract
    assert not profiles["napstic_oa"].content.pdf_fulltext
    assert not profiles["napstic_oa"].access.login_required


def test_napstic_service_keeps_diagnostics_and_results(monkeypatch):
    import academic_providers.service as service
    mock_http(monkeypatch, lambda req: httpx.Response(200, json=napstic_payload([record()])))
    service._CACHE.clear()
    monkeypatch.setattr(service, "_rate_limit", lambda *args: None)
    result = service.search_academic_metadata(AcademicSearchRequest(query="生物量", provider="napstic_oa"))
    assert len(result.items) == 1 and not result.error
    assert result.metadata["attempted_providers"] == ["napstic_oa"]
    assert result.metadata["provider_outcomes"][0]["outcome"] == "results"
    assert result.metadata["fulltext_resolution"]["applied"] is False
    assert result.metadata["fulltext_resolution"]["reason"] == "disabled_by_request"


@pytest.mark.parametrize("payload,kind", [({"data": []}, "no_match"), ({"error": "bad response"}, "search_response_unparsed"),
    ({"data": {}}, "search_response_unparsed"), ({"data": [{"paperId": "x"}]}, "search_response_unparsed")])
def test_semantic_empty_schema_distinction(monkeypatch, payload, kind):
    mock_http(monkeypatch, lambda req: httpx.Response(200, json=payload))
    result = SemanticScholarProvider(retries=0).search(AcademicSearchRequest(query="biomass"))
    assert not result and result.diagnostics[0]["type"] == kind


@pytest.mark.parametrize("status", [401, 403])
def test_semantic_auth_error_reaches_service_without_secret(monkeypatch, status):
    import academic_providers.service as service
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "fixture-secret")
    mock_http(monkeypatch, lambda req: httpx.Response(status))
    service._CACHE.clear()
    result = service.search_academic_metadata(AcademicSearchRequest(query="biomass", provider="semantic_scholar"))
    error = result.error["details"][0]
    assert error["type"] == "auth_required" and error["retryable"] is False
    assert "fixture-secret" not in str(result.to_mcp_dict())


def test_semantic_config_key_routing_and_oa_link(monkeypatch):
    profiles = load_academic_provider_profiles()
    provider = SemanticScholarProvider()
    assert provider.auto_available is False
    assert "semanticscholar" not in plan_academic_search(AcademicSearchRequest(query="biomass"), "auto", profiles).provider_order
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", " fixture-key ")
    provider = SemanticScholarProvider()
    assert provider.auto_available is True
    assert "semanticscholar" in plan_academic_search(AcademicSearchRequest(query="biomass"), "auto", profiles,
                                                    semantic_available=provider.auto_available).provider_order
    def respond(req):
        assert req.headers["x-api-key"] == "fixture-key"
        return httpx.Response(200, json={"data": [{"title": "Biomass study", "paperId": "abc",
            "url": "https://www.semanticscholar.org/paper/abc", "isOpenAccess": True,
            "openAccessPdf": {"url": "https://example.org/paper.pdf"}}]})
    mock_http(monkeypatch, respond)
    work = provider.search(AcademicSearchRequest(query="biomass"))[0]
    assert work.raw["open_access_pdf_url"] == "https://example.org/paper.pdf"
    assert work.raw["fulltext_verified"] is False


def test_semantic_invalid_json_is_not_empty_results(monkeypatch):
    mock_http(monkeypatch, lambda req: httpx.Response(200, text="<html>maintenance</html>"))
    result = SemanticScholarProvider(retries=0).search(AcademicSearchRequest(query="biomass"))
    assert result.diagnostics[0]["type"] == "search_response_unparsed"


def test_semantic_saved_configuration_loads_into_provider_on_startup(monkeypatch, tmp_path):
    from onboarding.configuration import apply_updates, public_provider_guides
    from runtime import env_loader
    path = tmp_path / ".env"
    monkeypatch.setenv("KR_RUNTIME_ENV_PATH", str(path))
    monkeypatch.setattr(env_loader, "REPO_ROOT", str(tmp_path))
    assert apply_updates({"SEMANTIC_SCHOLAR_API_KEY": "fixture-local-key"}, path) == ["SEMANTIC_SCHOLAR_API_KEY"]
    guides = public_provider_guides(path)
    guide = next(row for row in guides if row["id"] == "semantic_scholar")
    assert guide["configured"] is True
    assert "fixture-local-key" not in str(guides)
    env_loader.load_runtime_env()
    assert SemanticScholarProvider().api_key == "fixture-local-key"
