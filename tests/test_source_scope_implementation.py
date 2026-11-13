from conftest import patch_native
"""本轮来源收敛与社区授权合同：离线、不接正式账号。"""
import json
from contextlib import contextmanager

import httpx
import pytest

from academic_providers.models import AcademicSearchRequest, AcademicWork
from academic_providers.profile import load_academic_provider_profiles
from academic_providers.planner import plan_academic_search
from academic_providers.wanfang import WanfangProvider
from academic_providers.semanticscholar import SemanticScholarProvider, SemanticScholarRateLimitError
from collectors.platform import maimai

pytestmark = pytest.mark.usefixtures("paid_test_policy")


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setattr("runtime.proxy_config.get_httpx_proxy", lambda: None)
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("KR_TASK_DB_PATH",str(tmp_path/"tasks.sqlite3"))
    monkeypatch.setenv("KR_SEMANTIC_RATE_DB_PATH", str(tmp_path / "semantic.sqlite3"))
    monkeypatch.delenv("WFDATA_APP_KEY", raising=False)
    monkeypatch.setenv("WFDATA_ENABLED", "true")


@pytest.mark.parametrize("query,domain", [
    ("人工智能 教育", {"nssd", "chinaxiv", "sciengine", "arxiv"}),
    ("clinical gene cancer", {"europepmc"}),
    ("台湾 香港 教育 学位论文", {"nssd", "toaj", "ntur", "calis_thesis"}),
    ("科技报告 地质", {"nstrs", "chinaxiv", "sciengine"}),
])
def test_domain_sources_get_coverage_even_if_primary_fills_limit(query, domain, monkeypatch):
    import academic_providers.service as service
    service._CACHE.clear()
    profiles = load_academic_provider_profiles()
    plan = plan_academic_search(AcademicSearchRequest(query=query, limit=1), "auto", profiles)
    calls = []
    class Provider:
        def __init__(self, name): self.name = name
        def search(self, request):
            calls.append(self.name)
            return [AcademicWork(title=query + " " + self.name, url="https://example.org/" + self.name)]
    monkeypatch.setattr(service, "_providers", lambda: {name: Provider(name) for name in plan.provider_order})
    monkeypatch.setattr(service, "_provider_plan", lambda *a: plan)
    monkeypatch.setattr(service, "_rate_limit", lambda *a: None)
    monkeypatch.setattr(service, "academic_provider_status", lambda: {})
    result = service.search_academic_metadata(AcademicSearchRequest(query=query, provider="auto", limit=1, options={"resolve_fulltext": False, "_background_discovery": True}))
    assert len(result.items) == 1
    assert domain.issubset(calls)
    assert set(plan.required_providers).issubset(calls)
    assert "crossref" not in calls and "vip_oa" not in calls and "ar5iv" not in calls


def test_doi_with_existing_fulltext_does_not_repeat_oa_lookup():
    p = plan_academic_search(AcademicSearchRequest(query="10.1234/example", options={"has_readable_fulltext": True}), "auto", load_academic_provider_profiles())
    assert p.provider_order == ["crossref"]


def test_hkjo_explicit_boundary_and_real_service_dispatch(monkeypatch):
    import academic_providers.service as service
    profiles = load_academic_provider_profiles()
    assert profiles["hkjo"].default_policy == "explicit"
    automatic = plan_academic_search(AcademicSearchRequest(query="香港 教育"), "auto", profiles)
    assert "hkjo" not in automatic.provider_order
    calls = []
    class HKJO:
        def search(self, request):
            calls.append(request.query)
            return [AcademicWork(title="香港教育研究", url="https://example.org/hkjo")]
    service._CACHE.clear()
    monkeypatch.setattr(service, "_providers", lambda: {"hkjo": HKJO()})
    monkeypatch.setattr(service, "_rate_limit", lambda *a: None)
    monkeypatch.setattr(service, "academic_provider_status", lambda: {})
    result = service.search_academic_metadata(AcademicSearchRequest(query="香港 教育", provider="hkjo", limit=1))
    assert calls == ["香港 教育"] and len(result.items) == 1


def test_serpapi_branch_is_reachable_only_when_budget_admitted():
    profiles = load_academic_provider_profiles()
    request = AcademicSearchRequest(query="history")
    assert "serpapi_scholar" not in plan_academic_search(request, "auto", profiles).provider_order
    assert "serpapi_scholar" in plan_academic_search(request, "auto", profiles, serpapi_available=True).provider_order


