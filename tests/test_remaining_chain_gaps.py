"""用户报告的跨链路缺口；无账号、无云端计费。"""
import inspect
import json

import httpx
import pytest

import server
from academic_providers.chinese_open_access import ChineseOpenAccessProvider, OpenAccessPlatformConfig
from academic_providers.models import AcademicSearchRequest


@pytest.mark.parametrize('platform', ['', '微信'])
@pytest.mark.parametrize('required_media', [False, True])
def test_public_web_reaches_real_worker_archive_and_fixed_read(monkeypatch, tmp_path, platform, required_media):
    import time
    from dataclasses import asdict
    from generic_web.models import GenericWebResponse
    import generic_web
    from kr_core import DetailRequest
    from kr_core.evidence_assets import EvidenceAssets
    from runtime.knowledge import Search, Fetch, read
    from runtime.task_service import TaskService
    from runtime.tasks import TaskStore
    from runtime import task_scope
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('KR_EVIDENCE_ROOT', str(tmp_path / 'assets'))
    monkeypatch.setenv('KR_LOG_DIR', str(tmp_path / 'logs'))
    # 只替换网络返回；真实worker、覆盖判断、证据库与回读均保留。
    body = '真实正文 English 中文，包含可检索的唯一内容 stage12-public-article。' * 6
    url = 'https://mp.weixin.qq.com/s/stage12-public-article' if platform == '微信' else 'https://example.org/stage12-article'
    metadata = {}
    if platform == '微信':
        from collectors.platform.wechat import parse_html
        article = parse_html('<h1 id="activity-name">article</h1><div id="js_content"><p>' + body + '</p></div>', url)
        assert article['body_verified']
        body = article['content']
        metadata['wechat'] = article
    monkeypatch.setattr(generic_web, 'collect_url', lambda req: GenericWebResponse(
        url=req.url, content=body, title='article', collector='fixture_network', metadata=metadata))
    monkeypatch.setattr(task_scope, 'attach_detail_task_scope', lambda value, *args: value)
    service = TaskService(TaskStore(str(tmp_path / 'state/tasks.sqlite3')))
    request = DetailRequest(url=url, platform=platform,
        options={'include_transcript': required_media, 'include_ocr': False, 'include_comments': False})
    try:
        task = service.submit('content_detail', asdict(request))
        end = time.monotonic() + 10
        while time.monotonic() < end:
            result = service.result(task['task_id'])
            if result['status'] not in {'queued', 'running'}: break
            time.sleep(.02)
        # 微信已解析完整媒体清单，纯文字文章明确“不适用”；普通网页
        # 尚未证明没有音视频，不能用空字符串冒充转写成功。
        missing_media = required_media and platform != '微信'
        assert result['status'] == ('failed' if missing_media else 'completed'), result
        assert result['result']['content'] == body
        assert result['result']['content_coverage']['request_satisfied'] is not missing_media
        if platform == '微信' and required_media:
            assert result['result']['transcript_result']['status'] == 'not_applicable'
            assert result['result']['transcript_result']['reason'] == 'no_observed_audio_video'
        assets = EvidenceAssets(tmp_path / 'assets', read_only=True)
        found = read(Search(action='search', query='stage12-public-article'), assets)
        assert found['items']
        version = found['items'][0]['asset']['version_id']
        assert body in read(Fetch(action='fetch', version_id=version), assets)['text']
    finally:
        service.stop_maintenance()


def test_recruitment_exposes_explicit_research_scope():
    assert 'research_task_id' in inspect.signature(server.search_recruitment).parameters


