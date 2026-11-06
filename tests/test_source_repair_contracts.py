"""跨越本轮实际故障边界；所有网络由明确夹具替代。"""
import asyncio
import httpx
import pytest

from academic_providers.models import AcademicSearchRequest
from collectors.platform.zhilian import _parse_zhilian_detail_html


def test_zhilian_security_page_cannot_be_job_detail():
    result = _parse_zhilian_detail_html('<title>Security Verification</title><body>确认您是真人</body>',
        'https://jobs.zhaopin.com/CC123456789J123456789.htm')
    assert result['status'] == 'needs_interaction' and not result['content']


def test_zhilian_job_description_is_separate_from_navigation():
    jd = '岗位职责：负责工程系统开发与测试。任职要求：熟悉 Python。' * 8
    result = _parse_zhilian_detail_html('<nav>扫码登录</nav><h1>工程师</h1><div class="job-describe">'+jd+'</div>',
        'https://jobs.zhaopin.com/CC123456789J123456789.htm')
    assert result['status'] == 'ok' and result['content'] == jd


def test_maimai_share_keeps_discussion_without_calling_it_a_job(monkeypatch):
    import generic_web
    from generic_web.models import GenericWebResponse
    from collectors.platform.maimai import public_discussion_detail
    url='https://maimai.cn/article/detail?fid=123'
    monkeypatch.setattr(generic_web,'collect_url',lambda req: GenericWebResponse(url=req.url, title='社区讨论',content='讨论正文。'*30))
    result=public_discussion_detail(url)
    assert result['body_verified'] and not result['job_verified'] and not result['comments_collected']
    monkeypatch.setattr(generic_web,'collect_url',lambda req: GenericWebResponse(url=req.url,final_url='https://maimai.cn/platform/login',content='验证码登录'))
    assert public_discussion_detail(url)['failure_type']=='login_required'
    assert public_discussion_detail('https://maimai.cn.evil.example/article/detail?fid=123')['failure_type']=='unsupported_url'


@pytest.mark.parametrize('transport',['stdio','sse','streamable-http'])
def test_all_transports_restore_browser_lifecycle(monkeypatch,transport):
    import server
    from runtime import desktop_notification
    calls=[]
    monkeypatch.setattr(server,'MCP_TRANSPORT',transport)
    monkeypatch.setattr(server,'_bootstrap_persistent_runtime',lambda:calls.append('restore'))
    from runtime.execution_owner import OwnerClient
    def connect(self):
        calls.append('connect_owner')
        return {'status':'ready'}
    monkeypatch.setattr(OwnerClient,'start',connect)
    monkeypatch.setattr(server,'record_server_started',lambda **kw:None)
    monkeypatch.setattr(server,'record_server_stopped',lambda **kw:None)
    class Stop:
        def set(self):pass
    monkeypatch.setattr(desktop_notification,'start_notification_outbox_worker',lambda:Stop())
    async def scenario():
        async with server._mcp_lifespan(None):
            assert calls==['connect_owner']
            assert server.mcp._kr_forward_owner
        assert not server.mcp._kr_forward_owner
    asyncio.run(scenario())


def test_semantic_scholar_does_not_retry_before_long_retry_after(monkeypatch, tmp_path):
    from academic_providers.semanticscholar import SemanticScholarProvider,SemanticScholarRateLimitError
    import academic_providers.semanticscholar as module
    monkeypatch.setattr('runtime.proxy_config.get_httpx_proxy', lambda: None)
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path))
    calls=[]
    def respond(req):
        calls.append(req)
        return httpx.Response(429,headers={'Retry-After':'120'},request=req)
    original=httpx.Client
    monkeypatch.setattr(httpx,'Client',lambda **kw:original(transport=httpx.MockTransport(respond),**kw))
    monkeypatch.setattr(module.time,'sleep',lambda delay:pytest.fail('must return a cooldown, not sleep or retry'))
    with pytest.raises(SemanticScholarRateLimitError,match='120'):
        SemanticScholarProvider().search(AcademicSearchRequest(query='education'))
    assert len(calls)==1


