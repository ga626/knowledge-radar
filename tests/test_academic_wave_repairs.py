"""学术波次、真实状态和正文留存的回归合同。"""
from types import SimpleNamespace
import json

import httpx
import pytest

from academic_providers.models import AcademicSearchRequest, AcademicWork
from academic_providers import service
from academic_providers.relevance import has_query_evidence
from academic_providers.profile import load_academic_provider_profiles
from academic_providers.planner import plan_academic_search
from academic_providers.wanfang import WanfangProvider
from academic_providers.crossref import CrossrefProvider
from academic_providers.fulltext import extract_academic_fulltext, _normalize_direct_read_status
from kr_core.evidence_store import EvidenceStore

pytestmark = pytest.mark.usefixtures("paid_test_policy")


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    service._CACHE.clear()
    monkeypatch.setattr(service, '_rate_limit', lambda *_: None)
    monkeypatch.setattr(service, 'academic_provider_status', lambda: {})


def test_wanfang_other_source_authorization_never_enables_paid_search():
    profiles = load_academic_provider_profiles()
    for authorized in (['core'], 'wanfang', {'wanfang': True}):
        plan = plan_academic_search(AcademicSearchRequest('人工智能', options={'authorized_sources': authorized}),
                                   'auto', profiles, wanfang_available=True)
        assert 'wanfang' not in plan.provider_order
    plan = plan_academic_search(AcademicSearchRequest('人工智能', options={'authorized_sources': ['wanfang']}),
                               'auto', profiles, wanfang_available=True)
    assert 'wanfang' in plan.provider_order


def test_vip_reading_candidate_never_implies_open_access_or_obtained_fulltext():
    from academic_providers.vip_oa import VipOpenAccessProvider
    work = VipOpenAccessProvider()._work_from_search_result(
        {'title': '人工智能', 'detail_url': 'https://www.cqvip.com/doc/journal/7203295942'},
        AcademicSearchRequest('人工智能'))
    assert work.raw['reading_url']
    assert work.oa_status != 'open'
    assert work.full_text_status == 'online_reading_candidate'
    assert work.raw['entitlement_status'] == 'not_checked'


def test_wrapped_http_failure_preserves_safe_diagnostics(monkeypatch):
    response = httpx.Response(429, headers={'Retry-After': '120'},
        json={'code': 'RATE_LIMIT', 'token': 'never-echo-this'},
        request=httpx.Request('GET', 'https://example.org/search?api_key=never-echo-this'))
    def search(request):
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            from academic_providers.openalex import OpenAlexError
            raise OpenAlexError('upstream failed') from exc
    monkeypatch.setattr(service, '_providers', lambda: {'openalex': SimpleNamespace(search=search)})
    result = service.search_academic_metadata(AcademicSearchRequest('cancer', provider='openalex'))
    diagnostic = result.error['details'][0]
    assert diagnostic['http_status'] == 429
    assert diagnostic['retry_after_seconds'] == 120
    assert diagnostic['provider_code'] == 'RATE_LIMIT'
    assert diagnostic['request_stage'] == 'search'
    assert 'never-echo-this' not in json.dumps(diagnostic)


def test_http_diagnostic_redacts_url_userinfo_as_well_as_query():
    from academic_providers.chinese_open_access import _fetch_diagnostic
    response=httpx.Response(403, json={'code': 'FORBIDDEN'},
        request=httpx.Request('GET','https://private-user:private-password@example.org/path?token=private-token'))
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        diagnostic=_fetch_diagnostic(exc)
    assert diagnostic['request_origin']=='https://example.org/'
    assert 'private-' not in json.dumps(diagnostic)


def test_nstrs_explicit_restriction_uses_public_permission_fields(monkeypatch):
    from academic_providers.nstrs import NstrsProvider
    p = NstrsProvider()
    monkeypatch.setattr(p, '_browser_search', lambda _: {'list': [{'id': 'r1', 'title': '人工智能'}]})
    work = p.search(AcademicSearchRequest('人工智能'))[0]
    assert work.raw['download_allowed'] is False
    assert work.raw['evidence_storage_allowed'] is False


