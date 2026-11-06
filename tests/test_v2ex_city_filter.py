from __future__ import annotations

from collectors.platform import v2ex


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