@pytest.mark.parametrize('module,cls,payload,expected',[
    ('nssd','NssdProvider',{'data':{'rows':[{'title':'真实标题','encryptedUrl':'public-record','remark':'摘要'}]}},1),
    ('toaj','ToajProvider',{'obj':[{'vitId':'123','title1':'真实标题','summaryCn':'摘要','authorNameList':[]}]},1),
])
def test_verified_public_api_schemas(monkeypatch,module,cls,payload,expected):
    import importlib
    monkeypatch.setattr('runtime.proxy_config.get_httpx_proxy',lambda:None)
    original=httpx.Client
    monkeypatch.setattr(httpx,'Client',lambda **kw:original(transport=httpx.MockTransport(lambda req:httpx.Response(200,json=payload,request=req)),**kw))
    result=getattr(importlib.import_module('academic_providers.'+module),cls)().search(AcademicSearchRequest(query='研究'))
    assert len(result)==expected
    assert result.diagnostics==([] if expected else [{'type':'no_match','retryable':False,'message':'已执行主题检索，接口返回空记录列表'}])


def test_pubscholar_does_not_discard_search_record_without_direct_pdf(monkeypatch):
    from academic_providers.pubscholar import PubScholarProvider
    monkeypatch.setattr(PubScholarProvider,'_search_with_httpx',lambda *a,**kw:{'content':[{'id':'article123','title':'真实文章标题'}]})
    result=PubScholarProvider().search(AcademicSearchRequest(query='文章'))
    assert len(result)==1 and not result[0].raw['pdf_url']
    assert result.diagnostics[0]['type']=='fulltext_unavailable'


def test_retired_models_absent_from_runtime_and_prompt():
    from pathlib import Path
    from runtime import model_roles
    assert not hasattr(model_roles,'RETIRED_MODEL_CONTROLS')
    text=(Path(__file__).resolve().parents[1]/'config/codex-product/plugin/knowledgeradar-research/skills/research/SKILL.md').read_text('utf-8')
    assert '1.7B' not in text
    # 短Skill不重复模型配置；角色事实仍由运行合同给出。
    assert 'qwen3-asr/0.6b' in model_roles.ASR_MODEL_REF.lower()


@pytest.mark.parametrize('payload,expected,reason', [
    ({'success': True, 'data': {'records': [
        {'documentType': 'ARTICLE', 'doi': '10.1234/example', 'pubYear': '2026',
         'entries': [{'language': 'zh_CN', 'title': '<em>真实</em>文章',
                      'authors': [{'author': '作者'}], 'resource': {'resource': '订阅获取'}}]},
        {'documentType': 'BOOK', 'bookId': 'book1', 'bookPublisherAbb': 'CSPM', 'titleZh': '书籍'}]}}, 2, None),
    ({'success': True, 'data': {'records': []}}, 0, 'no_match'),
    ({'success': False, 'code': 20001}, 0, 'search_response_unparsed'),
])
def test_sciengine_current_public_api_preserves_access_boundary(monkeypatch, payload, expected, reason):
    from academic_providers.sciengine import SciEngineProvider
    monkeypatch.setattr('runtime.proxy_config.get_httpx_proxy', lambda: None)
    original = httpx.Client
    requests = []
    def respond(req):
        requests.append(req)
        return httpx.Response(200, json=payload, request=req)
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    result = SciEngineProvider().search(AcademicSearchRequest(query='人工智能'))
    assert len(result) == expected
    assert requests[0].url.path.endswith('/open/SciSearch/searchNew')
    assert requests[0].method == 'POST' and 'multipart/form-data' in requests[0].headers['content-type']
    assert '人工智能'.encode() in requests[0].content
    if reason:
        assert result.diagnostics[0]['type'] == reason
    else:
        assert result[0].title == '真实文章' and result[0].oa_status == ''
        assert all(not row.raw['fulltext_verified'] for row in result)