def test_legacy_download_denial_cannot_be_lost_in_doi_merge():
    a = AcademicWork('Paper', 'https://example.org/a', doi='10.1234/a', raw={'fulltext_download_allowed': False})
    b = AcademicWork('Paper', 'https://example.org/b', doi=a.doi, raw={'download_allowed': True})
    for items in ([a,b], [b,a]):
        assert service._dedupe(items)[0].raw['download_allowed'] is False


def test_ntur_prefers_record_handle_over_rest_resource(monkeypatch):
    from academic_providers.ntur import NturProvider
    client = httpx.Client
    monkeypatch.setattr('runtime.proxy_config.get_httpx_proxy', lambda: None)
    def respond(req):
        item = {'name': 'Machine learning', 'uuid': 'example-uuid', 'metadata': {
            'dc.identifier.uri': [{'value': 'https://scholars.lib.ntu.edu.tw/handle/123456789/1'}]}}
        row = {'_embedded': {'indexableObject': item}, '_links': {
            'indexableObject': {'href': 'https://scholars.lib.ntu.edu.tw/server/api/core/items/example-uuid'}}}
        return httpx.Response(200, json={'_embedded': {'searchResult': {'_embedded': {'objects': [row]}}}})
    monkeypatch.setattr(httpx, 'Client', lambda **kw: client(transport=httpx.MockTransport(respond), **kw))
    work = NturProvider().search(AcademicSearchRequest('machine learning'))[0]
    assert work.url == 'https://scholars.lib.ntu.edu.tw/handle/123456789/1'
    assert '/server/api/' in work.raw['source_api_url']
    assert work.full_text_status == 'metadata_only'


def test_ntur_missing_public_identifier_is_unparsed_not_fake_article(monkeypatch):
    from academic_providers.ntur import NturProvider
    client = httpx.Client
    monkeypatch.setattr('runtime.proxy_config.get_httpx_proxy', lambda: None)
    row = {'_embedded': {'indexableObject': {'name': 'Machine learning', 'uuid': 'u1'}},
           '_links': {'indexableObject': {'href': 'https://scholars.lib.ntu.edu.tw/server/api/core/items/u1'}}}
    monkeypatch.setattr(httpx, 'Client', lambda **kw: client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={'_embedded': {'searchResult': {'_embedded': {'objects': [row]}}}})), **kw))
    result = NturProvider().search(AcademicSearchRequest('machine learning'))
    assert not result
    assert result.diagnostics[0]['type'] == 'search_response_unparsed'


def test_openalex_does_not_publish_unverified_daily_credit_as_current_policy():
    from academic_providers.openalex import OpenAlexProvider
    quota = OpenAlexProvider().status()['quota']
    assert quota['policy_status'] == 'not_verified_current'
    assert 'free_credit_per_day_usd' not in quota


def test_chinaxiv_identifier_link_does_not_consume_title_or_result_limit():
    from academic_providers.chinaxiv import ChinaXivProvider
    page='<a href="/abs/202611.00064">1. ChinaXiv:202611.00064</a><a href="/abs/202611.00064">人工智能与科学研究</a>'
    response=httpx.Response(200,text=page,request=httpx.Request('GET','https://chinaxiv.org/user/search.htm'))
    rows=ChinaXivProvider()._collect_response(response,'https://chinaxiv.org/user/search.htm',AcademicSearchRequest('人工智能'),set(),1)
    assert len(rows)==1 and rows[0].title=='人工智能与科学研究'


def test_wanfang_payment_failure_is_not_login_failure_and_leaks_no_account(monkeypatch):
    monkeypatch.setenv('WFDATA_ENABLED', 'true')
    monkeypatch.setenv('WFDATA_APP_KEY', 'never-echo-this')
    client = httpx.Client
    monkeypatch.setattr('runtime.proxy_config.get_httpx_proxy', lambda: None)
    payload = {'code': 'AUTH_PAYMENT_FAILED', 'app_key': 'never-echo-this', 'app_id': 'private',
               'billing': {'pending_amount': 0.01, 'billing_mode': 'postpaid'}}
    monkeypatch.setattr(httpx, 'Client', lambda **kw: client(transport=httpx.MockTransport(
        lambda req: httpx.Response(403, json=payload)), **kw))
    # 产品已停用万方：先核默认不发送，再单测历史HTTP回执分类。
    assert WanfangProvider().search(AcademicSearchRequest('人工智能')).diagnostics[0]['type'] == 'budget_blocked'
    from contextlib import nullcontext
    monkeypatch.setattr('runtime.paid_execution.paid_send', lambda *a, **kw: nullcontext())
    rows = WanfangProvider().search(AcademicSearchRequest('人工智能'))
    assert rows.diagnostics[0]['type'] == 'billing_required'
    assert rows.diagnostics[0]['pending_amount_cny'] == 0.01
    assert rows.diagnostics[0]['retryable'] is False
    assert 'never-echo-this' not in json.dumps(rows.diagnostics)
    assert 'app_id' not in json.dumps(rows.diagnostics)


