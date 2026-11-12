"""公开职位正文必须保留字段出处，不能串入相似岗位工资。"""
import json

import httpx
import pytest

from generic_web import GenericWebRequest
from generic_web.collector import collect_url, collect_rendered_html
from collectors.platform.jobposting import parse_jobposting

URL = "https://www.zhaopin.com/jobdetail/CC123J456.htm"
JD = "岗位职责：负责Python后端开发与商家授权接口、增量同步和故障恢复。任职要求：熟悉SQL和服务开发，具有独立交付经验。" * 5


def html_page(*, salary="-元 查看薪资", self_card=True):
    card = f'<a href="{URL}" class="recommend-item"><h3>接入工程师</h3><span>1.2-1.8万·15薪</span><p>目标公司</p></a>' if self_card else ""
    return f"""<html><head><title>接入工程师招聘</title></head><body>
    <main><header><h1>接入工程师</h1><p class="summary-plane__salary">{salary}</p>
    <p class="summary-plane__info">福州 · 3-5年 · 本科</p><a class="summary-plane__company">目标公司</a></header>
    <section><h2>职位描述</h2><p>{JD}</p></section></main>
    <aside><h2>相似职位</h2><a href="/jobdetail/OTHER.htm"><h3>接入工程师</h3><span>8-9万·18薪</span><p>另一家公司</p></a>{card}</aside>
    </body></html>"""


def extract(monkeypatch, html, *, rendered=False):
    if rendered:
        return collect_rendered_html(GenericWebRequest(URL, use_jina=False), html=html)
    from generic_web import collector, acquisition
    acquisition._PARSED.clear()
    acquisition._RESPONSES.clear()
    raw = httpx.Response(200, text=html, headers={"content-type": "text/html"}, request=httpx.Request("GET", URL))
    monkeypatch.setattr(collector, "_fetch_html", lambda *a, **k: (raw, "text/html"))
    # 故意让正文算法只返回职责，复现页头字段漏失；字段必须来自同一次原始字节。
    monkeypatch.setattr("trafilatura.extract", lambda *a, **k: JD)
    return collect_url(GenericWebRequest(URL, use_jina=False))


@pytest.mark.parametrize("rendered", [False, True])
def test_header_and_same_url_quote_remain_separate_from_description(monkeypatch, rendered):
    result = extract(monkeypatch, html_page(), rendered=rendered)
    assert result.error is None
    fields = result.metadata["job_posting"]
    assert fields["company"] == "目标公司"
    assert fields["location"] == "福州 · 3-5年 · 本科"
    assert fields["salary"] == ""  # 页头遮蔽，不能用推荐卡覆盖主字段。
    assert fields["salary_claim_allowed"] is False
    assert fields["salary_observations"][0]["value"] == "1.2-1.8万·15薪"
    assert fields["salary_observations"][0]["source_role"] == "same_url_recommendation"
    assert "8-9万" not in json.dumps(fields, ensure_ascii=False)
    assert "福州" in result.content and "3-5年" in result.content and "15薪" in result.content
    assert "原页头部" in result.content and "同一职位链接" in result.content


def test_target_header_salary_does_not_gain_months_from_recommendation(monkeypatch):
    result = extract(monkeypatch, html_page(salary="1.2-1.8万/月"))
    fields = result.metadata["job_posting"]
    assert fields["salary"] == "1.2-1.8万/月"
    assert fields["field_sources"]["salary"]["source_role"] == "primary_header"
    assert "15" not in fields["salary"]


def test_unrelated_recommendation_never_unmasks_target_salary(monkeypatch):
    result = extract(monkeypatch, html_page(self_card=False))
    fields = result.metadata["job_posting"]
    assert fields["salary"] == "" and not fields["salary_observations"]
    assert "8-9万" not in result.content