def test_wanfang_without_key_makes_no_request(monkeypatch):
    monkeypatch.setattr(httpx, "Client", lambda **kw: pytest.fail("无授权不得联网"))
    result = WanfangProvider().search(AcademicSearchRequest(query="人工智能"))
    assert result.diagnostics[0]["type"] == "auth_required"
    assert result.diagnostics[0]["request_executed"] is False


def test_wanfang_current_post_header_and_detail_contract(monkeypatch, paid_test_policy):
    monkeypatch.setenv("WFDATA_APP_KEY", "fixture-not-a-secret")
    monkeypatch.setenv("WFDATA_ENABLED", "true")
    # This offline test checks request shape only; the disabled production cost
    # policy is exercised separately and is not loosened for a wire fixture.
    from contextlib import nullcontext
    monkeypatch.setattr('runtime.paid_execution.paid_send',lambda *a,**kw:nullcontext())
    monkeypatch.setattr("runtime.proxy_config.get_httpx_proxy", lambda: None)
    requests = []
    doc = {"resource_type": "Periodical", "fields": {"Title": ["人工智能论文"], "Id": "paper1", "Creator": ["张某"], "PublishYear": 2025, "DOI": "10.1234/example"}}
    def respond(req):
        requests.append(req)
        assert req.method == "POST" and req.headers["x-app-key"] == "fixture-not-a-secret"
        return httpx.Response(200, json={"documents": [doc]} if req.url.path.endswith("getQuery") else doc)
    client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(respond), **kw))
    provider = WanfangProvider()
    result = provider.search(AcademicSearchRequest(query="人工智能"))
    assert result[0].year == 2025 and result[0].full_text_status == "metadata_only"
    assert json.loads(requests[0].content)["collections"] == ["OpenPeriodical"]
    assert provider.get_document("OpenPeriodical", "paper1")[0].title == "人工智能论文"


@pytest.mark.parametrize("payload,kind", [({"documents": []}, "no_match"), ({"foo": []}, "search_response_unparsed"), ({"code": "INVALID_APP_KEY"}, "api_contract_or_entitlement_error")])
def test_wanfang_empty_and_business_error_differ(monkeypatch, payload, kind):
    monkeypatch.setenv("WFDATA_APP_KEY", "fixture")
    monkeypatch.setattr(WanfangProvider, "_request", lambda *a: (payload, []) if "code" not in payload else (None, [{"type": kind}]))
    result = WanfangProvider().search(AcademicSearchRequest(query="人工智能"))
    assert result.diagnostics[0]["type"] == kind


def test_wanfang_key_is_never_sent_by_plain_http(monkeypatch):
    monkeypatch.setenv("WFDATA_APP_KEY", "fixture")
    monkeypatch.setenv("WFDATA_BASE_URL", "http://api.wfdata.com")
    monkeypatch.setattr(httpx, "Client", lambda **kw: pytest.fail("明文地址不得发送 Key"))
    assert WanfangProvider().search(AcademicSearchRequest(query="人工智能")).diagnostics[0]["type"] == "configuration_error"


def test_semantic_slots_and_cooldown_shared_between_instances(monkeypatch):
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "fixture")
    first, second = SemanticScholarProvider(), SemanticScholarProvider()
    assert first.auto_available
    first._reserve()
    with pytest.raises(SemanticScholarRateLimitError):
        second._reserve()
    first._cooldown(120)
    assert not second.auto_available
    assert second.status()["cooldown_remaining_s"] > 110


def test_semantic_long_retry_after_does_not_retry_early(monkeypatch):
    monkeypatch.setattr("runtime.proxy_config.get_httpx_proxy", lambda: None)
    calls = []
    client = httpx.Client
    def respond(req):
        calls.append(req)
        return httpx.Response(429, headers={"Retry-After": "120"})
    monkeypatch.setattr(httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(respond), **kw))
    monkeypatch.setattr("academic_providers.semanticscholar.time.sleep", lambda *a: pytest.fail("长冷却不应阻塞或提前重试"))
    provider = SemanticScholarProvider()
    with pytest.raises(SemanticScholarRateLimitError):
        provider.search(AcademicSearchRequest(query="test"))
    assert len(calls) == 1 and provider.status()["cooldown_remaining_s"] > 110


