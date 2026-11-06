"""真实Chrome隔离世界中的打包脚本测试；仅本地页面，不宣称知乎业务验收。"""
import json
from pathlib import Path
import pytest

URL = 'https://www.zhihu.com/question/123/answer/1937205528846655537'
ANSWER_TARGET = {'kind':'answer','identifier':'1937205528846655537','question_id':'123','url':URL}
ARTICLE_TARGET = {'kind':'article','identifier':'123','question_id':'','url':'https://zhuanlan.zhihu.com/p/123'}


@pytest.fixture(scope='module')
def dom_browser():
    from playwright.sync_api import sync_playwright
    from runtime.executables import resolve_managed_chrome
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=str(resolve_managed_chrome().path),headless=True)
        try: yield browser
        finally: browser.close()


def _isolated_extract(browser,html,url,operation,payload,api=None):
    """真实隔离世界中的打包脚本合同；网页为本地fixture，不宣称真实知乎业务。"""
    context=browser.new_context()
    try:
        page=context.new_page()
        def route_request(route):
            if '/api/v4/comment_v5/' in route.request.url:
                assert api is not None, 'unexpected comment API request'
                status, value = api(route.request.url)
                route.fulfill(status=status,content_type='application/json',body=json.dumps(value,ensure_ascii=False))
            else:
                route.fulfill(status=200,content_type='text/html; charset=utf-8',body=html)
        page.route('**/*',route_request)
        page.goto(url)
        cdp=context.new_cdp_session(page)
        frame=cdp.send('Page.getFrameTree')['frameTree']['frame']['id']
        world=cdp.send('Page.createIsolatedWorld',{'frameId':frame,'worldName':'zhihu-contract'})['executionContextId']
        root=Path(__file__).resolve().parents[1]
        code="globalThis.KRSites={};globalThis.KRWaitForChange=async(signal,ms)=>new Promise(resolve=>setTimeout(resolve,Math.min(ms,25)));globalThis.KRWaitFor=async()=>{};"+(root/'browser_link/extension/sites/zhihu.js').read_text(encoding='utf-8')
        cdp.send('Runtime.evaluate',{'expression':code,'contextId':world})
        result=cdp.send('Runtime.evaluate',{'expression':f"KRSites.zhihu.extract({json.dumps(operation)},{json.dumps(payload)},new AbortController().signal)",
            'contextId':world,'awaitPromise':True,'returnByValue':True})
        assert 'exceptionDetails' not in result, result
        return result['result']['value']
    finally: context.close()


def test_packaged_dom_chooses_exact_entity_and_preserves_large_id(dom_browser):
    html='''<html><body><h1>指定问题</h1><div class="AnswerItem" data-zop='{"itemId":"5"}'><span class="RichText">别人的长回答</span></div>
      <script id="js-initialData">{"initialState":{"entities":{"answers":{"1937205528846655537":{"id":1937205528846655537,"content":"<p>指定的短回答</p>","author":{"name":"目标作者"},"question":{"title":"指定问题"}}}}}}</script></body></html>'''
    result=_isolated_extract(dom_browser,html,URL,'detail',{'target':ANSWER_TARGET})
    assert result['body_html']=='<p>指定的短回答</p>' and result['author']=='目标作者'
    assert result['target']['identifier']=='1937205528846655537' and result['body_complete']


def test_packaged_dom_never_uses_page_world_globals_or_unrelated_citations(dom_browser):
    html=f'''<h1>问题</h1><script>window.__INITIAL_STATE__={{answers:{{fake:true}}}}</script>
        <div class="AnswerItem" data-zop='{{"itemId":"5"}}'><span class="RichText">其他回答<a href="{URL}">引用了目标</a></span></div>'''
    result=_isolated_extract(dom_browser,html,URL,'detail',{'target':ANSWER_TARGET})
    assert result['failure_type']=='page_structure_changed'