def test_wanfang_protobuf_fields_and_exact_document_identity(monkeypatch):
    doc = {'resource_type': 'Thesis', 'fields': {
        'Id': {'stringValue': 'd1'}, 'Title': {'listValue': {'values': [{'stringValue': '人工智能'}]}},
        'PublishYear': {'numberValue': 2025}, 'Creator': {'values': [{'stringValue': '作者'}]}}}
    p = WanfangProvider()
    monkeypatch.setattr(p, '_request', lambda *_: (doc, []))
    assert p.get_document('OpenThesis', 'd1')[0].raw['identity_match'] is True
    assert p.get_document('OpenThesis', 'd1')[0].document_type == 'dissertation'
    assert p.get_document('OpenThesis', 'other').diagnostics[0]['type'] == 'identity_mismatch'


def test_crossref_doi_uses_identity_endpoint_and_validates_returned_doi(monkeypatch):
    seen = []
    client = httpx.Client
    def respond(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={'message': {'DOI': '10.1234/ABC', 'title': ['Unrelated title']}})
    monkeypatch.setattr(httpx, 'Client', lambda **kw: client(transport=httpx.MockTransport(respond), **kw))
    result = CrossrefProvider().search(AcademicSearchRequest('https://doi.org/10.1234/abc'))
    assert seen[0] == 'https://api.crossref.org/works/10.1234/abc'
    assert result[0].verification_status == 'doi_matched'
    assert has_query_evidence('10.1234/ABC', result[0])
    bad = CrossrefProvider().search(AcademicSearchRequest('10.1234/other'))
    assert bad.diagnostics[0]['type'] == 'identity_mismatch'


def test_chinese_question_keeps_topic_candidates_but_discards_generic_words():
    assert has_query_evidence('研究人工智能在医学影像中的应用', AcademicWork('医学影像智能诊断', 'https://example.org'))
    assert not has_query_evidence('study cancer immunotherapy review', AcademicWork('A review of bridge construction', 'https://example.org'))


def test_doi_duplicate_merges_abstract_source_links_without_mutating_input():
    a = AcademicWork('Paper', 'https://example.org/a', doi='10.1234/A', source_database='openalex')
    b = AcademicWork('Paper', 'https://example.org/b', doi='10.1234/a', abstract='Useful abstract',
        source_database='core', raw={'download_url': 'https://example.org/b.pdf'})
    result = service._dedupe([a, b])
    assert len(result) == 1 and result[0].abstract == 'Useful abstract'
    assert len(result[0].raw['source_records']) == 2
    assert 'https://example.org/b.pdf' in result[0].raw['fulltext_candidate_urls']
    assert a.raw == {} and a.abstract == ''
    assert len(service._dedupe([AcademicWork('Paper', '', year=2020), AcademicWork('Paper', '', year=2021)])) == 2


def test_default_search_never_reads_fulltext_or_reuses_mutated_cache(monkeypatch):
    class P:
        def search(self, request):
            return [AcademicWork('Cancer treatment', 'https://example.org', source_database='pubscholar')]
    monkeypatch.setattr(service, '_providers', lambda: {'pubscholar': P()})
    monkeypatch.setattr('academic_providers.fulltext.extract_academic_fulltext', lambda *a, **k: pytest.fail('默认不得读正文'))
    request = AcademicSearchRequest('cancer', provider='pubscholar', options={'_background_discovery':True})
    result = service.search_academic_metadata(request)
    assert result.metadata['fulltext_resolution']['applied'] is False
    result.items[0].raw['poison'] = True
    assert 'poison' not in service.search_academic_metadata(request).items[0].raw


