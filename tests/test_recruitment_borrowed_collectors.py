"""站点边界测试：真实解析器、HTTP协议和借用桥接缝隙。"""
import json
import subprocess
import shutil
import httpx
import pytest
from conftest import patch_native
from runtime import native_site
from collectors.platform import boss as boss, liepin, maimai, v2ex


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('KR_TASK_DB_PATH', str(tmp_path / 'tasks.sqlite3'))
    monkeypatch.setenv('KR_RUNTIME_LEASE_DB_PATH', str(tmp_path / 'leases.sqlite3'))
    monkeypatch.delenv('KR_V2EX_TOKEN', raising=False)


def fake_http(monkeypatch, handler):
    monkeypatch.setattr(v2ex, '_client', lambda: httpx.Client(transport=httpx.MockTransport(handler)))




def test_boss_result_limit_is_not_a_city_mismatch(monkeypatch):
    patch_native(monkeypatch, lambda **kw: {'ok': True, 'data': {'query': 'Python',
        'items': [{'title': str(i), 'city': '杭州'} for i in range(5)] + [{'title': 'other', 'city': '北京'}]}})
    result = boss.search('Python', '杭州', 2)
    assert result['metadata']['city_filter']['dropped'] == 1
    assert result['metadata']['result_limit']['omitted'] == 3


@pytest.mark.parametrize('operation',['search','detail'])
def test_boss_blank_page_is_navigation_failure_not_logout_or_query_mismatch(monkeypatch,operation):
    patch_native(monkeypatch,lambda **kw:{'ok':True,'data':{'url':'about:blank'}})
    monkeypatch.setattr(boss,'request_login',lambda **kw:pytest.fail('no logout evidence'))
    result=boss.search('Python') if operation=='search' else boss.detail('https://www.zhipin.com/job_detail/one.html')
    assert result['failure_type']=='browser_navigation_interrupted'
    assert not result['retryable'] and not result['manual_action_required']


@pytest.mark.parametrize('flag,kind',[('risk','anti_bot_verification'),('login','login_required')])
def test_boss_blank_page_cannot_hide_known_remote_signal(monkeypatch,flag,kind):
    patch_native(monkeypatch,lambda **kw:{'ok':True,'data':{'url':'about:blank',flag:True}})
    assert boss.search('Python')['failure_type']==kind


def test_boss_isolated_detail_preserves_full_signed_url_and_job_identity(monkeypatch):
    url = 'https://www.zhipin.com/job_detail/one.html?securityId=fixture&lid=list-origin'
    calls = []
    def invoke(**kw):
        calls.append(kw)
        return {'ok': True, 'data': {'url': url, 'content': '岗位职责：Python后端开发与接口设计。' * 25}}
    patch_native(monkeypatch, invoke)
    assert boss.detail(url)['status'] == 'ok'
    assert calls[0]['url'] == url and calls[0]['platform'] == 'boss'
    assert 'steps' not in calls[0]
    patch_native(monkeypatch, lambda **kw: {'ok': True, 'data': {
        'url': 'https://www.zhipin.com/web/geek/jobs', 'detail_job_id': 'two', 'content': '岗位职责。' * 30}})
    assert boss.detail(url)['failure_type'] == 'page_identity_changed'


def test_boss_detail_does_not_replay_search_or_click_discarded_cards(monkeypatch):
    calls=[]
    patch_native(monkeypatch,lambda **kw: calls.append(kw) or {
        'ok':True,'data':{'url':'https://www.zhipin.com/job_detail/one.html','content':'岗位职责：Python开发。'*25}})
    boss.detail('https://www.zhipin.com/job_detail/one.html')
    assert len(calls)==1 and calls[0]['operation']=='detail'
    assert calls[0]['url']=='https://www.zhipin.com/job_detail/one.html'
    assert not calls[0].get('steps')


def test_boss_public_cards_not_authentication_and_preview_not_full_jd(monkeypatch):
    assert not boss.authenticated({'items': [{'title': 'public candidate'}], 'content': '岗位职责。' * 80})
    monkeypatch.setattr(boss, 'request_login', lambda: {'status': 'waiting_for_user'})
    patch_native(monkeypatch, lambda **kw: {'ok': True, 'data': {
        'url': 'https://www.zhipin.com/job_detail/one.html', 'detail_login_required': True,
        'content': '登录前预览。' * 50}})
    assert boss.detail('https://www.zhipin.com/job_detail/one.html')['failure_type'] == 'login_required'


def test_boss_login_opens_sign_page_once_not_expired_job_page(monkeypatch):
    calls = []
    patch_native(monkeypatch, lambda **kw: calls.append(kw) or {
        'ok': True, 'data': {'login': True, 'authenticated_ui': False}})
    assert boss.request_login(activate=True)['status'] == 'waiting_for_user'
    assert len(calls) == 1
    assert calls[0]['url'] == 'https://www.zhipin.com/web/user/'
    assert 'expression' not in calls[0]
    assert boss.request_login()['status'] == 'waiting_for_user'
    assert len(calls) == 1


