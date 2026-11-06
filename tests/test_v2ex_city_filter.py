from __future__ import annotations

from collectors.platform import v2ex


def test_v2ex_adapter_passes_unified_comment_budget(monkeypatch):
    from kr_core.models import DetailRequest
    from platform_adapters.v2ex_adapter import detail_strategy
    seen = {}
    def collect(url, **kwargs):
        seen.update(kwargs)
        return {'title': 'topic', 'content': 'body', 'url': url}
    monkeypatch.setattr(v2ex, 'detail', collect)
    request = DetailRequest(url='https://v2ex.com/t/1', platform='V2EX',
        options={'include_comments': True, 'comments': {'root_limit': 2, 'reply_limit': 1, 'total_limit': 3, 'max_pages': 1}})
    detail_strategy().extract(request)
    assert seen['comment_limit'] == 2
    assert seen['comment_max_pages'] == 1


def test_v2ex_city_filter_keeps_matching_city_and_remote() -> None:
    assert v2ex._city_matches("外企 TD Synnex 招聘 [成都/北京]", "北京")
    assert v2ex._city_matches("AI 工程师 远程全职", "北京")
    assert not v2ex._city_matches("深圳 Python 开发", "北京")


def test_v2ex_html_parser_filters_by_city() -> None:
    html = """
    <div class="cell item">
      <span class="item_title"><a href="/t/1">深圳 Python 开发</a></span>
      <strong><a href="/member/a">a</a></strong>
    </div>
    <div class="cell item">
      <span class="item_title"><a href="/t/2">北京 Python 开发</a></span>
      <strong><a href="/member/b">b</a></strong>
    </div>
    """

    items = v2ex._parse_v2ex_jobs_html(html, "https://www.v2ex.com/go/jobs", keyword="Python", city="北京", limit=10)

    assert [item["title"] for item in items] == ["北京 Python 开发"]


def test_v2ex_empty_result_is_no_result_signal(monkeypatch):
    monkeypatch.setattr(v2ex,'discover',lambda *a,**kw:{'platform':'V2EX','items':[],'status':'empty','empty_reason':'no_match_in_scanned_node_pages','metadata':{'open_index_eligible':True}})
    result=v2ex.legacy_search_v2ex('unlikely-keyword',limit=3,city='北京')
    assert result['status']=='empty' and not result.get('error')
    assert result['metadata']['open_index_eligible']


def test_v2ex_official_reply_limits_and_page_coverage(monkeypatch):
    from contextlib import nullcontext
    from runtime import recruitment_governance
    monkeypatch.setenv('KR_V2EX_TOKEN', 'fixture-not-a-real-token')
    monkeypatch.setattr(recruitment_governance, 'execute_operation', lambda p, op, call, **kw: call())
    monkeypatch.setattr(v2ex, '_client', lambda: nullcontext(object()))
    calls = []
    def get(client, path, **kw):
        calls.append(path)
        data = {'title':'topic', 'content':'body', 'replies':20} if '/replies?' not in path else [
            {'id':i,'content':str(i)} for i in range(10)]
        return type('Response', (), {'json':lambda self:{'success':True,'result':data}})()
    monkeypatch.setattr(v2ex, '_get', get)
    small=v2ex.detail('https://v2ex.com/t/1',include_comments=True,comment_limit=2,comment_max_pages=1)
    assert len(small['comments']) == 2 and small['comments_next_offset'] == 2
    assert small['comments_coverage']['request_satisfied'] is True
    calls.clear()
    bounded=v2ex.detail('https://v2ex.com/t/1',include_comments=True,comment_limit=15,comment_max_pages=1)
    assert len(bounded['comments']) == 10
    assert bounded['comments_coverage'] == {'request_satisfied':False, 'exhausted':False, 'stop_reason':'comment_page_budget'}
    assert sum('/replies?' in path for path in calls) == 1