def test_top_k_counts_readable_candidates_and_legacy_opt_in(monkeypatch):
    rows = [AcademicWork('Cancer', 'https://example.org/meta', source_database='crossref'),
            AcademicWork('Cancer', 'https://example.org/read', source_database='europepmc')]
    seen = []
    from academic_providers.fulltext import AcademicFullTextResult
    def extract(pid, url, **kw):
        seen.append(url)
        return AcademicFullTextResult(pid, 'PASS', url, 'public_html_text', True, text='actual body', text_length=11)
    monkeypatch.setattr('runtime.task_service.TaskService.submit', lambda self,op,payload,**kw:seen.append(__import__('academic_providers.identity',fromlist=['load_work']).load_work(payload['work_reference']).url) or {'task_id':'read-paper','status':'queued'})
    result = service._resolve_fulltext_candidates(AcademicSearchRequest('Cancer', options={
        'need_fulltext': True, 'fulltext_top_k': 1}), rows)
    assert seen == ['https://example.org/read']
    assert result['resolved_count'] == 0
    assert result['tasks'][0]['status']=='queued'
    result = service._resolve_fulltext_candidates(AcademicSearchRequest('Cancer', options={
        'resolve_fulltext': False, 'need_fulltext': True}), rows)
    assert result['applied'] is False


def test_probe_sample_does_not_prove_acquired_body():
    result = _normalize_direct_read_status('pubscholar', 'https://example.org', {
        'status': 'PASS', 'text_extractable': True, 'text_probe': {'sample': 'short sample', 'text_length': 5000}})
    assert result.status == 'PROBE_ONLY' and result.text == ''


def test_public_reader_uses_html_body_and_does_not_accept_abstract_page(monkeypatch):
    html = '<html><body><div class="ltx_document"><p>' + 'Actual scientific body. ' * 60 + '</p></div></body></html>'
    monkeypatch.setattr('generic_web.url_policy.guarded_fetch', lambda url, **kw:
        httpx.Response(200, text=html, request=httpx.Request('GET', url)))
    result = extract_academic_fulltext('ar5iv', 'https://ar5iv.labs.arxiv.org/html/2301.12345')
    assert result.status == 'PASS' and len(result.text) > 500 and result.coverage == 'partial'
    html = '<html><body><h1>Article title</h1><p>Abstract only</p></body></html>'
    result = extract_academic_fulltext('ar5iv', 'https://ar5iv.labs.arxiv.org/html/2301.12345')
    assert result.status != 'PASS' and result.text == ''


def test_public_reader_respects_host_specific_no_proxy_without_skipping_url_safety(monkeypatch):
    from generic_web.url_policy import guarded_fetch
    from contextlib import contextmanager
    monkeypatch.setenv('NO_PROXY', 'www.ebi.ac.uk')
    monkeypatch.setattr('runtime.proxy_config.get_httpx_proxy', lambda: 'http://127.0.0.1:7897')
    monkeypatch.setattr('generic_web.url_policy.socket.getaddrinfo', lambda *a, **k:
        [(2, 1, 6, '', ('193.62.193.80', 443))])
    seen = []
    class Client:
        def __init__(self, **kwargs): seen.append(kwargs['proxy'])
        def __enter__(self): return self
        def __exit__(self, *args): pass
        @contextmanager
        def stream(self, method, url, **kwargs):
            assert '193.62.193.80' in url
            assert kwargs['headers']['Host'] in {'www.ebi.ac.uk', 'example.org'}
            yield httpx.Response(200, content=b'body', request=httpx.Request(method, url))
    monkeypatch.setattr('generic_web.url_policy.httpx.Client', Client)
    guarded_fetch('https://www.ebi.ac.uk/article')
    guarded_fetch('https://example.org/article')
    assert seen == [None, 'http://127.0.0.1:7897']


def test_academic_text_asset_roundtrip_and_storage_failure_visible(tmp_path, monkeypatch):
    store = EvidenceStore(str(tmp_path / 'records.jsonl'))
    body = 'actual full text\n' * 100
    response = {'items': [{'title': 'Paper', 'url': 'https://example.org/paper', 'raw': {
        'fulltext_resolution': {'text': body, 'coverage': 'partial', 'mode': 'public_pdf_text'}}}]}
    store.append_academic_search(query='paper', response=response)
    item = response['items'][0]
    ref = item['evidence_assets'][0]
    assert store.assets.fetch(ref['version_id'], max_chars=10000)['text'] == body
    assert 'text' not in item['raw']['fulltext_resolution']
    assert store.recent()[0]['payload']['evidence_assets'][0]['version_id'] == ref['version_id']
    monkeypatch.setattr(store.assets, 'put', lambda *a, **k: (_ for _ in ()).throw(OSError('unavailable')))
    failure = {'items': [{'url': 'https://example.org/other', 'raw': {'fulltext_text': body}}]}
    store.append_academic_search(query='paper', response=failure)
    assert failure['evidence_storage_gaps'][0]['part'] == 'document_text'


