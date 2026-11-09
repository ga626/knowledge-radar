import pytest
from kr_core.content_contracts import duration_seconds
from collectors.platform.zhilian_parser import parse_job_html, normalize_jobs
from runtime.xhs_candidates import normalize_xhs_image_assets


@pytest.mark.parametrize("raw,expected", [(123,123),("04:03",243),("1:02:03",3723),("PT1H2M3S",3723),("P1DT2S",86402),("unknown",None)])
def test_duration_in_seconds(raw,expected):
    assert duration_seconds(raw)==expected


def test_job_links_not_homepage():
    html = '<a href="https://www.zhaopin.com/">智联招聘</a><article class="job-card"><a href="https://jobs.zhaopin.com/CC123456789J00123456789.htm">Python开发工程师</a><span>20K-30K</span></article>'
    jobs=parse_job_html(html)
    assert len(jobs)==1 and jobs[0]["content_type"]=="job_posting"
    assert not normalize_jobs([{"title":"首页","url":"https://jobs.zhaopin.com/"}])


def test_images_require_content_ownership():
    assets, images, quality=normalize_xhs_image_assets({"note_id":"current","image_assets":[
        {"url":"https://example.org/body.png","role":"content","note_id":"current"},
        {"url":"https://example.org/recommendation.png","role":"content","note_id":"other"},
        {"url":"https://example.org/avatar.png","role":"unknown"}]})
    assert images==["https://example.org/body.png"]
    assert quality["noise_count"]==1


def test_transcript_cache_policy_and_integrity(tmp_path,monkeypatch):
    from collectors.platform.bilibili import _write_transcript_cache,_read_transcript_cache
    path=str(tmp_path/"BVfixture_transcript.txt")
    _write_transcript_cache(path,"固定中文字幕 / English sample")
    assert _read_transcript_cache(path)=="固定中文字幕 / English sample"
    monkeypatch.setenv("KR_ASR_LANGUAGE","en")
    assert _read_transcript_cache(path)==""


def test_asr_cache_rejects_changed_audio_and_model(tmp_path, monkeypatch):
    from collectors.platform.bilibili import _write_transcript_cache,_read_transcript_cache
    from runtime import asset_identity
    model={'sha256':'first-model'}
    monkeypatch.setattr(asset_identity,'model_identity',lambda _:dict(model))
    audio=tmp_path/'fixture.wav';audio.write_bytes(b'original audio')
    path=str(tmp_path/'fixture_transcript.txt')
    _write_transcript_cache(path,'known words',kind='asr',audio_path=str(audio))
    assert _read_transcript_cache(path)=='known words'
    model['sha256']='changed-model'
    assert _read_transcript_cache(path)==''
    model['sha256']='first-model'
    audio.write_bytes(b'replaced audio')
    assert _read_transcript_cache(path)==''


def test_natural_speech_requires_bound_gold_and_critical_review():
    import hashlib
    from kr_core.content_contracts import natural_speech_assessment
    row = {"id": "en-fixture", "language": "en", "audio_sha256": "audio", "reference": "the word Sie", "hypothesis": "the world say"}
    annotation = {"id": row["id"], "audio_sha256": "audio", "corpus_reference": row["reference"]}
    result = natural_speech_assessment([row], {"samples": [annotation]})
    assert result["status"] == "INCOMPLETE" and result["samples"][0]["reference_kind"] == "corpus_reference"
    annotation.update(human_review_status="APPROVED", human_reference=row["reference"], reviewer="fixture-reviewer",
                      reviewed_transcript_sha256=hashlib.sha256(row["hypothesis"].encode()).hexdigest(),
                      critical_review={"numbers": "NOT_APPLICABLE", "proper_names": "FAIL", "meaning": "FAIL"})
    assert natural_speech_assessment([row], {"samples": [annotation]})["status"] == "FAIL"
    row["hypothesis"] = "the word Sie"
    assert natural_speech_assessment([row], {"samples": [annotation]})["status"] == "INCOMPLETE"


def test_empty_speech_evaluation_cannot_pass():
    from kr_core.content_contracts import natural_speech_assessment
    assert natural_speech_assessment([], {"samples": []})["status"] == "INCOMPLETE"


@pytest.mark.parametrize("text", ["Client Challenge\nPlease enable JavaScript to continue", "Redirecting..."])
def test_shell_is_not_requested_article_body(text):
    from kr_core.evidence_coverage import content_coverage
    result = content_coverage({"content": text})
    assert result["request_satisfied"] is False
    assert "content" in result["required_gaps"]


def test_partial_transcript_is_not_recommended_as_complete_evidence():
    from routing.agent_native import build_agent_native_fields
    data = {"content": "video description", "content_coverage": {"request_satisfied": False, "required_gaps": ["transcript"]}}
    assert build_agent_native_fields(data)["recommended_next_action"] != "use_as_evidence"


def test_wechat_code_preserves_br_and_indentation():
    from generic_web.collector import html_fragment_to_markdown
    result = html_fragment_to_markdown("<pre><code>if ok:<br>    alpha()<br><br>    beta()</code></pre>")
    assert "if ok:\n    alpha()\n\n    beta()" in result