def test_semantic_short_retry_after_respects_local_slot(monkeypatch):
    import academic_providers.semanticscholar as semantic
    clock = [1000.0]
    requests = []
    monkeypatch.setattr(semantic.time, "time", lambda: clock[0])
    monkeypatch.setattr(semantic.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    client = httpx.Client
    def respond(req):
        requests.append(clock[0])
        return httpx.Response(429, headers={"Retry-After": "0.1"}) if len(requests) == 1 else httpx.Response(200, json={"data": []})
    monkeypatch.setattr(httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(respond), **kw))
    assert SemanticScholarProvider().search(AcademicSearchRequest(query="test")) == []
    assert len(requests) == 2 and requests[1] - requests[0] >= 1.1 - 1e-8


@pytest.mark.parametrize("payload,status,url,text,expected", [
    ({"result": "ok", "user": {"id": 1}}, 200, "https://maimai.cn/community/home/recommended", "", "ok"),
    ({"result": "ok", "user": {}}, 200, "https://maimai.cn/community", "", "failed"),
    ({}, 204, "https://maimai.cn/community", "", "failed"),
    ({}, 401, "https://maimai.cn/community", "", "needs_interaction"),
    ({}, 200, "https://maimai.cn/platform/login?to=community", "验证码登录", "needs_interaction"),
    ({}, 403, "https://maimai.cn/community", "安全验证", "needs_interaction"),
    ({}, 429, "https://maimai.cn/community", "", "failed"),
])
def test_maimai_auth_needs_current_api_evidence(payload, status, url, text, expected):
    result = maimai._auth_from_probe({"http_status": status, "data": payload}, url, text)
    assert result["status"] == expected
    if expected == "failed":
        assert not result["manual_action_required"]


def test_maimai_search_preserves_gossip_and_feed_context_not_contacts():
    data = {"feeds": {"feeds": [{"efid": "feed-token", "feed": {"id": 12, "text": "研发岗位讨论"}}]},
            "gossips": {"gossips": [{"gid": 34, "gossip": {"egid": "gossip-token", "text": "招聘社区讨论"}}]},
            "contacts": {"contacts": [{"name": "不得当作帖子"}]}}
    rows = maimai._search_items(data, 10)
    assert len(rows) == 2
    assert "/feed-detail/12?" in rows[0]["url"] and "efid=feed-token" in rows[0]["url"]
    assert "/gossip-detail/34?" in rows[1]["url"] and "egid=gossip-token" in rows[1]["url"]
    assert all(row["content_type"] == "community_discussion_candidate" and not row["job_verified"] for row in rows)


def test_maimai_gossip_search_normalizes_identifier():
    rows=maimai._search_items({'gossips':[{'gid':'gossip_34','egid':'token','text':'社区讨论'}]},5)
    assert '/gossip-detail/34?' in rows[0]['url']


@pytest.mark.parametrize("url", ["https://maimai.cn.evil.example/community/feed-detail/12", "https://maimai.cn@evil.example/community/feed-detail/12", "file:///private", "https://maimai.cn/community/home/recommended"])
def test_maimai_detail_rejects_foreign_or_non_post_urls_without_browser(url, monkeypatch):
    patch_native(monkeypatch,lambda **kw:pytest.fail('must not borrow'))
    assert maimai.discussion_detail(url)['failure_type']=='unsupported_url'


def test_maimai_auth_failure_reaches_server_manual_handoff(monkeypatch):
    import server
    calls = []
    monkeypatch.setattr(server, "request_browser_interaction", lambda *a, **kw: calls.append(a) or {"status": "waiting_for_user"})
    result = server._promote_manual_interaction_result(maimai._failure("login_required", "请本人登录脉脉", manual=True), platform="maimai", original_tool="search_recruitment")
    assert calls and calls[0][0] == "maimai"
    assert result["metadata"]["manual_interaction_request"]["status"] == "waiting_for_user"


def test_nstrs_public_record_is_not_download_permission(monkeypatch):
    from academic_providers.nstrs import NstrsProvider
    monkeypatch.setattr(NstrsProvider, "_browser_search", lambda *a: {"RESULT": {"list": [{"id": "report1", "title": "人工智能报告"}]}})
    result = NstrsProvider().search(AcademicSearchRequest(query="人工智能"))
    assert result[0].raw["fulltext_download_allowed"] is False
    assert result[0].full_text_status == "registered_online_view_no_download"