def test_provider_budget_exposes_continuation_and_missing_required_coverage(monkeypatch):
    calls = []
    class P:
        def __init__(self, name): self.name = name
        def search(self, request):
            calls.append(self.name)
            return []
    monkeypatch.setattr(service, '_providers', lambda: {n: P(n) for n in ('openalex', 'crossref', 'arxiv')})
    monkeypatch.setattr(service, '_provider_plan', lambda *a: SimpleNamespace(
        provider_order=['openalex', 'crossref', 'arxiv'], required_providers=['openalex', 'arxiv']))
    monkeypatch.setattr(service, '_route_plan_metadata', lambda *_: {})
    result = service.search_academic_metadata(AcademicSearchRequest('cancer', provider='auto', options={'max_provider_attempts': 1}))
    budget = result.metadata['execution_budget']
    assert calls == ['openalex'] and budget['continuation'] == {'provider_offset': 1}
    assert budget['missing_required_providers'] == ['arxiv']
    service.search_academic_metadata(AcademicSearchRequest('cancer', provider='auto', options={'provider_offset': 1}))
    assert calls == ['openalex', 'crossref', 'arxiv']


def test_paused_wanfang_blocks_search_and_detail_even_with_saved_key(monkeypatch):
    monkeypatch.delenv('WFDATA_ENABLED', raising=False)
    monkeypatch.setenv('WFDATA_APP_KEY', 'saved-key-must-not-trigger-billing')
    monkeypatch.setattr(httpx, 'Client', lambda **kw: pytest.fail('停用的万方不得联网'))
    provider = WanfangProvider()
    assert provider.status()['available'] is False
    for rows in (provider.search(AcademicSearchRequest('人工智能')),
                 provider.get_document('OpenPeriodical', 'p1')):
        assert rows.diagnostics[0]['type'] == 'provider_disabled'
        assert rows.diagnostics[0]['request_executed'] is False
    assert 'wanfang' not in service._provider_plan(AcademicSearchRequest('人工智能',
        provider='auto', options={'authorized_sources': ['wanfang']}), 'auto').provider_order


@pytest.mark.parametrize('options', [
    {'deadline_s': 'no'}, {'deadline_s': float('nan')}, {'max_provider_attempts': []},
    {'fulltext_top_k': 'many'}, {'resolve_fulltext': 'false'}, {'fulltext_min_score': float('inf')},
])
def test_invalid_budget_returns_input_error_before_provider_calls(monkeypatch, options):
    monkeypatch.setattr(service, '_providers', lambda: pytest.fail('先验证输入再构造来源'))
    response = service.search_academic_metadata(AcademicSearchRequest('cancer', options=options))
    assert response.error['type'] == 'invalid_input'


def test_cached_response_isolated_and_credential_changes_invalidate_it(monkeypatch):
    calls = []
    class P:
        def __init__(self):
            import os
            self.api_key = os.environ.get('OPENALEX_API_KEY', '')
        def search(self, request):
            calls.append(self.api_key)
            return [AcademicWork('Cancer treatment', 'https://example.org/paper', source_database='openalex')]
    monkeypatch.setattr(service, '_providers', lambda: {'openalex': P()})
    monkeypatch.setenv('OPENALEX_API_KEY', 'first')
    req = AcademicSearchRequest('cancer', provider='openalex')
    service.search_academic_metadata(req)
    cached = service.search_academic_metadata(req)
    cached.metadata['execution_budget']['attempted_count'] = -10
    assert service.search_academic_metadata(req).metadata['execution_budget']['attempted_count'] == 1
    monkeypatch.setenv('OPENALEX_API_KEY', 'second')
    service.search_academic_metadata(req)
    assert calls == ['first', 'second']


