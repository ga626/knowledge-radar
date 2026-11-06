"""知乎迁移的身份、失败、阶段恢复及公共存证合同。"""
from dataclasses import asdict
import hashlib
import pytest
from conftest import patch_native
from collectors.platform import zhihu as z
from kr_core import DetailRequest, SearchRequest

URL = 'https://www.zhihu.com/question/123/answer/1937205528846655537'


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    # Collector contract fixture keeps both transports behind the same fake boundary.
    monkeypatch.setattr(z, '_reader_comments', lambda **payload: z._site().invoke('comments', **payload))
    monkeypatch.setenv('KR_DATA_ROOT', str(tmp_path / 'data'))
    monkeypatch.setenv('KR_LOG_DIR', str(tmp_path / 'logs'))
    monkeypatch.setenv('KR_EVIDENCE_ROOT', str(tmp_path / 'evidence-assets'))
    for key, name in [('KR_STATE_DIR','state'),('KR_TASK_DB_PATH','tasks.sqlite3'),('KR_RUNTIME_LEASE_DB_PATH','leases.sqlite3'),('KR_EVIDENCE_DIR','evidence')]:
        monkeypatch.setenv(key, str(tmp_path / name))
    monkeypatch.setenv('KR_BROWSER_LINK_STATE_FILE', str(tmp_path / 'absent-host.json'))
    monkeypatch.delenv('KR_DURABLE_TASK_ID', raising=False)


def body(url=URL, html='<p>准确的指定回答。</p>', **extra):
    return {'ok':True,'data':{'adapter_version':z.ADAPTER_VERSION,'target':asdict(z.parse_target(url)),
        'url':url,'title':'目标问题','author':'作者','body_html':html,'body_sha256':hashlib.sha256(html.encode()).hexdigest(),
        'body_complete':True,'body_source':'matched_initial_data','parts':1,'snapshot_id':'snap',**extra}}


@pytest.mark.parametrize('raw,kind,identifier,question', [
    (URL+'/?utm_source=x','answer','1937205528846655537','123'),
    ('https://www.zhihu.com/answer/1937205528846655537/','answer','1937205528846655537',''),
    ('https://zhuanlan.zhihu.com/p/9007199254740993123/','article','9007199254740993123',''),
    ('https://zhihu.com/question/123/','question','123','123'),
])
def test_target_is_string_safe(raw,kind,identifier,question):
    t=z.parse_target(raw)
    assert (t.kind,t.identifier,t.question_id)==(kind,identifier,question)
    assert not t.url.endswith('/') and '?' not in t.url


@pytest.mark.parametrize('url',['https://evil.zhihu.com/question/1','https://www.zhihu.com.evil.org/question/1',
    'https://u:p@www.zhihu.com/question/1','https://www.zhihu.com:8888/question/1','file:///question/1',
    'https://zhuanlan.zhihu.com/question/1','https://www.zhihu.com/question/no'])
def test_rejects_untrusted_targets(url):
    with pytest.raises(ValueError): z.parse_target(url)


def test_exact_id_rejects_other_answer_and_parent(monkeypatch):
    for url in ['https://www.zhihu.com/question/123/answer/5','https://www.zhihu.com/question/999/answer/1937205528846655537']:
        patch_native(monkeypatch, lambda **kw: body(url))
        assert z.collect_detail(DetailRequest(URL))['failure_type']=='page_identity_changed'


def test_short_answer_accepts_canonical_redirect_and_short_text(monkeypatch):
    patch_native(monkeypatch,lambda **kw:body())
    result=z.collect_detail(DetailRequest('https://www.zhihu.com/answer/1937205528846655537/'))
    assert result['url']==URL and result['content']=='准确的指定回答。'
    assert result['body_verified'] and result['body_complete']


@pytest.mark.parametrize('kind,manual',[('access_forbidden',False),('rate_limited',False),
    ('legal_restriction',False),('not_found',False),('login_required',True),('anti_bot_verification',True)])