def test_nstrs_delayed_publication_is_not_current_fulltext(monkeypatch):
    from academic_providers.nstrs import NstrsProvider
    monkeypatch.setattr(NstrsProvider, "_browser_search", lambda *a: {"RESULT": {"list": [{"id": "report1", "title": "报告", "publicScope": 2}]}})
    result = NstrsProvider().search(AcademicSearchRequest(query="人工智能"))
    assert result[0].full_text_status == "delayed_publication"


@pytest.mark.parametrize("status,payload,expected,manual", [
    (401, {}, "login_required", True),
    (200, {"error_code": "not_login"}, "login_required", True),
    (429, {}, "rate_limited", False),
    (403, {}, "access_denied", False),
])
def test_maimai_search_session_loss_is_classified_after_successful_auth(monkeypatch,status,payload,expected,manual):
    patch_native(monkeypatch,lambda **kw:{'ok':True,'data':{'probe':{'http_status':status,'data':payload}}})
    monkeypatch.setattr(maimai,'request_login',lambda **kw:{'status':'waiting_for_user'})
    result=maimai.legacy_search_maimai('招聘')
    assert result['failure_type']==expected and result['manual_action_required'] is manual


def test_maimai_worker_timeout_is_structured(monkeypatch):
    patch_native(monkeypatch,lambda **kw:{'ok':False,'code':'native_command_timeout','request_executed':True})
    assert maimai.legacy_search_maimai('招聘')['failure_type']=='request_outcome_unknown'
    assert not maimai.admission()['allowed']


@pytest.mark.parametrize("options,legacy,expected", [
    ({"include_comments": True}, False, True),
    ({"include_comments": False}, True, False),
    ({}, True, True),
    ({}, False, False),
])
def test_maimai_detail_strategy_obeys_explicit_comments_option(monkeypatch, options, legacy, expected):
    import server
    from kr_core import DetailRequest
    calls = []
    def detail(url, **kw):
        calls.append(kw)
        return {"platform": "脉脉", "url": url, "content": "帖子正文", "status": "ok"}
    monkeypatch.setattr(maimai, "discussion_detail", detail)
    request = DetailRequest(url="https://maimai.cn/community/feed-detail/12?efid=e", platform="脉脉",
                            options=options, enable_comment_filtering=legacy)
    response = server.registry.detail_strategy("脉脉").extract(request).to_legacy_dict()
    assert calls == [{"include_comments": expected,"comment_limit":20}] and response["content"] == "帖子正文"


def test_maimai_detail_login_loss_reaches_managed_handoff(monkeypatch):
    from kr_core import DetailRequest
    from platform_adapters.maimai_adapter import detail_strategy
    calls=[]
    patch_native(monkeypatch,lambda **kw:{'ok':False,'code':'AUTH_REQUIRED','request_executed':True})
    monkeypatch.setattr(maimai,'request_login',lambda **kw:calls.append(kw) or {'status':'waiting_for_user'})
    result=detail_strategy().extract(DetailRequest(url='https://maimai.cn/community/feed-detail/12?efid=e',platform='脉脉')).to_legacy_dict()
    assert calls and result['interaction']['status']=='waiting_for_user'


def test_generation_is_not_gene_but_real_gene_query_still_routes_biomed():
    from academic_providers.planner import analyze_academic_query
    assert 'biomed' not in analyze_academic_query(AcademicSearchRequest(query='retrieval augmented generation')).disciplines
    assert 'biomed' in analyze_academic_query(AcademicSearchRequest(query='clinical gene therapy')).disciplines


def test_academic_options_are_reachable_through_mcp_function(monkeypatch):
    import server
    from academic_providers.models import AcademicSearchResponse
    seen = []
    monkeypatch.setattr(server, 'search_academic_metadata', lambda request: seen.append(request) or AcademicSearchResponse(query=request.query, provider=request.provider))
    monkeypatch.setattr(server, '_record_academic_evidence', lambda *a, **kw: None)
    server.search_academic('10.1234/example', options={'has_readable_fulltext': True, 'resolve_fulltext': False})
    assert seen[0].provider == 'auto' and seen[0].options['has_readable_fulltext']


def test_academic_cache_cannot_merge_options_after_first_1000_chars():
    from academic_providers.service import _options_cache_key
    assert _options_cache_key({'a':'x'*1200,'document_id':'one'}) != _options_cache_key({'a':'x'*1200,'document_id':'two'})