def test_borrowed_attention_does_not_navigate_again_when_auth_unknown(monkeypatch):
    calls = []
    native_site.write_state(boss.state_path(), {'expires_at': native_site.time.time() + 300})
    patch_native(monkeypatch, lambda **kw: calls.append(kw) or {'ok': True, 'data': {}})
    assert boss.request_login(activate=True)['status'] == 'waiting_for_user'
    assert len(calls) == 1 and calls[0]['operation'] == 'attention' and calls[0]['url'] == ''




def test_maimai_explicit_login_verification_uses_account_api(monkeypatch):
    calls=[]
    def invoke(**kw):
        calls.append(kw)
        return {'ok': True, 'data': {'probe': {'http_status': 200, 'data': {'result': 'ok', 'user': {'id': 'fixture'}}}}}
    patch_native(monkeypatch, invoke)
    assert maimai.complete_login()['status'] == 'ok'
    assert calls[0]['operation']=='auth_verify' and calls[0]['platform']=='maimai'
    assert maimai.interaction_status() == 'ready'


def test_liepin_http_success_does_not_borrow_browser(monkeypatch):
    monkeypatch.setattr(liepin, '_static_liepin_detail', lambda url: {'status': 'ok', 'content': '完整职位正文'})
    patch_native(monkeypatch, lambda **kw: pytest.fail('HTTP success must not borrow'))
    assert liepin.detail('https://www.liepin.com/job/123.shtml')['content'] == '完整职位正文'


def test_maimai_detail_comments_keep_body_when_risk_stops(monkeypatch):
    url = 'https://maimai.cn/community/feed-detail/12?efid=e'
    html = '<script>self.__next_f.push(' + json.dumps([1, '0:{"feedData":{"feed":{"id":12,"text":"真实正文"}}}']) + ')</script>'
    calls = []
    def invoke(**kw):
        calls.append(kw)
        return {'ok': True, 'data': {'url': url, 'html': html}} if kw['operation'] == 'detail' else {'ok': False, 'code': 'SECURITY_BLOCK', 'request_executed': True}
    patch_native(monkeypatch, invoke)
    result = maimai.discussion_detail(url, include_comments=True)
    assert result['status'] == 'ok' and result['content'] == '真实正文'
    assert result['comments_status'] == 'anti_bot_verification'
    assert not maimai.admission()['allowed']
    maimai.legacy_search_maimai('招聘')
    assert [c['operation'] for c in calls] == ['detail', 'comments']


def test_maimai_search_uses_business_response_without_login_probe(monkeypatch):
    calls = []
    def invoke(**kw):
        calls.append(kw)
        return {'ok': True, 'data': {'probe': {'http_status': 200, 'data': {'result': 'ok', 'feeds': [
            {'fid': 12, 'efid': 'context', 'text': '招聘讨论'}]}}}}
    patch_native(monkeypatch, invoke)
    result = maimai.legacy_search_maimai('招聘')
    assert result['items'] and len(calls) == 1
    assert calls[0]['operation']=='search' and calls[0]['keyword']=='招聘'
    assert 'expression' not in calls[0]


def test_v2ex_no_match_stops_duplicate_hosts_and_enables_index(monkeypatch):
    calls = []
    def handle(req):
        calls.append(str(req.url))
        return httpx.Response(200, text='<div id="TopicsNode"><div class="cell item"><span class="item_title"><a href="/t/1">北京 Go</a></span></div></div>')
    fake_http(monkeypatch, handle)
    result = v2ex.legacy_search_v2ex('Python')
    assert result['status'] == 'empty' and not result.get('error')
    assert result['metadata']['open_index_eligible'] and len(calls) == 1


def test_v2ex_current_node_cards_without_item_class(monkeypatch):
    # 2026-10-29 实站cell去掉了item，标题和帖ID仍是稳定业务定位点。
    fake_http(monkeypatch, lambda req: httpx.Response(200, text='''<div id="TopicsNode">
      <div class="cell"><table><tr><td><span class="item_title"><a href="/t/123#reply6">远程 Python</a></span>
      <span class="topic_info"><strong><a href="/member/test">test</a></strong></span></td></tr></table></div>
      </div><div class="cell"><span class="item_title"><a href="/t/999">边栏无关帖子</a></span></div>'''))
    result = v2ex.legacy_search_v2ex('', limit=2)
    assert len(result['items']) == 1
    assert result['items'][0]['url'] == 'https://www.v2ex.com/t/123'


@pytest.mark.parametrize('status,kind', [(403,'access_denied'), (404,'not_found'), (429,'rate_limited')])
def test_v2ex_http_failures_not_empty_or_retried(monkeypatch,status,kind):
    calls = []
    fake_http(monkeypatch, lambda req: calls.append(req) or httpx.Response(status))
    result = v2ex.legacy_search_v2ex('Python')
    assert result['failure_type'] == kind and len(calls) == 1