def test_packaged_dom_classifies_actual_risk_page_without_search_or_login(dom_browser):
    html='<body>{"error":{"message":"您当前请求存在异常，暂时限制本次访问。","code":40362}}</body>'
    result=_isolated_extract(dom_browser,html,URL,'detail',{'target':ANSWER_TARGET})
    assert result['failure_type']=='anti_bot_verification' and result['risk'] and not result['login']


def test_sms_and_scan_login_copy_are_not_security_challenges(dom_browser):
    html='<div class="SignFlow" role="dialog">登录知乎 手机号 验证码 获取短信验证码 扫码登录</div>'
    result=_isolated_extract(dom_browser,html,'https://www.zhihu.com/signin?next=%2F','auth_verify',{})
    assert result['login'] and not result['risk'] and result['failure_type']=='login_required'


def test_auth_unconfirmed_is_typed_and_diagnostics_do_not_disclose_account(dom_browser):
    html='<header class="AppHeader"><div class="AppHeader-userInfo"><span class="UnknownProfile">private-user-name</span></div></header>'
    result=_isolated_extract(dom_browser,html,'https://www.zhihu.com/','auth_verify',{})
    assert not result['authenticated_ui'] and result['failure_type']=='auth_state_unconfirmed'
    assert result['auth_diagnostics']['header_present']
    assert result['auth_diagnostics']['verification_timed_out']
    assert 'private-user-name' not in json.dumps(result)


def test_auth_waits_for_delayed_profile_markup_in_same_document(dom_browser):
    html='''<header class="AppHeader"><div class="AppHeader-userInfo"></div></header>
    <script>setTimeout(()=>{document.querySelector('.AppHeader-userInfo').innerHTML='<button class="AppHeader-profileEntry"><img class="Avatar" style="width:20px;height:20px"></button>';},250);</script>'''
    result=_isolated_extract(dom_browser,html,'https://www.zhihu.com/','auth_verify',{})
    assert result['authenticated_ui'] and not result['failure_type']
    assert result['auth_diagnostics']['profile_marker_visible']


def test_header_login_entry_and_conflicting_profile_never_authenticate(dom_browser):
    html='<header class="AppHeader"><div class="AppHeader-userInfo"><button class="AppHeader-profileEntry"><img class="Avatar" style="width:20px;height:20px"></button><button class="AppHeader-login">登录 / 注册</button></div></header>'
    result=_isolated_extract(dom_browser,html,'https://www.zhihu.com/','auth_verify',{})
    assert result['login'] and not result['authenticated_ui'] and result['failure_type']=='login_required'


def test_generic_content_avatar_and_anonymous_home_do_not_prove_login(dom_browser):
    html='<header class="AppHeader"></header><article><img class="Avatar" style="width:20px;height:20px">公开内容</article>'
    result=_isolated_extract(dom_browser,html,'https://www.zhihu.com/','probe',{})
    assert not result['authenticated_ui'] and result['failure_type']=='auth_state_unconfirmed'
    assert not result['auth_diagnostics']['verification_timed_out']


@pytest.mark.parametrize('html,url',[
    ('<div role="dialog">请完成安全验证</div>','https://www.zhihu.com/signin'),
    ('<div class="Captcha">拖动滑块完成拼图</div>','https://www.zhihu.com/signin'),
    ('<body>验证页面</body>','https://www.zhihu.com/account/unhuman'),
])
def test_real_security_challenges_still_take_priority_over_login(dom_browser,html,url):
    result=_isolated_extract(dom_browser,html,url,'auth_verify',{})
    assert result['risk'] and result['failure_type']=='anti_bot_verification'


def test_hidden_challenge_and_quoted_verification_words_do_not_block_body(dom_browser):
    html='<div class="Captcha" hidden>安全验证</div><article><h1>验证码研究</h1><div class="Post-RichTextContainer">讨论如何设计验证码与安全验证。</div></article>'
    url='https://zhuanlan.zhihu.com/p/123'
    result=_isolated_extract(dom_browser,html,url,'detail',{'target':ARTICLE_TARGET})
    assert not result['risk'] and not result['failure_type'] and result['body_html']