def test_platform_cannot_supply_claim_credibility():
    from understanding.evidence import build_detail_evidence
    item = build_detail_evidence("https://www.zhihu.com/question/123", "知乎", {"content": "a source assertion " * 15})
    assert item.credibility.startswith("unverified")


def test_short_substantive_notice_not_rejected_by_length():
    from understanding.evidence import build_detail_evidence
    item = build_detail_evidence("https://example.org/notice", "web", {"content": "Service discontinued on November 1, 2026."})
    assert item.metadata["extraction_status"] == "extracted"


def test_video_description_has_its_own_content_role():
    from kr_core.evidence_coverage import content_coverage
    result = content_coverage({"content": "Learn the basics", "video_description": "Learn the basics"})
    assert result["parts"]["content"]["body_state"] == "description"
    assert result["request_satisfied"] is True


def test_challenge_html_cannot_become_an_article():
    from generic_web.collector import collect_rendered_html
    from generic_web.models import GenericWebRequest
    result = collect_rendered_html(GenericWebRequest("https://example.org/paper"), html="<html><title>Client Challenge</title><body>Please enable JavaScript to continue</body></html>")
    assert result.error and result.error["type"] == "access_challenge"


def test_local_asr_and_abstract_cannot_claim_original_article_body():
    from kr_core.evidence_coverage import content_coverage
    asr = content_coverage({'content':'Actual spoken words','source_kind':'asr'})
    assert asr['parts']['content']['source_role'] == 'transcript'
    abstract = content_coverage({'content':'Summary only','source_kind':'abstract'},{'content_scope':'fulltext'})
    assert not abstract['request_satisfied']


@pytest.mark.parametrize('html,blocked',[
    ('<body><main><h1>验证码实现</h1><p>Client challenge pages can be diagnosed. This article describes login security.</p></main></body>',False),
    ('<body><aside>Please enable JavaScript to continue</aside><article><h1>公告</h1><p>服务将于下周停止。</p></article></body>',False),
    ('<body><article><h1>公告</h1><p>服务将于下周停止。</p></article></body>',False),
    ('<body><div id="app"></div><script>location.replace("/target")</script></body>',True),
    ('<head><meta http-equiv="refresh" content="0;url=/target"></head><body>Redirecting...</body>',True),
])
def test_access_shell_recognition_preserves_real_short_articles(html,blocked):
    from bs4 import BeautifulSoup
    from generic_web.collector import _check_page_body, GenericWebError
    soup=BeautifulSoup(html,'html.parser')
    if blocked:
        with pytest.raises(GenericWebError):_check_page_body(soup,collector='fixture',status_code=200)
    else:
        _check_page_body(soup,collector='fixture',status_code=200)


FOOTER_SAMPLE = """Select topic of interest (you can select more than one)

Example Science, Inc.

1216 Broadway, 2nd Floor, New York, NY 10001, USA

© 2026 Example - All rights reserved

This site is protected by reCAPTCHA and the Google Privacy Policy and Terms of Service apply."""


def test_effectiveness_footer_only_cannot_satisfy_requested_body():
    from kr_core.evidence_coverage import content_coverage
    result=content_coverage({'title':'A technical research paper','content':FOOTER_SAMPLE})
    assert result['request_satisfied'] is False
    assert result['parts']['content']['missing_reason']=='boilerplate_only'


@pytest.mark.parametrize('body',[
    '服务将于明日停止。',
    'This tutorial explains reCAPTCHA, Privacy Policy and Terms of Service.',
    '```python\nprint("All rights reserved")\n```',
    'Our experiments found expansion harms stronger retrievers.'])
def test_effectiveness_footer_signals_do_not_delete_real_content(body):
    from kr_core.evidence_coverage import content_coverage
    result=content_coverage({'content':body+'\n\n'+FOOTER_SAMPLE})
    assert result['request_satisfied'] is True


def test_effectiveness_parser_falls_through_wrong_nonempty_result(monkeypatch):
    import httpx
    from generic_web import collector as c
    from generic_web.models import GenericWebRequest,GenericWebResponse
    monkeypatch.setattr("generic_web.acquisition.get_parsed",lambda key:None)
    html='<article><h1>真实研究</h1><p>Our research measures retrieval accuracy.</p></article><footer>Copyright</footer>'
    response=httpx.Response(200,text=html,request=httpx.Request('GET','https://example.org/regression'))
    monkeypatch.setattr(c,'public_addresses',lambda *a,**k:None)
    monkeypatch.setattr(c,'_fetch_html',lambda *a,**k:(response,'text/html'))
    monkeypatch.setattr(c,'_collect_via_trafilatura',lambda req,**k:GenericWebResponse(url=req.url,content=FOOTER_SAMPLE,collector='fixture-first'))
    monkeypatch.setattr(c,'_collect_via_readability',lambda req,**k:GenericWebResponse(url=req.url,content='Our research measures retrieval accuracy.',collector='fixture-second'))
    result=c.collect_url(GenericWebRequest('https://example.org/regression',use_jina=False))
    assert not result.error and result.collector=='fixture-second'
    assert result.metadata['extraction_observation']['semantic_relevance']=='not_evaluated'