@pytest.mark.parametrize('platform', ['boss', 'liepin', 'maimai', 'v2ex', 'zhilian'])
@pytest.mark.parametrize('outcome', ['success', 'failed', 'manual'])
def test_all_recruitment_branches_record_research_receipt(platform, outcome, monkeypatch, tmp_path):
    from runtime.research_ledger import open_task, read_task
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path/'state'))
    monkeypatch.setenv('KR_EVIDENCE_ROOT', str(tmp_path/'evidence'))
    result = {'items': [{'title': '真实职位', 'url': 'https://jobs.zhaopin.com/CC123456789J123456789.htm'}], 'total': 1}
    if outcome != 'success':
        result = {'items': [], 'total': 0, 'error': {'failure_type': 'login_required' if outcome == 'manual' else 'fixture_failure',
                                                   'manual_action_required': outcome == 'manual'}}
    monkeypatch.setattr(server, '_with_recruitment_runtime_guards', lambda *a, **kw: dict(result))
    monkeypatch.setattr(server, '_promote_manual_interaction_result', lambda result, **kw: result)
    task = open_task(objective='招聘测试', budget='fast', considered=[])
    response = server.search_recruitment(platform=platform, keyword='开发', research_task_id=task['research_task_id'])
    assert response['research_receipt']['research_task_id'] == task['research_task_id']
    receipts = read_task(task_id=task['research_task_id'])['tool_receipts']
    assert len(receipts) == 1 and receipts[0]['tool'] == 'search_recruitment'


def test_zhilian_fallback_rejects_home_and_foreign_sites(monkeypatch):
    class Response:
        def to_mcp_dict(self):
            return {'items': [{'title': '北京招聘', 'url': url} for url in (
                'https://www.zhaopin.com/', 'https://linkedin.com/jobs/123456',
                'https://jobs.zhaopin.com/CC123456789J00123456789.htm') ]}
    monkeypatch.setattr(server, 'search_web', lambda request: Response())
    result = server._recruitment_open_web_fallback_after_route_failure(
        platform='zhilian', keyword='开发', city='', limit=5,
        direct_result={'error': {'failure_type': 'schema_changed'}})
    assert [item['url'] for item in result['items']] == ['https://jobs.zhaopin.com/CC123456789J00123456789.htm']




def test_wechat_states_codex_relevance_boundary(monkeypatch):
    monkeypatch.setattr('use_cases.web_search.execute', lambda *a, **kw: {'items': []})
    result = server.search_wechat_articles(query='芯片研究', source='web')
    assert result['metadata']['relevance_judgement_by'] == 'Codex'
    assert '基础过滤' in result['metadata']['relevance_boundary']


@pytest.mark.parametrize('body,expected', [
    ('<div id="app"></div><script src="/bundle.js"></script>', 'search_response_unparsed'),
    ('<html>没有找到相关结果</html>', 'no_match'),
])
def test_academic_empty_carries_reason(monkeypatch, body, expected):
    class Provider(ChineseOpenAccessProvider):
        name = 'fixture'
        config = OpenAccessPlatformConfig(name='fixture', display_name='fixture', homepage='https://papers.example/')
        def search_urls(self, request):
            return ['https://papers.example/search?q=test']
    monkeypatch.setattr('runtime.proxy_config.get_httpx_proxy', lambda: None)
    original = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body, request=request))
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original(transport=transport, **kw))
    result = Provider().search(AcademicSearchRequest(query='test'))
    assert result.diagnostics[0]['type'] == expected


def test_xhs_ocr_uses_cloud_ocr_role_and_shared_client(monkeypatch):
    import understanding.image as image
    from runtime import cloud_ocr
    from runtime.model_roles import OCR_MODEL_REF
    assert image.configured_models('image') == (OCR_MODEL_REF,)
    monkeypatch.setattr(cloud_ocr, 'recognize_image', lambda *a, **kw: ('真实图片文字', {}))
    monkeypatch.setattr(cloud_ocr, 'normalize_image_bytes', lambda content: content)
    text, model = image.call_multimodal_models(models=(OCR_MODEL_REF,), images_base64=['aW1hZ2U='], system_prompt='', user_text='')
    assert json.loads(text)['text'] == '真实图片文字'
    assert model == OCR_MODEL_REF




@pytest.mark.parametrize('url', [
    'https://www.youtube.com/watch?v=fixture', 'https://www.zhihu.com/question/123/answer/456',
    'https://www.xiaohongshu.com/explore/0123456789abcdef01234567', 'https://www.bilibili.com/video/BV1FQrEBdEQc',
    'https://mp.weixin.qq.com/s/fixture', 'https://example.com/article',
])
def test_all_enhanced_detail_entries_preserve_research_scope(monkeypatch, url):
    import runtime.task_service as module
    captured = {}
    class Service:
        def submit(self, operation, payload, **kw):
            captured.update(operation=operation, payload=payload, **kw)
            return {'task_id': 'durable-fixture', 'status': 'queued'}
        def result(self, task_id):
            return {'status': 'queued'}
    monkeypatch.setattr(module, 'TaskService', Service)
    result = server.get_content_detail(url=url, auto_multimodal=True, research_task_id='scope-fixture')
    assert result['task_id'] == 'durable-fixture'
    assert captured['operation'] == 'content_detail'
    assert captured['scope']['research_task_id'] == 'scope-fixture'


