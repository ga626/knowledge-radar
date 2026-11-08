"""网页重构关键合同：真实算法、隔离配置与有界网络替身，不调用账号。"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import threading
import time

import httpx
import pytest

from runtime.search_configuration import current_configuration, configuration_scope, sending_provider, check_search_send
from onboarding.configuration import apply_updates, public_provider_guides
from search_providers.models import WebSearchRequest, SearchProviderResult


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv('KR_RUNTIME_ENV_PATH',str(tmp_path/'runtime.env'))
    monkeypatch.setenv('KR_STATE_DIR',str(tmp_path/'state'))
    monkeypatch.setenv('KR_LOG_DIR',str(tmp_path/'logs'))
    monkeypatch.setenv('KR_HOST_SEARCH_CARD_DIR',str(tmp_path/'host'))
    monkeypatch.setenv('KR_WEB_SEARCH_PROVIDERS','')
    monkeypatch.setenv('KR_SEARCH_POLICY','{}')
    for name in ('ANYSEARCH_API_KEY','ANYSEARCH_SEARCH_ENDPOINT','TAVILY_API_KEY','BRAVE_SEARCH_API_KEY','EXA_API_KEY','SEARXNG_BASE_URL'):
        monkeypatch.delenv(name,raising=False)
    from generic_web.acquisition import _RESPONSES, _PARSED
    _RESPONSES.clear(); _PARSED.clear()
    monkeypatch.setenv('KR_EVIDENCE_ROOT',str(tmp_path/'assets'))
    monkeypatch.delenv('DDGS_BACKENDS', raising=False)


def test_pinned_ddgs_default_is_supported_and_preserves_real_upstream(monkeypatch):
    from ddgs.ddgs import DDGS
    from search_providers.providers import DdgsSearchProvider
    calls=[]
    def search(self, category, query, **kw):
        calls.append((category, query, kw))
        return [{'title':'说明','href':'https://example.org/a','body':'正文线索'}]
    monkeypatch.setattr(DDGS,'_search_sync',search)
    rows=DdgsSearchProvider().search(WebSearchRequest('说明',language='zh'))
    assert calls[0][2]['backend']=='duckduckgo' and calls[0][2]['region']=='cn-zh'
    assert rows[0].raw['discovery_backend']=='duckduckgo' and rows[0].raw['upstream_engine']=='bing'
    from search_providers.providers import SearchProviderError
    with pytest.raises(SearchProviderError):
        DdgsSearchProvider().search(WebSearchRequest('q',options={'ddgs_backend':'auto'}))
    assert len(calls)==1


def test_http_pool_never_retains_cookie_or_authentication(monkeypatch):
    from search_providers import http_client as module
    module.close_shared_clients()
    monkeypatch.setattr('runtime.proxy_config.get_httpx_proxy',lambda:None)
    received=[]
    def respond(req):
        received.append(dict(req.headers))
        return httpx.Response(200,json={'ok':True},headers={'set-cookie':'identity=secret; Path=/'})
    original=httpx.Client
    monkeypatch.setattr(httpx,'Client',lambda **kw:original(transport=httpx.MockTransport(respond),**kw))
    module.request_json('GET','https://example.org/a',timeout=1,headers={'Authorization':'first'})
    module.request_json('GET','https://example.org/b',timeout=2,headers={'Authorization':'second'})
    assert received[0]['authorization']=='first' and received[1]['authorization']=='second'
    assert all('cookie' not in row for row in received)
    assert module.shared_client_summary()['client_count']==1
    module.close_shared_clients()


def test_http_revalidation_cannot_bypass_smaller_document_budget():
    from generic_web.acquisition import fetch
    from generic_web.models import GenericWebRequest
    from generic_web.url_policy import URLPolicyError
    req=GenericWebRequest('https://example.org/file',max_bytes=100,deadline_at=time.monotonic()+20)
    def initial(url,**kw):
        return httpx.Response(200,headers={'etag':'one','cache-control':'no-cache'},content=b'12345',request=httpx.Request('GET',url))
    fetch(req,{},initial)
    def revalidate(url,**kw):
        return httpx.Response(304,request=httpx.Request('GET',url))
    with pytest.raises(URLPolicyError):
        fetch(replace(req,max_bytes=2),{},revalidate)


def test_anonymous_get_retries_connection_setup_once_but_never_http_refusal():
    from generic_web.acquisition import fetch
    from generic_web.models import GenericWebRequest
    request=GenericWebRequest('https://example.org/plain',deadline_at=time.monotonic()+10)
    seen=[]
    def temporary(url,**kw):
        seen.append(1)
        if len(seen)==1:raise httpx.ConnectError('TLS connection setup')
        return httpx.Response(200,content=b'data',request=httpx.Request('GET',url))
    result=fetch(request,{},temporary)
    assert seen==[1,1] and result.extensions['acquisition_fetch_count']==2
    seen.clear()
    def refused(url,**kw):
        seen.append(1)
        return httpx.Response(403,request=httpx.Request('GET',url))
    assert fetch(request,{},refused).status_code==403 and len(seen)==1


def test_web_pdf_uses_common_processor_and_keeps_actual_ocr_method(monkeypatch):
    from generic_web.collector import collect_url
    from generic_web.models import GenericWebRequest
    from generic_web import collector
    monkeypatch.setattr(collector,'public_addresses',lambda *a,**kw:None)
    monkeypatch.setattr(collector,'guarded_fetch',lambda url,**kw:httpx.Response(200,
        content=b'%PDF-fixture',headers={'content-type':'application/octet-stream'},request=httpx.Request('GET',url)))
    calls=[]
    def process(data,**kw):
        calls.append(data)
        return {'text':'Selected\nScanned','document_blocks':[{'page':1,'text':'Selected'},{'page':2,'text':'Scanned'}],
                'coverage':'complete_text_and_ocr','processing_method':'pypdf+page_ocr','ocr_status':'complete'}
    monkeypatch.setattr('runtime.pdf_text.process_pdf',process)
    result=collect_url(GenericWebRequest('https://example.org/file',use_jina=False))
    assert not result.error and calls==[b'%PDF-fixture']
    assert result.metadata['processing_method']=='pypdf+page_ocr'


def test_public_configuration_matches_effective_process_and_file_tombstone(monkeypatch):
    from onboarding.configuration import public_snapshot
    monkeypatch.setenv('BRAVE_SEARCH_API_KEY','process-secret')
    assert next(x for x in public_snapshot()['fields'] if x['provider_id']=='brave')['configured']
    apply_updates({},delete_keys=['BRAVE_SEARCH_API_KEY'])
    snapshot=public_snapshot()
    assert not next(x for x in snapshot['fields'] if x['provider_id']=='brave')['configured']
    assert 'process-secret' not in json.dumps(snapshot)


def test_readiness_requires_enabled_route_and_supports_keyless_library():
    from onboarding.configuration import public_snapshot
    from onboarding.product_status import capability_packs
    apply_updates({'BRAVE_SEARCH_API_KEY':'test-key'})
    assert next(x for x in capability_packs(public_snapshot()) if x['id']=='core_web')['status']=='needs_setup'
    apply_updates({},provider_updates={'ddgs':{'enabled':True,'authorized':True}})
    assert next(x for x in capability_packs(public_snapshot()) if x['id']=='core_web')['status']=='ready'
    assert public_snapshot()['search_health']=='not_probed'


def test_pdf_expansion_budget_and_mixed_text_coverage(monkeypatch):
    from io import BytesIO
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject
    from runtime import pdf_text
    writer=PdfWriter()
    page=writer.add_blank_page(100,100)
    stream=DecodedStreamObject();stream.set_data(b' '*100)
    page[NameObject('/Contents')]=stream
    out=BytesIO();writer.write(out)
    monkeypatch.setattr(pdf_text,'MAX_EXPANDED_CONTENT_BYTES',64)
    with pytest.raises(ValueError,match='document_expansion_budget_exceeded'):
        pdf_text.extract_pdf_text(out.getvalue())
    monkeypatch.setattr(pdf_text,'MAX_EXPANDED_CONTENT_BYTES',200)
    result=pdf_text.extract_pdf_text(out.getvalue())
    assert result['coverage']=='partial' and result['empty_pages']==[1]


def test_evidence_asset_receipt_keeps_pdf_coverage():
    from kr_core.evidence_store import EvidenceStore
    from kr_core.evidence_assets import EvidenceAssets
    detail={'content':'已抽取原文','metadata':{'coverage':'complete_selectable_text'}}
    EvidenceStore().record_detail(platform='web',url='https://example.org/a.pdf',response=detail)
    ref=detail['evidence_assets'][0]
    assert ref['coverage']==detail['content_coverage']['parts']['content']['coverage']=='complete_selectable_text'
    assert EvidenceAssets().fetch(ref['version_id'])['text']=='已抽取原文'


def test_academic_reader_never_converts_cancellation_to_fulltext_unavailable(monkeypatch):
    from academic_providers import fulltext
    from runtime.execution_context import ExecutionStopped
    class Reader:
        def verify_article_fulltext(self,url):raise ExecutionStopped('execution_superseded_or_cancelled')
    monkeypatch.setattr(fulltext,'provider_supports_direct_read',lambda *a,**kw:True)
    monkeypatch.setattr(fulltext,'instantiate_academic_providers',lambda *a,**kw:{'fixture':Reader()})
    with pytest.raises(ExecutionStopped):
        fulltext.extract_academic_fulltext('fixture','https://example.org',profiles={'fixture':None})


def test_one_dynamic_request_timeout_keeps_shared_browser_usable(monkeypatch):
    import asyncio
    from contextlib import nullcontext
    from generic_web import browser_runtime as module
    module.close()
    # 本例验证取消/复用，不把跨进程租约的磁盘耗时当成1秒浏览器SLA。
    # 真实租约的互斥、释放与进程身份由test_resource_concurrency_leases覆盖。
    monkeypatch.setattr('runtime.resource_concurrency.acquire_resource', lambda *a, **kw: nullcontext())
    opened,closed,launches=[],[],[]
    entered = threading.Event()
    cancelled = threading.Event()
    class PageContext:
        def __init__(self): self.id=len(opened);opened.append(self.id)
        async def close(self): closed.append(self.id)
    class Browser:
        def is_connected(self):return True
        async def new_context(self,**kw):return PageContext()
        async def close(self):pass
    async def launch():launches.append(1);module._BROWSER=Browser()
    monkeypatch.setattr(module,'_launch_browser',launch)
    async def work(block):
        async with module.context() as (ctx,reused):
            entered.set()
            try:
                if block:
                    await asyncio.Event().wait()
                return reused
            finally:
                if block:
                    cancelled.set()
    try:
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(module.run, work(True), timeout=1)
            assert entered.wait(5), 'request did not enter its browser context'
            with pytest.raises(TimeoutError):
                pending.result(timeout=5)
        assert cancelled.wait(5), 'timed-out request did not receive cancellation'
        assert module.run(work(False),timeout=5) is True
    finally:
        module.close()
    assert len(launches)==1 and sorted(opened)==sorted(closed)


def test_save_delete_generation_and_revoke(monkeypatch):
    monkeypatch.setenv('BRAVE_SEARCH_API_KEY','process-old-secret')
    initial = current_configuration(fresh=True)
    assert not initial.allowed('brave')
    apply_updates({'BRAVE_SEARCH_API_KEY':'new-secret'},provider_updates={'brave':{'enabled':True,'authorized':True}},expected_generation=initial.generation)
    active = current_configuration(fresh=True)
    assert active.allowed('brave') and active.values['BRAVE_SEARCH_API_KEY']=='new-secret'
    assert active.generation!=initial.generation
    with pytest.raises(ValueError,match='其他窗口'):
        apply_updates({},expected_generation=initial.generation)
    with configuration_scope(active), sending_provider('brave'):
        apply_updates({},delete_keys=['BRAVE_SEARCH_API_KEY'],provider_updates={'brave':{'enabled':False,'authorized':False}})
        with pytest.raises(PermissionError,match='revoked'):
            check_search_send()
    assert current_configuration(fresh=True).values['BRAVE_SEARCH_API_KEY']==''
    assert 'new-secret' not in json.dumps(public_provider_guides())


def test_parallel_config_saves_do_not_lose_fields():
    with ThreadPoolExecutor(2) as pool:
        futures=[pool.submit(apply_updates,{'BRAVE_SEARCH_API_KEY':'a'}),pool.submit(apply_updates,{'EXA_API_KEY':'b'})]
        for f in futures: f.result()
    values=current_configuration(fresh=True).values
    assert values['BRAVE_SEARCH_API_KEY']=='a' and values['EXA_API_KEY']=='b'


@pytest.mark.parametrize('policy',[{'providers':[]},{'providers':{'brave':{'enabled':'true'}}},{'connections':[{'id':'bad'}]},[]])
def test_bad_nested_policy_fails_closed(monkeypatch, policy):
    monkeypatch.setenv('KR_SEARCH_POLICY',json.dumps(policy))
    config=current_configuration(fresh=True)
    assert not any(config.allowed(x) for x in ('anysearch','brave','tavily','searxng:bad'))


def test_retired_instance_config_cannot_send_or_disable_other_sources(monkeypatch):
    connection={'id':'public-one','url':'https://search.example.com','mode':'public','enabled':True,'authorized':True}
    monkeypatch.setenv('KR_SEARCH_POLICY',json.dumps({'providers':{'searxng':{'enabled':True,'authorized':True}},'connections':[connection]}))
    active=current_configuration(fresh=True)
    assert not active.allowed('searxng:public-one') and not active.allowed('searxng')
    assert active.allowed('anysearch') and not active.policy.get('configuration_error')
    with configuration_scope(active),sending_provider('searxng:public-one'):
        with pytest.raises(PermissionError,match='revoked'):
            check_search_send()
    with pytest.raises(ValueError,match='退役'):
        apply_updates({},connections=[connection])
    from search_providers.service import _providers
    assert not any(n.startswith('searxng') for n in _providers())
    assert not any(g['id']=='searxng' for g in public_provider_guides())


@pytest.mark.parametrize('url',['http://search.example.com','https://127.0.0.1','https://u:p@example.com','https://example.com?token=x'])
def test_public_instance_rejects_private_or_sensitive_urls(url):
    from runtime.search_configuration import validate_connection_url
    with pytest.raises(ValueError): validate_connection_url(url)


def test_optional_keys_do_not_join_default_plan():
    from search_providers.planner import auto_search_plan
    apply_updates({'BRAVE_SEARCH_API_KEY':'a','EXA_API_KEY':'b'},provider_updates={
        'brave':{'enabled':True,'authorized':True},'exa':{'enabled':True,'authorized':True}})
    plan=auto_search_plan({name:{'available':True} for name in ('anysearch','brave','exa')})
    assert plan.waves==[['anysearch']]


def test_strict_unsupported_constraint_does_not_send(monkeypatch):
    from search_providers.service import _search_provider
    class Provider:
        name='anysearch'
        def search(self,request): raise AssertionError('must not send')
    _,items,error=_search_provider(WebSearchRequest('q',freshness='week',options={'require_constraints':True}),Provider())
    assert not items and error['type']=='unsupported_constraint'


def test_native_capability_does_not_verify_a_language_constraint():
    from search_providers.constraints import constraint_report
    from search_providers.models import WebSearchRequest
    request = WebSearchRequest('技术实践', language='zh')
    planned = constraint_report(request, 'anysearch')['language']
    observed = constraint_report(request, 'anysearch', sent=True)['language']
    assert planned['sent'] is None and not planned['verified']
    assert observed['sent'] == 'zh' and not observed['hard_constraint_satisfied']


def test_domain_filter_is_enforced_and_reported():
    from search_providers.constraints import filter_results, constraint_report
    request=WebSearchRequest('q',options={'include_domains':['example.com']})
    good=SearchProviderResult('good','https://sub.example.com/a')
    bad=SearchProviderResult('bad','https://example.com.evil.test/a')
    assert filter_results(request,[good,bad])==[good]
    assert constraint_report(request,'anysearch')['domains']['method']=='post_filter'
    assert constraint_report(request,'anysearch')['domains']['locally_enforceable'] is True
    checked=constraint_report(request,'anysearch',sent=True,filtered=True)['domains']
    assert checked['verified'] and checked['hard_constraint_satisfied']
    assert checked['verification_scope']=='returned_urls_only' and not checked['recall_guaranteed']


def test_strict_domains_allow_search_and_filter_without_claiming_recall(monkeypatch):
    from search_providers.service import _search_provider
    class Provider:
        name='anysearch'
        def status(self): return {}
        def available(self): return True
        def search(self,request):
            return [SearchProviderResult('ok','https://example.com/a'),SearchProviderResult('wrong','https://other.test/a')]
    request=WebSearchRequest('q',options={'include_domains':['example.com'],'require_constraints':True})
    _,items,error=_search_provider(request,Provider())
    assert not error and [x.url for x in items]==['https://example.com/a']


def test_duplicate_upstreams_do_not_multiply_ranking_votes():
    from search_providers.aggregation import aggregate_results
    a=SearchProviderResult('a','https://example.com/a',raw={'upstream_engine':'bing'})
    b=replace(a,raw={'engines':['bing']})
    one=aggregate_results({'ddgs':[a]},1)[0]
    two=aggregate_results({'ddgs':[a],'searxng:one':[b],'searxng:two':[b]},1)[0]
    assert one.raw['rank_score']==two.raw['rank_score']
    assert len(two.raw['provider_provenance'])==3


def test_one_download_even_when_local_parsers_fallback(monkeypatch):
    import generic_web.collector as collector
    calls=[]
    monkeypatch.setattr(collector,'public_addresses',lambda *a,**kw:None)
    monkeypatch.setattr(collector,'guarded_fetch',lambda url,**kw: calls.append(url) or httpx.Response(200,
        headers={'content-type':'text/html'},text='<html><title>公告</title><article><p>今晚维护。</p></article></html>',request=httpx.Request('GET',url)))
    def failed(*args,**kw): raise collector.GenericWebError('parse_failed','fixture',collector='first')
    monkeypatch.setattr(collector,'_collect_via_trafilatura',failed)
    monkeypatch.setattr(collector,'_collect_via_readability',failed)
    result=collector.collect_url(collector.GenericWebRequest(url='https://example.com/a',use_jina=False))
    assert not result.error and '今晚维护' in result.content
    assert len(calls)==1 and result.metadata['acquisition']['fetch_count']==1


def test_primary_extractor_keeps_substantive_limitations_in_lists():
    from generic_web.collector import _collect_via_trafilatura
    from generic_web.models import GenericWebRequest
    html='<html><title>WAL notes</title><body><p>'+('Transaction model explanation. '*60)+'</p><ol><li>WAL permits only one writer at a time.</li><li>Readers and a writer may run concurrently.</li></ol><p>'+('Read the limitations before deployment. '*60)+'</p></body></html>'
    request=GenericWebRequest(url='https://example.org/wal')
    artifact=(httpx.Response(200,text=html,request=httpx.Request('GET',request.url)),'text/html')
    result=_collect_via_trafilatura(request,artifact=artifact)
    assert 'WAL permits only one writer at a time.' in result.content
    assert 'Readers and a writer may run concurrently.' in result.content


@pytest.mark.parametrize('status,kind',[(401,'authentication_required'),(403,'access_forbidden'),(404,'not_found'),(429,'rate_limited'),(451,'legal_restriction')])
def test_http_terminal_errors_never_trigger_extra_readers(monkeypatch,status,kind):
    import generic_web.collector as collector
    calls=[]
    monkeypatch.setattr(collector,'public_addresses',lambda *a,**kw:None)
    monkeypatch.setattr(collector,'guarded_fetch',lambda url,**kw:calls.append(url) or httpx.Response(status,
        headers={'content-type':'text/html','retry-after':'30'},text='failure'*300,request=httpx.Request('GET',url)))
    result=collector.collect_url(collector.GenericWebRequest('https://example.com/'+str(status)))
    assert len(calls)==1 and result.error['type']==kind
    if status==429: assert result.error['retry_after']=='30'


def test_loading_shell_and_cloudflare_article_are_different(monkeypatch):
    import generic_web.collector as collector
    monkeypatch.setattr(collector,'public_addresses',lambda *a,**kw:None)
    monkeypatch.setattr(collector,'guarded_fetch',lambda url,**kw:httpx.Response(200,headers={'content-type':'text/html'},
        text='<div id="app">Loading...</div><script src="app.js"></script>',request=httpx.Request('GET',url)))
    result=collector.collect_url(collector.GenericWebRequest('https://example.com/shell',use_jina=False))
    assert result.error['type']=='requires_dynamic_render'
    assert not collector._looks_blocked('Cloudflare challenge pages can be diagnosed using these steps.')
    rendered=collector.collect_rendered_html(collector.GenericWebRequest('https://example.com'),html='<article>long failure</article>',status_code=403)
    assert rendered.error['type']=='access_forbidden'


def test_http_cache_revalidates_and_no_store_does_not_persist():
    from generic_web.acquisition import fetch, _RESPONSES
    from generic_web.models import GenericWebRequest
    request=GenericWebRequest('https://example.com/cache',deadline_at=time.monotonic()+10)
    calls=[]
    def transport(url,**kw):
        calls.append(kw['headers'])
        return httpx.Response(200 if len(calls)==1 else 304,headers={'etag':'"one"','cache-control':'no-cache'},
            content=b'body' if len(calls)==1 else b'',request=httpx.Request('GET',url))
    assert fetch(request,{},transport).content==b'body'
    assert fetch(request,{},transport).content==b'body'
    assert calls[1]['If-None-Match']=='"one"'
    def private(url,**kw): return httpx.Response(200,headers={'cache-control':'no-store'},content=b'private',request=httpx.Request('GET',url))
    fetch(request,{},private)
    assert not _RESPONSES


def test_search_cache_bounds_copy_and_one_execution_coalescing():
    from kr_core.search_cache import SearchCache
    cache=SearchCache(max_entries=2,max_bytes=10000)
    gate=threading.Event(); started=threading.Event(); calls=[]
    def work():
        calls.append(1); started.set(); assert gate.wait(3)
        return {'items':[{'title':'original'}]}
    with ThreadPoolExecutor(2) as pool:
        one=pool.submit(cache.run,'same',work)
        assert started.wait(1)
        two=pool.submit(cache.run,'same',work)
        gate.set(); a=one.result(); b=two.result()
    assert len(calls)==1
    a['items'][0]['title']='changed'
    assert b['items'][0]['title']=='original'
    cache.set('two',{'items':[]}); cache.set('three',{'items':[]})
    assert cache.summary()['entries']==2 and cache.get('same') is None


def test_host_handoff_is_fixed_idempotent_and_declared(monkeypatch,tmp_path):
    from runtime.host_search_handoff import register, submit
    from runtime.research_index import ResearchIndex
    monkeypatch.setattr('runtime.research_ledger.read_task',lambda **kw:{'status':'open'})
    index=ResearchIndex(tmp_path/'index')
    batch=register('task-a','official docs',index=index)
    rows=[{'title':'Official','url':'https://example.com/docs','host_reference':'actual-host-reference'}]
    first=submit('task-a',batch['batch_id'],rows,host_tool='web.run',index=index)
    again=submit('task-a',batch['batch_id'],rows,host_tool='web.run',index=index)
    assert first==again and first['verification']=='host_declared_not_native_verified'
    assert len(index.read('task-a')['items'])==1
    assert index.read('task-a',view='host_batches')['items'][0]['status']=='projected'
    with pytest.raises(ValueError,match='conflict'):
        submit('task-a',batch['batch_id'],[{'title':'changed','url':'https://example.com/other', 'host_reference':'other-reference'}],host_tool='web.run',index=index)


def test_host_empty_payload_is_not_zero_matches(monkeypatch,tmp_path):
    from runtime.host_search_handoff import register, submit
    from runtime.research_index import ResearchIndex
    monkeypatch.setattr('runtime.research_ledger.read_task',lambda **kw:{'status':'open'})
    index=ResearchIndex(tmp_path/'index')
    batch=register('task-a','official docs',index=index)
    with pytest.raises(ValueError,match='requires_explicit'):
        submit('task-a',batch['batch_id'],[],host_tool='web.run',index=index)
    result=submit('task-a',batch['batch_id'],[],host_tool='web.run',host_outcome='unavailable',index=index)
    assert result['status']=='blocked' and result['host_outcome']=='unavailable'
    assert index.read('task-a',view='host_batches')['items'][0]['status']=='blocked'
    assert not index.read('task-a')['items']
    batch=register('task-a','confirmed zero',index=index)
    assert submit('task-a',batch['batch_id'],[],host_tool='web.run',host_outcome='no_results',index=index)['status']=='recorded'


@pytest.mark.parametrize('status,kind', [(202,'upstream_challenge'),(403,'access_forbidden'),(429,'rate_limited')])
def test_ddgs_keeps_suppressed_upstream_status(monkeypatch,status,kind):
    from ddgs.http_client import HttpClient
    from types import SimpleNamespace
    from search_providers.providers import DdgsSearchProvider, SearchProviderError
    calls=[]
    def respond(self,*args,**kwargs):
        calls.append(args)
        return SimpleNamespace(status_code=status, text='blocked')
    monkeypatch.setattr(HttpClient,'request',respond)
    with pytest.raises(SearchProviderError) as caught:
        DdgsSearchProvider().search(WebSearchRequest('query',language='en'))
    assert caught.value.error_type==kind and caught.value.status_code==status
    assert len(calls)==1


def test_ddgs_worker_respects_permission_revoked_before_send(monkeypatch):
    from contextvars import ContextVar
    from ddgs.http_client import HttpClient
    from runtime import search_configuration
    from search_providers.providers import DdgsSearchProvider, SearchProviderError
    marker = ContextVar('worker_send_marker', default=False)
    marker.set(True)
    seen = []
    def check():
        seen.append(marker.get())
        if len(seen) > 1:
            raise PermissionError('search_provider_revoked')
    monkeypatch.setattr(search_configuration, 'check_search_send', check)
    monkeypatch.setattr(HttpClient, 'request', lambda *a, **kw: pytest.fail('revoked send'))
    with pytest.raises(SearchProviderError) as caught:
        DdgsSearchProvider().search(WebSearchRequest('query', language='en'))
    assert caught.value.error_type == 'permission_revoked'
    assert len(seen) >= 2 and all(seen)


def test_readonly_post_declaration_and_redirect_never_replay_body(monkeypatch):
    from generic_web import url_policy as policy
    endpoint='https://example.org/search'
    monkeypatch.setattr(policy,'public_addresses',lambda url,**kw:(__import__('urllib.parse',fromlist=['urlsplit']).urlsplit(url),'example.org',['93.184.216.34']))
    monkeypatch.setattr('runtime.proxy_config.get_httpx_proxy',lambda:None)
    actual=httpx.Client
    sent=[]
    status=[307]
    def respond(request):
        sent.append((request.method,request.content))
        return httpx.Response(status[0],headers={'location':'/result'}) if len(sent)==1 else httpx.Response(200,text='result')
    monkeypatch.setattr(policy.httpx,'Client',lambda **kw:actual(transport=httpx.MockTransport(respond),**kw))
    declaration=policy.ReadOnlyPost(endpoint,frozenset({'q'}),redirect_to_get=True)
    with pytest.raises(policy.URLPolicyError,match='declaration_required'):
        policy.guarded_fetch(endpoint,form={'q':'test'})
    with pytest.raises(policy.URLPolicyError,match='mismatch'):
        policy.guarded_fetch(endpoint,readonly_post=declaration,form={'delete':'1'})
    with pytest.raises(policy.URLPolicyError,match='redirect_forbidden'):
        policy.guarded_fetch(endpoint,readonly_post=declaration,form={'q':'test'})
    assert len(sent)==1
    sent.clear();status[0]=303
    response=policy.guarded_fetch(endpoint,readonly_post=declaration,form={'q':'test'})
    assert response.status_code==200 and sent==[('POST',b'q=test'),('GET',b'')]


def test_expired_wave_does_not_send_and_running_work_is_reported_draining(monkeypatch):
    from search_providers.service import _run_wave
    calls=[]
    class Provider:
        name='anysearch'
        def available(self): return True
        def status(self): return {'available':True}
        def search(self,request): calls.append(1); time.sleep(.2); return []
    expired=WebSearchRequest('q',options={'_deadline':time.monotonic()-1})
    result=_run_wave(expired,{'anysearch':Provider()},['anysearch'])
    assert not calls and result[1][0]['type']=='deadline_exceeded'
    def slow(request,provider):
        time.sleep(.25)
        return provider.name, [], None
    monkeypatch.setattr('search_providers.service._search_provider',slow)
    running=replace(expired,options={'_deadline':time.monotonic()+.03})
    result=_run_wave(running,{'anysearch':Provider()},['anysearch'])
    assert result[1][0]['execution_state']=='draining'
    time.sleep(.25)