def test_jsonld_identity_selects_target_not_first_recommended_job():
    other = {"@type": "JobPosting", "title": "别的岗位", "url": URL.replace("CC123J456", "OTHER"),
        "description": JD, "baseSalary": {"value": {"minValue": 80000, "maxValue": 90000}}}
    target = {"@type": "JobPosting", "title": "接入工程师", "url": URL,
        "description": JD, "hiringOrganization": {"name": "目标公司"},
        "jobLocation": {"address": {"addressLocality": "福州", "streetAddress": "园区"}},
        "experienceRequirements": "3-5年", "educationRequirements": "本科",
        "datePosted": "2026-10-27", "baseSalary": {"currency": "CNY",
            "value": {"minValue": 12000, "maxValue": 18000, "unitText": "MONTH"}}}
    html = '<h1>接入工程师</h1><script type="application/ld+json">' + json.dumps([other, target]) + '</script>'
    result = parse_jobposting(html, url=URL)
    assert result["title"] == "接入工程师" and result["location"] == "福州 园区"
    assert result["experience"] == "3-5年" and result["education"] == "本科"
    assert result["published_at"] == "2026-10-27"
    assert result["salary"] == "12000-18000 CNY MONTH"


def test_identity_mismatch_never_attaches_foreign_jsonld():
    html = '<h1>接入工程师</h1><script type="application/ld+json">' + json.dumps({
        "@type": "JobPosting", "title": "相似推荐", "url": URL.replace("CC123J456", "OTHER"), "description": JD}) + '</script>'
    assert parse_jobposting(html, url=URL) is None


def test_liepin_static_detail_does_not_take_another_jobs_jsonld(monkeypatch):
    from collectors.platform.liepin import _static_liepin_detail
    target = "https://www.liepin.com/job/123456.shtml"
    html = '<script type="application/ld+json">' + json.dumps({
        "@type": "JobPosting", "title": "别的岗位", "url": target.replace("123456", "999999"),
        "description": JD, "baseSalary": {"value": {"minValue": 80000, "maxValue": 90000}}}) + '</script>'
    raw = httpx.Response(200, text=html, request=httpx.Request("GET", target))
    monkeypatch.setattr("generic_web.url_policy.guarded_fetch", lambda *a, **k: raw)
    assert _static_liepin_detail(target) is None


def test_duplicate_paragraphs_preserve_raw_and_mark_derived_cleanup(monkeypatch):
    raw = html_page().replace(f"<p>{JD}</p>", f"<p>{JD}</p><p>{JD}</p>")
    result = extract(monkeypatch, raw, rendered=True)
    assert result.content.count(JD) == 1
    assert result.metadata["job_posting"]["removed_duplicate_paragraphs"] == 1


def test_job_field_enrichment_never_turns_login_shell_into_body(monkeypatch):
    shell = '<html><title>Security Verification</title><h1>接入工程师</h1><script src="/captcha.js"></script><p>请完成安全验证</p></html>'
    result = extract(monkeypatch, shell, rendered=True)
    assert result.error and not result.content


def test_plain_article_with_salary_mentions_is_not_a_job(monkeypatch):
    html = f'<html><title>行业观察</title><article><h1>工程师的收入讨论</h1><p>{JD}</p><p>某公司1.2-1.8万</p></article></html>'
    result = extract(monkeypatch, html, rendered=True)
    assert "job_posting" not in result.metadata

def test_same_url_salary_sibling_is_preserved_without_neighbor_quote(monkeypatch):
    page = html_page(self_card=False).replace("</aside>",
        f'<li class="job-recommend__item"><a href="{URL}">接入工程师</a><span>1.2-1.8万·15薪</span><a href="/company/one">目标公司</a></li></aside>')
    result = extract(monkeypatch, page)
    quotes = result.metadata["job_posting"]["salary_observations"]
    assert [q["value"] for q in quotes] == ["1.2-1.8万·15薪"]


def test_ordinary_pages_skip_job_parser(monkeypatch):
    from collectors.platform import jobposting
    monkeypatch.setattr(jobposting, "parse_jobposting", lambda *a, **k: pytest.fail("irrelevant page reparsed"))
    from generic_web.models import GenericWebResponse
    response = GenericWebResponse(url="https://example.org/article", content="ordinary article")
    assert jobposting.enrich_job_response(response, html="<article>ordinary article</article>",
                                          url=response.url) is response