def test_typed_failure_never_becomes_empty_or_generic_login(monkeypatch,kind,manual):
    calls=[]
    patch_native(monkeypatch, lambda **kw:calls.append(kw) or {'ok':True,'data':{'adapter_version':z.ADAPTER_VERSION,'failure_type':kind,'url':URL}})
    result=z.legacy_search_zhihu('术语')
    assert result['failure_type']==kind and result['manual_action_required']==manual
    assert len(calls)==1 and not result['items']
    from platform_adapters.common import response_from_legacy
    response=response_from_legacy('知乎',result).to_mcp_dict()
    assert response['error']['metadata']['failure_type']==kind
    if not manual: assert not response['error'].get('login_required')


def test_failure_preserves_fixed_structural_diagnostics(monkeypatch):
    hints={'scope_found':False,'expand_clicked':True,'shapes':[]}
    patch_native(monkeypatch,lambda **kw:{'ok':True,'data':{'adapter_version':z.ADAPTER_VERSION,
        'failure_type':'comments_scope_unavailable','url':URL,'comment_diagnostics':hints,'unrelated':'not-forwarded'}})
    result=z._invoke('comments',url=URL)
    assert result['metadata']['comment_diagnostics']==hints
    assert 'unrelated' not in result['metadata']


def test_empty_results_require_positive_evidence(monkeypatch):
    patch_native(monkeypatch,lambda **kw:{'ok':True,'data':{'adapter_version':z.ADAPTER_VERSION,'items':[]}})
    assert z.legacy_search_zhihu('术语')['failure_type']=='page_structure_changed'
    patch_native(monkeypatch,lambda **kw:{'ok':True,'data':{'adapter_version':z.ADAPTER_VERSION,'items':[],'empty_confirmed':True}})
    assert not z.legacy_search_zhihu('术语').get('error')


def test_long_snapshot_is_reassembled_and_hash_verified(monkeypatch):
    html='<pre>代码\n  缩进</pre><p>'+'资料😀'*70000+'</p>'
    parts=[html[:90000],html[90000:180000],html[180000:]]
    calls=[]
    def native(**kw):
        calls.append(kw)
        response=body(html=html,parts=3)
        response['data']['body_html']=parts[kw.get('read_part',0)]
        response['data']['part']=kw.get('read_part',0)
        return response
    patch_native(monkeypatch,native)
    result=z.collect_detail(DetailRequest(URL))
    assert result['content'].count('资料😀')==70000 and '  缩进' in result['content']
    assert len(calls)==3 and [r['read_part'] for r in calls[1:]]==[1,2]
    assert all(r['url']==URL for r in calls)


def test_mixed_or_expired_snapshot_is_rejected(monkeypatch):
    def native(**kw):
        response=body(parts=2)
        if 'read_part' in kw: response['data'].update(part=1,snapshot_id='different')
        return response
    patch_native(monkeypatch,native)
    assert z.collect_detail(DetailRequest(URL))['failure_type']=='content_snapshot_changed'


def test_ocr_retry_reuses_capture_and_releases_browser(monkeypatch,tmp_path):
    from detail_strategies.zhihu import collect,enhance_detail
    from runtime import media_acquisition,media_execution
    from runtime.leases import get_runtime_lease_coordinator
    calls=[]
    image={'type':'image','url':'https://pic1.zhimg.com/fixture.png','locator':{'kind':'body_image','index':0}}
    patch_native(monkeypatch,lambda **kw:calls.append(kw['operation']) or body(media_manifest=[image]))
    request=DetailRequest(URL,platform='知乎',options={'include_ocr':True,'_detail_stage':'collection'})
    captured=collect(request)
    assert captured['_enhancement_pending'] and calls==['detail','release']
    path=tmp_path/'image.png';path.write_bytes(b'fixture')
    def acquire(*a,**kw):
        identity=z.runtime_identity()
        assert not get_runtime_lease_coordinator().resource_leases('browser_site',identity['profile']+':zhihu')
        return {'type':'image','status':'acquired','path':str(path),'source_url':image['url'],'sha256':'fixture'}
    monkeypatch.setattr(media_acquisition,'acquire_http_asset',acquire)
    processing=[]
    def process(asset,component,**kw):
        processing.append(asset)
        return {'status':'failed' if len(processing)==1 else 'ok','text':'' if len(processing)==1 else '图内证据','input_sha256':'fixture'}
    monkeypatch.setattr(media_execution,'process_local_asset',process)
    first=enhance_detail(request,captured)
    assert first['status']=='partial' and calls==['detail','release']
    second=enhance_detail(request,captured)
    assert second['ocr_text']=='图内证据' and second['ocr']['results'][0]['locator']==image['locator']
    assert second['enhancement_status']=='ok' and calls==['detail','release']