def test_v2ex_node_two_pages_and_public_detail_replies(monkeypatch):
    calls=[]
    def handle(req):
        calls.append(str(req.url))
        if req.url.path == '/go/jobs':
            page = req.url.params.get('p')
            return httpx.Response(200,text=f'<div id="TopicsNode"><div class="cell item"><span class="item_title"><a href="/t/{page}">北京 Python {page}</a></span></div></div><a class="page_normal" href="?p=2">2</a>')
        return httpx.Response(200,text='<h1>Python招聘</h1><div class="topic_content">完整帖子正文</div><div id="r_2"><a href="/member/u">u</a><div class="reply_content">回复内容</div></div>')
    fake_http(monkeypatch,handle)
    result=v2ex.legacy_search_v2ex('Python')
    assert len(result['items']) == 2 and result['metadata']['pagination']['pages_scanned'] == 2
    from kr_core import DetailRequest
    from runtime.platform_execution import ensure_bindings
    ensure_bindings()
    # 证据写入由公共详情用例处理；本测试只核HTTP及策略合同。
    from platform_adapters.v2ex_adapter import detail_strategy
    detail=detail_strategy().extract(DetailRequest(platform='V2EX',url=result['items'][0]['url'],options={'include_comments':True})).to_legacy_dict()
    assert detail['content'] == '完整帖子正文' and detail['comments'][0]['content'] == '回复内容'
    assert detail['content_type'] == 'community_discussion'


def test_v2ex_invalid_pat_never_silently_downgrades(monkeypatch):
    monkeypatch.setenv('KR_V2EX_TOKEN','test-token')
    calls=[]
    def handle(req):
        calls.append(req)
        assert req.headers['Authorization'] == 'Bearer test-token'
        return httpx.Response(401)
    fake_http(monkeypatch,handle)
    result=v2ex.detail('https://www.v2ex.com/t/1')
    assert result['failure_type'] == 'authorization_required' and len(calls) == 1


def test_v2ex_v2_reply_limit_does_not_claim_all(monkeypatch):
    monkeypatch.setenv('KR_V2EX_TOKEN','test-token')
    def handle(req):
        if req.url.path.endswith('/replies'):
            return httpx.Response(200,json={'success':True,'result':[{'id':x,'content':str(x)} for x in range(5)]})
        return httpx.Response(200,json={'success':True,'result':{'title':'职位讨论','content':'帖子正文','replies':5}})
    fake_http(monkeypatch,handle)
    result=v2ex.detail('https://www.v2ex.com/t/1',include_comments=True,comment_limit=2)
    assert len(result['comments']) == 2 and not result['comments_complete']
    assert result['comments_truncated']


def test_pending_manual_page_is_not_probed_in_background(monkeypatch):
    native_site.write_state(boss.state_path(), {'expires_at': native_site.time.time() + 300, 'cancelled': False})
    patch_native(monkeypatch,lambda **kw: pytest.fail('pending login must stay detached'))
    assert boss.probe_auth()['status']=='waiting_for_user'
    assert boss.request_login()['status']=='waiting_for_user'


def test_failed_manual_hold_release_never_marks_account_ready(monkeypatch):
    native_site.write_state(boss.state_path(), {'expires_at': 1})
    def invoke(**kw):
        return {'ok':False,'code':'browser_session_busy'} if kw['operation']=='release_login' else {'ok':True,'data':{'authenticated_ui':True}}
    patch_native(monkeypatch,invoke)
    assert boss.complete_login()['status']=='not_verified'
    assert not boss._site().state().get('verified_at')
    assert not boss.state_path().with_name('auth-generation.json').exists()


@pytest.mark.parametrize('change',[{'cancelled':True},{'risk':True},{'expires_at':999},{'navigation_loop':True}])
def test_late_auth_result_cannot_overwrite_new_interaction(monkeypatch,change):
    native_site.write_state(boss.state_path(), {'expires_at': 1})
    def invoke(**kw):
        if kw['operation']=='auth_verify':
            native_site.write_state(boss.state_path(),change)
        return {'ok':True,'data':{'authenticated_ui':True}}
    patch_native(monkeypatch,invoke)
    assert boss.complete_login()['failure_type']=='interaction_changed_during_probe'
    assert boss._site().state()==change
    assert not boss.state_path().with_name('auth-generation.json').exists()


def test_cancelled_login_can_restart_but_stale_completion_cannot(monkeypatch):
    native_site.write_state(boss.state_path(), {'cancelled':True,'expires_at':1})
    calls=[]
    patch_native(monkeypatch,lambda **kw:calls.append(kw) or {'ok':True,'data':{}})
    assert boss.complete_login()['failure_type']=='manual_interaction_cancelled'
    assert not calls
    assert boss.request_login(activate=True)['status']=='waiting_for_user'
    assert calls[0]['operation']=='login' and not boss._site().state()['cancelled']
    assert not boss.admission()['allowed']