def test_packaged_dom_keeps_image_manifest_and_rejects_wrong_target(dom_browser):
    url='https://zhuanlan.zhihu.com/p/123'
    html='<article><h1>测试文章</h1><div class="Post-RichTextContainer"><p>说明</p><img data-original="https://pic1.zhimg.com/a.png" alt="公式"><pre>代码\n  缩进</pre></div></article>'
    result=_isolated_extract(dom_browser,html,url,'detail',{'target':ARTICLE_TARGET})
    assert result['media_manifest'][0]['url']=='https://pic1.zhimg.com/a.png' and '<pre>代码\n  缩进</pre>' in result['body_html']
    wrong=_isolated_extract(dom_browser,html,url,'detail',{'target':ANSWER_TARGET})
    assert wrong['failure_type']=='page_identity_changed'


def test_packaged_search_has_explicit_empty_and_keeps_content_types(dom_browser):
    url='https://www.zhihu.com/search?type=content&q=Python'
    html=f'<div class="SearchResult-Card"><h2><a href="{URL}">回答标题</a></h2><div class="RichText">摘要</div></div>'
    result=_isolated_extract(dom_browser,html,url,'search',{'keyword':'Python','limit':2})
    assert result['items'][0]['url']==URL and result['items'][0]['title']=='回答标题'
    empty=_isolated_extract(dom_browser,'<div class="EmptyState">没有找到相关结果</div>',url,'search',{'keyword':'Python'})
    assert empty['empty_confirmed'] and not empty['items']
    wrong=_isolated_extract(dom_browser,html,url,'search',{'keyword':'wrong'})
    assert wrong['failure_type']=='search_query_not_applied'


def reader_html(inner):
    return '<div class="AnswerItem" data-answer-id="1937205528846655537">'+inner+'</div>'


def test_reader_comments_preserve_ids_thread_but_do_not_guess_parent(dom_browser):
    html=reader_html('<div class="Comments-container">2 条评论<div data-id="9007199254740993123"><a href="/people/a">甲</a><div class="CommentContent">正文<img src="https://pic1.zhimg.com/test.png"></div><div data-id="9007199254740993999"><div class="CommentContent">回复<img class="sticker" alt="[赞]" src="https://pic1.zhimg.com/sticker.png"></div></div></div></div>')
    value=_isolated_extract(dom_browser,html,URL,'comments',{'target':ANSWER_TARGET})
    assert value['exhausted'] and value['request_satisfied']
    root,reply=value['comments']
    assert root['id']=='9007199254740993123' and root['media'][0]['type']=='image'
    assert reply['root_id']==root['id'] and reply['parent_id']==''
    assert reply['relationship_confidence']=='dom_thread_root_only'
    assert reply['content']=='回复[赞]' and not reply['media']
    assert value['comment_diagnostics']['direct_api_requests']==0


def test_reader_clicks_only_target_once_and_waits_for_page_render(dom_browser):
    html=reader_html('<button onclick="this.disabled=true;setTimeout(()=>{this.outerHTML=\'<div class=&quot;Comments-container&quot;>1 条评论<div data-id=&quot;123&quot;><div class=&quot;CommentContent&quot;>正常页面</div></div></div>\'},100)">1 条评论</button>')
    value=_isolated_extract(dom_browser,html,URL,'comments',{'target':ANSWER_TARGET})
    assert value['comments'][0]['content']=='正常页面' and value['exhausted']


def test_reader_limit_and_unexpanded_replies_never_claim_exhaustion(dom_browser):
    html=reader_html('<div class="Comments-container">2 条评论<div data-id="1"><div class="CommentContent">甲</div><div data-id="2"><div class="CommentContent">乙</div></div></div><button>展开更多回复</button></div>')
    value=_isolated_extract(dom_browser,html,URL,'comments',{'target':ANSWER_TARGET,'limit':1})
    assert len(value['comments'])==1 and not value['exhausted'] and not value['request_satisfied']