def test_return_limit_does_not_read_papers_that_will_be_discarded(monkeypatch):
    rows = [AcademicWork('Cancer therapy ' + str(i), 'https://example.org/' + str(i),
        source_database='openalex', doi='10.1234/' + str(i),
        raw={'fulltext_text': 'actual body ' * 100}) for i in range(3)]
    monkeypatch.setattr(service, '_providers', lambda: {'openalex': SimpleNamespace(search=lambda _: rows)})
    seen = []
    from academic_providers.fulltext import AcademicFullTextResult
    def extract(pid, url, **kw):
        seen.append(url)
        return AcademicFullTextResult(pid, 'PASS', url, 'api_text', True, text='actual body', text_length=11)
    monkeypatch.setattr('runtime.task_service.TaskService.submit', lambda self,operation,payload,**kw: seen.append(__import__('academic_providers.identity',fromlist=['load_work']).load_work(payload['work_reference']).url) or {'task_id':'test-fulltext','status':'queued'})
    result = service.search_academic_metadata(AcademicSearchRequest('cancer', limit=1, provider='openalex',
        options={'resolve_fulltext': True, 'fulltext_top_k': 3}))
    assert seen == [result.items[0].url]
    assert result.metadata['fulltext_resolution']['resolved_count'] == 0
    assert result.metadata['fulltext_resolution']['tasks'][0]['status'] == 'queued'


def test_arxiv_version_is_preserved_and_pdf_fallback_is_within_url_budget(monkeypatch):
    from academic_providers.ar5iv import _arxiv_id_from_text
    assert _arxiv_id_from_text('https://arxiv.org/abs/1706.03762v5') == '1706.03762v5'
    assert _arxiv_id_from_text('https://arxiv.org/abs/hep-th/9901001v2') == 'hep-th/9901001v2'
    calls = []
    def fetch(url, **kw):
        calls.append(url)
        if '/pdf/' in url:
            return httpx.Response(200, content=b'%PDF-fixture', request=httpx.Request('GET', url))
        return httpx.Response(404, request=httpx.Request('GET', url))
    monkeypatch.setattr('generic_web.url_policy.guarded_fetch', fetch)
    monkeypatch.setattr('runtime.pdf_text.process_pdf', lambda *a, **k:
        {'text': 'actual body ' * 100, 'page_count': 2,
         'coverage': 'complete_selectable_text', 'processing_method': 'pypdf'})
    result = extract_academic_fulltext('arxiv', 'https://arxiv.org/abs/1706.03762v5',
        work=AcademicWork('Paper', 'https://arxiv.org/abs/1706.03762v5', source_database='arxiv'))
    assert result.status == 'PASS' and len(calls) <= 4
    assert 'https://arxiv.org/pdf/1706.03762v5' in calls


def test_pdf_page_locators_survive_academic_storage_and_readback(tmp_path, monkeypatch):
    from academic_providers.pubscholar import extract_pdf_text
    class Page:
        def __init__(self, text): self.text = text
        def get(self, key): return None
        def extract_text(self): return self.text
    monkeypatch.setattr('pypdf.PdfReader', lambda _: SimpleNamespace(is_encrypted=False, pages=[Page('Page one\nMethod'), Page('Page two\nResults')]))
    parsed = extract_pdf_text(b'%PDF-fixture')
    assert {row['page'] for row in parsed['locators']} == {1, 2}
    response = {'items': [{'url': 'https://example.org/paper', 'raw': {
        'fulltext_resolution': {'text': parsed['text'], 'locators': parsed['locators'], 'coverage': 'partial'}}}]}
    store = EvidenceStore(str(tmp_path / 'records.jsonl'))
    store.append_academic_search(query='paper', response=response)
    ref = response['items'][0]['evidence_assets'][0]
    saved = store.assets.fetch(ref['version_id'])
    assert {row['page'] for row in saved['locators']} == {1, 2}
    assert all(parsed['text'][row['start']:row['end']] for row in saved['locators'])


def test_unpermitted_academic_body_is_not_stored_and_reason_is_explicit(tmp_path):
    store = EvidenceStore(str(tmp_path / 'records.jsonl'))
    response = {'items': [{'url': 'https://example.org/paper', 'evidence_storage_allowed': False,
        'raw': {'fulltext_text': 'must not persist', 'fulltext_resolution': {'text': 'must not persist'}}}]}
    store.append_academic_search(query='paper', response=response)
    assert response['evidence_storage_gaps'][0]['reason'] == 'storage_not_permitted'
    assert 'must not persist' not in json.dumps(store.recent())