def test_effectiveness_rendered_footer_is_not_body(monkeypatch):
    from generic_web import collector as c
    from generic_web.models import GenericWebRequest, GenericWebResponse
    monkeypatch.setattr(c, '_collect_from_html', lambda req, **kw: GenericWebResponse(url=req.url, content=FOOTER_SAMPLE, collector='dynamic_playwright'))
    result = c.collect_rendered_html(GenericWebRequest('https://example.org/footer'), html='<body>fixture</body>')
    assert result.error and result.error['message'] == 'boilerplate_only'


# PR39 native receipt: receipt-5f4b45369990a34c; public page text, no account data.
F39_ERROR_PAGE = """Not found

This page does not exist

Interactive graph

On this page

[Powered by Obsidian Publish](https://publish.obsidian.md)

Oh no! Seems like something went wrong!"""


@pytest.mark.parametrize('text', [F39_ERROR_PAGE, ' '.join(F39_ERROR_PAGE.split()),
    '# 404 Not Found\nThe requested page could not be found.\nBack to home',
    '页面不存在\n您访问的页面不存在\n返回首页'])
def test_f39_error_only_page_never_satisfies_body_even_with_stale_body_declaration(text):
    from kr_core.evidence_coverage import content_coverage
    result=content_coverage({'title':'Back up your Obsidian files - Obsidian Help','content':text,
                            'body_state':'body','asset_kind':'body','source_role':'body'})
    part=result['parts']['content']
    assert not result['request_satisfied'] and part['state']=='missing'
    assert part['body_state']=='error_shell' and part['missing_reason']=='error_shell'
    assert part['source_role']!='body' and part['asset_kind']!='body'


@pytest.mark.parametrize('text', ['服务将于明日停止。','Not found means the server could not locate the requested resource.',
    '```python\nraise ValueError("Not found")\n```',
    'This page does not exist\n这是错误提示的中文翻译教程。',
    'Not found\nThis page does not exist\nOur backup procedure is to copy the vault before upgrading.'])
def test_f39_real_short_articles_and_code_remain_acquired(text):
    from kr_core.evidence_coverage import content_coverage
    assert content_coverage({'title':'Not found','content':text})['request_satisfied']


def test_f39_rendered_error_page_returns_typed_failure(monkeypatch):
    from generic_web import collector as c
    from generic_web.models import GenericWebRequest,GenericWebResponse
    monkeypatch.setattr(c,'_collect_from_html',lambda req,**kw:GenericWebResponse(url=req.url,
        title='Back up your Obsidian files',content=F39_ERROR_PAGE,collector='dynamic_playwright'))
    result=c.collect_rendered_html(GenericWebRequest('https://example.org/missing'),html='<main>fixture</main>')
    assert result.error and result.error['message']=='error_shell'


def test_f39_detail_evidence_does_not_call_error_text_extracted():
    from understanding.evidence import build_detail_evidence
    item=build_detail_evidence('https://example.org/missing','web',{'title':'Backup','content':F39_ERROR_PAGE})
    assert item.metadata['extraction_status']=='insufficient_content'
    assert item.metadata['body_state']=='error_shell'


def test_f39_real_rendered_dom_is_rejected_but_article_with_error_sidebar_is_kept():
    from generic_web.collector import collect_rendered_html
    from generic_web.models import GenericWebRequest
    request=GenericWebRequest('https://example.org/missing')
    result=collect_rendered_html(request,html='<title>Backup</title><main><h1>Not found</h1><p>This page does not exist</p></main><nav>On this page</nav>')
    assert result.error and not result.content
    good=collect_rendered_html(request,html='<title>Backup</title><aside>Not found This page does not exist</aside><article><h1>备份指南</h1><p>升级前请复制整个知识库目录。</p></article>')
    assert not good.error and '升级前请复制' in good.content


def test_f39_stale_acquired_flag_is_reconciled_before_evidence_storage(tmp_path):
    from kr_core.evidence_store import EvidenceStore
    response={'content':F39_ERROR_PAGE,'title':'Backup','content_coverage':{
        'parts':{'content':{'requested':True,'state':'acquired','body_state':'body'},
                 'transcript':{'requested':True,'state':'missing'}},
        'required_gaps':['transcript'],'request_satisfied':True}}
    store=EvidenceStore(str(tmp_path/'evidence.jsonl'))
    store.append_detail(platform='dynamic_web',url='https://example.org/missing',response=response)
    assert not response['content_coverage']['request_satisfied']
    assert set(response['content_coverage']['required_gaps'])=={'content','transcript'}
    assert store.assets.fetch(response['evidence_assets'][0]['version_id'])['text']==F39_ERROR_PAGE
    assert response['content_coverage']['parts']['content']['body_state']=='error_shell'