def test_wanfang_document_lookup_is_public_search_option(monkeypatch):
    seen = []
    monkeypatch.setattr(WanfangProvider, 'get_document', lambda self, *args: seen.append(args) or [])
    WanfangProvider().search(AcademicSearchRequest(query='文献', options={'collection':'OpenThesis','document_id':'doc1'}))
    assert seen == [('OpenThesis','doc1')]


def test_retired_zhilian_has_no_search_detail_or_login_browser(monkeypatch):
    import server
    from runtime import chrome_manager as chrome
    from kr_core import DetailRequest
    monkeypatch.setattr(chrome, '_ensure_chrome_debugging_locked', lambda *a, **kw: pytest.fail('退役来源不能启动浏览器'))
    assert not chrome._ensure_chrome_debugging('zhilian')
    assert chrome.request_user_login('zhilian')['reason_code'] == 'SOURCE_DISABLED'
    assert 'zhilian' not in chrome.managed_browser_platforms()
    assert server.search_recruitment('zhilian','工程师')['failure_type'] == 'source_disabled'
    response = server._handle_detail_request(DetailRequest(url='https://jobs.zhaopin.com/CC123456789J00123456789.htm', platform='智联招聘'))
    assert response.to_legacy_dict()['failure_type'] == 'unsupported_url'


def test_maimai_dynamic_body_waits_for_requested_post(monkeypatch):
    url='https://maimai.cn/community/feed-detail/12?efid=e';calls=[]
    html='<script>self.__next_f.push('+json.dumps([1,'0:{"feedData":{"feed":{"id":12,"text":"真实正文"}}}'])+')</script>'
    patch_native(monkeypatch,lambda **kw:calls.append(kw) or {'ok':True,'data':{'url':url,'body_snapshot':{'schema':'maimai-body/v1','kind':'feed','target_id':'12','text':'真实正文','source':'matched_flight'}}})
    assert maimai.discussion_detail(url)['content']=='真实正文'
    assert calls[0]['operation']=='detail' and calls[0]['url']==url


def test_maimai_comment_failure_preserves_post_body(monkeypatch):
    monkeypatch.setattr(maimai,'_detail',lambda url:{'status':'ok','content':'已取得的帖子正文'})
    monkeypatch.setattr(maimai,'_comments',lambda *a:maimai._failure('comments_request_failed','评论不可用'))
    result=maimai.discussion_detail('https://maimai.cn/community/feed-detail/12?efid=e',include_comments=True)
    assert result['status']=='ok' and result['content']=='已取得的帖子正文'
    assert result['comments_status']=='comments_request_failed'


def test_maimai_temporary_page_and_operation_released_when_navigation_fails(monkeypatch):
    from runtime import recruitment_governance as g
    patch_native(monkeypatch,lambda **kw:{'ok':False,'code':'native_command_timeout','request_executed':True})
    assert maimai.legacy_search_maimai('招聘')['failure_type']=='request_outcome_unknown'
    lease=g.acquire_platform_lease('maimai')
    assert lease.acquired
    g.release_platform_lease(lease.lease_id)


@pytest.mark.parametrize('target,expected', [('12','ok'),('13','failed')])
def test_maimai_backend_validates_normalized_body_snapshot(monkeypatch,target,expected):
    url='https://maimai.cn/community/gossip-detail/12?egid=fixture'
    patch_native(monkeypatch, lambda **kw:{'ok':True,'data':{'url':url,'body_snapshot':{
        'schema':'maimai-body/v1','target_id':target,'kind':'gossip','text':'已取得正文','title':'标题','source':'matched_flight'}}})
    result=maimai.discussion_detail(url)
    assert result['status']==expected
    if expected=='ok': assert result['content']=='已取得正文'
    else: assert result['failure_type']=='page_identity_changed'



def test_content_deadline_retains_unknown_operation_and_phase(monkeypatch):
    patch_native(monkeypatch,lambda **kw:{'ok':False,'code':'content_deadline_exceeded','request_id':'fixture-deadline',
        'request_executed':True,'data':{'diagnostics':{'phase':'adapter_started','elapsed_ms':18000,'replay_safe':False,'result_known':False}}})
    result=maimai._detail('https://maimai.cn/community/gossip-detail/12?egid=fixture')
    assert result['failure_type']=='request_outcome_unknown'
    assert result['metadata']['content_diagnostics']['phase']=='adapter_started'
    assert maimai.admission()['reason']=='outcome_latched'