def test_mcp_search_to_saved_evidence_readback_contract(tmp_path, monkeypatch):
    import server
    import runtime.research_workspace as workspace
    from kr_core.evidence_assets import EvidenceAssets
    from runtime import tool_trace
    monkeypatch.setattr(tool_trace, 'get_tool_trace_recorder', lambda: SimpleNamespace(record=lambda _: None))
    body = 'Unrelated background.\nCancer treatment methods and measured outcomes.\n' * 30
    work = AcademicWork('Cancer treatment', 'https://example.org/paper', source_database='openalex',
        document_type='journal-article', raw={'fulltext_text': body})
    monkeypatch.setattr(service, '_providers', lambda: {'openalex': SimpleNamespace(search=lambda _: [work])})
    store = EvidenceStore(str(tmp_path / 'records.jsonl'))
    monkeypatch.setattr(server, 'evidence_store', store)
    monkeypatch.setattr('runtime.task_service.TaskService.submit', lambda *a,**k: {'task_id':'test-fulltext','status':'queued'})
    result = server.search_academic('cancer treatment', provider='openalex',
        options={'resolve_fulltext': True, 'fulltext_top_k': 1})
    item = result['items'][0]
    assert item['document_type'] == 'journal-article'
    assert result['metadata']['fulltext_resolution']['resolved_count'] == 0
    assert result['metadata']['fulltext_resolution']['tasks'][0]['task_id']=='test-fulltext'
    assert not item['raw'].get('fulltext_resolution',{}).get('text')
    assert 'fulltext_text' not in item['raw']
    assert item['raw']['fulltext_ref']['version_id']
    monkeypatch.setattr('kr_core.evidence_store.EvidenceStore', lambda: store)
    from use_cases.academic_fulltext import execute
    detail = execute({'work_reference':item['raw']['fulltext_ref']['version_id'], 'timeout_s':30})
    ref = detail['evidence_assets'][0]
    assert ref['coverage'] == 'partial' and ref['raw_sha256']
    records = workspace.ResearchRecords(tmp_path, assets=EvidenceAssets(store.assets.root, read_only=True))
    monkeypatch.setattr(workspace, 'ResearchRecords', lambda: records)
    saved = server.get_task_status(research_action='evidence_fetch',
        research_payload={'version_id': ref['version_id'], 'max_chars': 24000})
    assert saved['text'] == body and saved['locators']


def test_html_section_locators_remain_aligned(monkeypatch):
    html = '<div class="ltx_document"><h2>Methods</h2><p>' + 'Cancer therapy method. ' * 40 + '</p><h2>Results</h2><p>' + 'Measured outcomes. ' * 40 + '</p></div>'
    monkeypatch.setattr('generic_web.url_policy.guarded_fetch', lambda url, **kw:
        httpx.Response(200, text=html, request=httpx.Request('GET', url)))
    result = extract_academic_fulltext('ar5iv', 'https://ar5iv.labs.arxiv.org/html/1706.03762v7')
    assert result.status == 'PASS'
    assert {row.get('section') for row in result.locators} == {'Methods', 'Results'}
    assert all(result.text[row['start']:row['end']] for row in result.locators)


def test_pubscholar_known_pdf_uses_pdf_reader_instead_of_article_page(monkeypatch):
    from academic_providers import fulltext
    from academic_providers.pubscholar import PubScholarProvider
    provider = PubScholarProvider()
    url = 'https://pubscholar.cn/files?fastdfspath=fixture'
    monkeypatch.setattr(fulltext, 'instantiate_academic_providers', lambda *_: {'pubscholar': provider})
    monkeypatch.setattr(provider, 'verify_article_fulltext', lambda *_: pytest.fail('已知 PDF 不再当文章页打开'))
    monkeypatch.setattr(provider, 'verify_pdf_text_url', lambda _: {
        'status': 'PASS', 'text_extractable': True, 'text_probe': {'text': 'real PDF text', 'text_length': 13}})
    result = fulltext.extract_academic_fulltext('pubscholar', url,
        work=AcademicWork('Paper', url, source_database='pubscholar', raw={'pdf_url': url}))
    assert result.status == 'PASS' and result.text == 'real PDF text'