def test_unsupported_image_does_not_fetch_and_reports_gap(monkeypatch):
    from detail_strategies.zhihu import collect,enhance_detail
    from runtime import media_acquisition
    patch_native(monkeypatch,lambda **kw:body(media_manifest=[{'type':'image','url':'https://evil.org/p.png'}]))
    request=DetailRequest(URL,options={'include_ocr':True,'_detail_stage':'collection'})
    captured=collect(request)
    monkeypatch.setattr(media_acquisition,'acquire_http_asset',lambda *a,**kw:pytest.fail('unapproved source'))
    result=enhance_detail(request,captured)
    assert result['status']=='partial' and result['media_gaps'][0]['reason']=='image_source_not_supported'


def test_comments_keep_parent_and_failure_does_not_discard_body(monkeypatch):
    patch_native(monkeypatch,lambda operation,**kw:body() if operation=='detail' else {'ok':True,'data':{'adapter_version':z.ADAPTER_VERSION,'url':URL,'comments':[
        {'id':'1','parent_id':'','content':'原评论'},{'id':'2','parent_id':'1','content':'回复'}],'limit':20}})
    request=DetailRequest(URL,options={'include_comments':True})
    result=z.collect_detail(request)
    assert result['comments'][1]['parent_id']=='1' and not result['comments_complete']
    patch_native(monkeypatch,lambda operation,**kw:body() if operation=='detail' else {'ok':False,'code':'access_forbidden'})
    result=z.collect_detail(request)
    assert result['content'] and result['status']=='partial' and result['comments_status']=='access_forbidden'


def test_bounded_api_comments_satisfy_request_without_claiming_exhaustion(monkeypatch):
    patch_native(monkeypatch,lambda operation,**kw:body() if operation=='detail' else {'ok':True,'data':{
        'adapter_version':z.ADAPTER_VERSION,'url':URL,'comments':[{'id':'1','parent_id':'','root_id':'1','content':'原评论'}],
        'limit':1,'request_satisfied':True,'has_more':True,'exhausted':False,'comment_source':'same_origin_comment_v5',
        'coverage':{'scope':'score_roots_with_bounded_replies','limit':1,'reply_limit_per_root':3}}})
    result=z.collect_detail(DetailRequest(URL,options={'include_comments':True,'comment_limit':1}))
    assert result['status']=='ok' and result['comments_complete']
    assert result['comments_coverage']['has_more'] and not result['comments_exhausted']
    from kr_core.evidence_coverage import content_coverage
    assert content_coverage(result,{'include_comments':True})['request_satisfied']


def test_explicit_incomplete_comment_window_remains_a_required_gap():
    from kr_core.evidence_coverage import content_coverage
    result=content_coverage({'content':'正文','comments':[{'content':'已有一条'}],'comments_complete':False}, {'include_comments':True})
    assert 'comments' in result['required_gaps']