def test_aistudio_shell_is_not_a_successful_empty_article():
    from generic_web.collector import collect_rendered_html
    from generic_web.models import GenericWebRequest
    result = collect_rendered_html(GenericWebRequest(url='https://aistudio.baidu.com/paddleocr'),
        html='<title>PaddleOCR</title><div id="app"></div><script src="/bundle.js"></script>',
        render_metadata={'html_chars': 90})
    assert result.error['type'] == 'rendered_content_unavailable'
    assert result.metadata['render']['html_chars'] == 90


@pytest.mark.parametrize('body,expected', [
    ('<title>系统正在维护中</title>', 'provider_maintenance'),
    ('<title>404</title>', 'search_route_unavailable'),
])
def test_academic_http200_error_pages_are_explicit(monkeypatch, body, expected):
    class Provider(ChineseOpenAccessProvider):
        name = 'fixture'
        config = OpenAccessPlatformConfig(name='fixture', display_name='fixture', homepage='https://papers.example/')
    monkeypatch.setattr('runtime.proxy_config.get_httpx_proxy', lambda: None)
    original = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body, request=request))
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original(transport=transport, **kw))
    assert Provider().search(AcademicSearchRequest(query='test')).diagnostics[0]['type'] == expected


def test_disabled_and_wrong_query_are_not_empty_success():
    from academic_providers.unpaywall import UnpaywallProvider
    request = AcademicSearchRequest(query='人工智能')
    assert UnpaywallProvider().search(request).diagnostics[0]['type'] == 'invalid_query'




@pytest.mark.parametrize('data,expected', [({}, 'parse_error'), ({'content': []}, 'no_match')])
def test_pubscholar_missing_schema_is_different_from_no_match(monkeypatch, data, expected):
    from academic_providers.pubscholar import PubScholarProvider
    monkeypatch.setattr(PubScholarProvider, '_search_with_httpx', lambda *a, **kw: data)
    assert PubScholarProvider().search(AcademicSearchRequest(query='人工智能')).diagnostics[0]['type'] == expected


def test_retired_basic_text_model_cannot_reenter(monkeypatch):
    from media_policy import MediaModelPolicy
    monkeypatch.setenv('KR_BASIC_TEXT_MODELS', 'old:paid-model')
    assert MediaModelPolicy.from_env().basic_text_models == ()


def test_long_image_signature_is_only_shortened_in_preview():
    from understanding.image import _select_xhs_ocr_images
    url = 'https://sns-webpic-qc.xhscdn.com/' + 'path/' * 60 + '?sign=fixture'
    selected, metadata = _select_xhs_ocr_images([url], 1)
    assert selected == [url]
    assert len(metadata['candidates_preview'][0]['url']) == 240


@pytest.mark.parametrize('platform,url', [
    ('boss', 'https://www.zhipin.com/'), ('liepin', 'https://www.liepin.com/'),
    ('v2ex', 'https://v2ex.com/'), ('v2ex', 'https://evil.test/t/123'),
    ('boss', 'https://zhipin.com.evil.test/job_detail/abc.html'),
])
def test_all_recruitment_fallbacks_exclude_navigation_and_foreign_hosts(platform, url):
    assert not server._recruitment_candidate_url(platform, url)


def test_image_plan_declares_actual_ocr_provider():
    from media_bundle import MediaBundle, MediaRef, MediaKind, MediaLocationType
    from media_router import plan_media_action
    bundle = MediaBundle.build(platform='小红书', source_url='https://example.com/note',
        image_refs=[MediaRef(kind=MediaKind.IMAGE, uri='https://example.com/image.jpg', location_type=MediaLocationType.URL)])
    plan = plan_media_action(bundle)
    assert plan.provider == 'siliconflow'
    assert plan.model == 'siliconflow:PaddlePaddle/PaddleOCR-VL-1.5'