@pytest.mark.parametrize('markup,kind',[
    ('<div class="Comments-container">加载失败</div>','reader_comments_load_failed'),
    ('<div class="Comments-container" hidden>暂无评论</div>','comments_control_not_exposed'),
])
def test_reader_failures_are_not_empty_and_never_fetch_api(dom_browser,markup,kind):
    value=_isolated_extract(dom_browser,reader_html(markup),URL,'comments',{'target':ANSWER_TARGET})
    assert value['failure_type']==kind and not value.get('empty_confirmed')


def test_reader_empty_requires_visible_explicit_message(dom_browser):
    value=_isolated_extract(dom_browser,reader_html('<button>0 条评论</button><div class="Comments-container">暂无评论</div>'),URL,'comments',{'target':ANSWER_TARGET})
    assert value['empty_confirmed'] and value['exhausted']


def test_reader_rejects_unrelated_answer_even_when_linked(dom_browser):
    html=f'<div class="AnswerItem" data-answer-id="42"><a href="{URL}">引用</a><button>1 条评论</button></div>'
    value=_isolated_extract(dom_browser,html,URL,'comments',{'target':ANSWER_TARGET})
    assert value['failure_type']=='page_structure_changed'


def test_reader_article_is_explicitly_unsupported(dom_browser):
    value=_isolated_extract(dom_browser,'<article>文章</article>',ARTICLE_TARGET['url'],'comments',{'target':ARTICLE_TARGET})
    assert value['failure_type']=='comments_target_unsupported'


def test_packaged_dom_rejects_paid_and_mismatched_parent(dom_browser):
    html='''<script id="js-initialData">{"initialState":{"entities":{"answers":{"1937205528846655537":{"id":"1937205528846655537","question":{"id":"999"},"content":"<p>其他问题</p>"}}}}}</script>'''
    assert _isolated_extract(dom_browser,html,URL,'detail',{'target':ANSWER_TARGET})['failure_type']=='page_identity_changed'
    paid='<div class="AnswerItem" data-answer-id="1937205528846655537"><div class="RichText">付费片段</div><button class="PurchaseBtn">购买</button></div>'
    assert _isolated_extract(dom_browser,paid,URL,'detail',{'target':ANSWER_TARGET})['failure_type']=='content_restricted'


def test_reader_loading_placeholder_is_not_confirmed_empty(dom_browser):
    html=reader_html(r'<div class="Comments-container">暂无评论</div><script>setTimeout(()=>{document.querySelector(".Comments-container").innerHTML="1 条评论<div data-id=\"123\"><div class=\"CommentContent\">延迟到达</div></div>"},400)</script>')
    value=_isolated_extract(dom_browser,html,URL,'comments',{'target':ANSWER_TARGET})
    assert not value['empty_confirmed'] and value['comments'][0]['content']=='延迟到达'


def test_reader_nonzero_counter_cannot_be_confirmed_empty(dom_browser):
    html=reader_html('<button>14 条评论</button><div class="Comments-container">暂无评论</div>')
    value=_isolated_extract(dom_browser,html,URL,'comments',{'target':ANSWER_TARGET})
    assert not value['empty_confirmed'] and not value['request_satisfied']


def test_reader_unapplied_order_remains_gap(dom_browser):
    html=reader_html('<div class="Comments-container">1 条评论<div data-id="1"><div class="CommentContent">甲</div></div></div>')
    value=_isolated_extract(dom_browser,html,URL,'comments',{'target':ANSWER_TARGET,'scope':{'order':'time'}})
    assert value['comments'] and not value['request_satisfied']
    assert value['coverage']['stop_reason']=='requested_order_not_applied'


def test_reader_reacquires_answer_after_react_replaces_node(dom_browser):
    html=reader_html('<button onclick="const n=this.parentElement.cloneNode(true);n.innerHTML=\'<div class=&quot;Comments-container&quot;>1 条评论<div data-id=&quot;9&quot;><div class=&quot;CommentContent&quot;>替换后</div></div></div>\';this.parentElement.replaceWith(n)">1 条评论</button>')
    value=_isolated_extract(dom_browser,html,URL,'comments',{'target':ANSWER_TARGET})
    assert value['comments'][0]['content']=='替换后'