def test_ocr_text_roundtrip_keeps_original_image_index_after_first_download_fails(monkeypatch,tmp_path):
    from detail_strategies.zhihu import collect,enhance_detail
    from runtime import media_acquisition,media_execution
    from kr_core.evidence_store import EvidenceStore
    images=[{'type':'image','url':f'https://pic1.zhimg.com/{i}.png','locator':{'kind':'body_image','index':i}} for i in range(2)]
    patch_native(monkeypatch,lambda **kw:body(media_manifest=images))
    request=DetailRequest(URL,options={'include_ocr':True,'_detail_stage':'collection'})
    captured=collect(request)
    def download(kind,url,*args,**kwargs):
        if url==images[0]['url']: raise ValueError('first_download_failed')
        return {'path':str(tmp_path/'image.png'),'source_url':url,'sha256':'input-sha'}
    monkeypatch.setattr(media_acquisition,'acquire_http_asset',download)
    monkeypatch.setattr(media_execution,'process_local_asset',lambda *a,**kw:{'status':'acquired','text':'图中文字😀',
        'input_sha256':'input-sha','model':'ocr-fixture','provider':'siliconflow','document_blocks':[{'page':1,'text':'图中文字😀'}]})
    result=enhance_detail(request,captured)
    assert result['status']=='partial' and result['ocr_text']=='图中文字😀'
    loc=result['ocr_text_locators'][0]
    assert loc['image_index']==1 and result['ocr_text'][loc['start']:loc['end']]=='图中文字😀'
    store=EvidenceStore(str(tmp_path/'ocr.jsonl'));store.record_detail(platform='知乎',url=URL,response=result)
    asset=next(r for r in result['evidence_assets'] if r['kind']=='ocr_text')
    read=store.assets.fetch(asset['version_id'],max_chars=1000)
    assert read['text']=='图中文字😀' and read['locators'][0]['image_index']==1
    assert read['asset']['provenance']['ocr_images'][0]['input_sha256']=='input-sha'
    assert read['asset']['provenance']['ocr_images'][0]['source_url']==images[1]['url']


def test_cancellation_propagates_and_releases_lease(monkeypatch):
    from runtime.execution_context import ExecutionStopped
    from platform_adapters.zhihu_adapter import ZhihuAdapter
    from runtime.leases import get_runtime_lease_coordinator
    patch_native(monkeypatch,lambda **kw:(_ for _ in ()).throw(ExecutionStopped('cancelled')))
    with pytest.raises(ExecutionStopped): ZhihuAdapter().search(SearchRequest('术语'))
    identity=z.runtime_identity()
    assert not get_runtime_lease_coordinator().resource_leases('browser_site',identity['profile']+':zhihu')


def test_question_directory_and_original_body_roundtrip(monkeypatch,tmp_path):
    from detail_strategies.zhihu import collect
    from kr_core.evidence_store import EvidenceStore
    question='https://www.zhihu.com/question/123'
    patch_native(monkeypatch,lambda **kw:body(question, '<p>问题说明</p>',answer_directory=[{'title':'指定回答','url':URL,'identifier':'1937205528846655537'}],directory_coverage='visible_answers_bounded_not_all_answers'))
    result=collect(DetailRequest(question,platform='知乎'))
    assert URL in result['content'] and result['directory_coverage'].startswith('visible_')
    store=EvidenceStore(str(tmp_path/'evidence.jsonl'));store.record_detail(platform='知乎',url=question,response=result)
    refs={r['kind']:r for r in result['evidence_assets']}
    assert 'answer_directory' in refs
    assert '问题说明' in store.assets.fetch(refs['content']['version_id'],max_chars=10000)['text']
    assert '1937205528846655537' in store.assets.fetch(refs['answer_directory']['version_id'],max_chars=10000)['text']


def test_zhihu_no_longer_enters_managed_chrome_or_closes_process(monkeypatch):
    from runtime import chrome_manager as chrome
    assert 'zhihu' not in chrome.managed_browser_platforms()
    assert not chrome._ensure_chrome_debugging('zhihu') and not chrome._managed_chrome_profile_dir('zhihu')
    assert chrome._chrome_debug_port('zhihu')==''
    calls=[]
    monkeypatch.setattr(z,'request_login',lambda:calls.append('login') or {'status':'waiting_for_user'})
    monkeypatch.setattr(z,'cancel_login',lambda:calls.append('cancel') or {'status':'cancelled'})
    monkeypatch.setattr(chrome,'_close_chrome_via_cdp',lambda *a:pytest.fail('borrowed browser closed'))
    chrome.request_browser_interaction('zhihu','login_required');chrome.cancel_browser_interaction('zhihu')
    assert calls==['login','cancel']


def test_explicit_partial_body_is_a_required_gap():
    from kr_core.evidence_coverage import content_coverage
    coverage=content_coverage({'content':'可见片段','body_complete':False,'body_gap':'visible_body_only'})
    assert not coverage['request_satisfied'] and coverage['required_gaps']==['content']


def test_ocr_of_verified_snapshot_survives_browser_disconnect(monkeypatch):
    from detail_strategies.zhihu import collect, enhance_detail
    patch_native(monkeypatch, lambda **kw: body())
    request = DetailRequest(URL, options={'include_ocr': True, '_detail_stage': 'collection'})
    captured = collect(request)
    monkeypatch.setattr(z, 'runtime_identity', lambda: {'generation': 'disconnected'})
    monkeypatch.setattr(z, 'collect_detail', lambda *a: pytest.fail('must not recollect'))
    result = enhance_detail(request, captured)
    assert result['enhancement_status'] == 'ok' and result['content'] == captured['content']
    assert result['collection_identity'] == captured['collection_identity']


def test_question_directory_rejects_foreign_entities(monkeypatch):
    question = 'https://www.zhihu.com/question/123'
    patch_native(monkeypatch, lambda **kw: body(question, answer_directory=[{'url': 'https://www.zhihu.com/question/999/answer/5'}]))
    assert z.collect_detail(DetailRequest(question))['failure_type'] == 'page_identity_changed'


def test_invalid_comments_keep_verified_body(monkeypatch):
    patch_native(monkeypatch, lambda operation, **kw: body() if operation == 'detail' else
                 {'ok': True, 'data': {'adapter_version': z.ADAPTER_VERSION, 'url': URL, 'comments': ['invalid']}})
    result = z.collect_detail(DetailRequest(URL, options={'include_comments': True}))
    assert result['content'] and result['status'] == 'partial' and result['comments_status'] == 'invalid_adapter_payload'


def test_invalid_bounds_fail_before_navigation(monkeypatch):
    patch_native(monkeypatch, lambda **kw: pytest.fail('invalid input must not navigate'))
    assert z.collect_detail(DetailRequest(URL, options={'comment_limit': 'wrong'}))['failure_type'] == 'invalid_request'


def test_search_to_worker_evidence_readback_uses_same_target(monkeypatch, tmp_path):
    from runtime.platform_execution import worker_detail
    from generic_web import url_policy
    from kr_core.evidence_store import EvidenceStore
    patch_native(monkeypatch, lambda operation, **kw:
        {'ok': True, 'data': {'adapter_version': z.ADAPTER_VERSION, 'items': [{'title': '目标问题', 'url': URL}]}}
        if operation == 'search' else body())
    monkeypatch.setattr(url_policy, 'validate_detail_target', lambda *a: None)
    from platform_adapters.zhihu_adapter import ZhihuAdapter
    hits = ZhihuAdapter().search(SearchRequest('问题', platform='知乎')).to_mcp_dict()
    result = worker_detail(DetailRequest(hits['items'][0]['url'], platform='知乎'))
    assert result['content_coverage']['request_satisfied'] and result['target']['identifier'] == '1937205528846655537'
    ref = next(row for row in result['evidence_assets'] if row['kind'] == 'content')
    assert '准确的指定回答' in EvidenceStore().assets.fetch(ref['version_id'], max_chars=10000)['text']
