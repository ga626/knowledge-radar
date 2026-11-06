"""微信真实缺陷回归及公共合同；网络与云端调用另外留真实回执。"""

import json
import httpx
import pytest

ARTICLE = """<html><h1 id="activity-name">芯片研究图解</h1><span id="js_name">研究公众号</span>
<span id="js_author_name">张研究员</span><span id="publish_time">2026-11-05 08:30</span>
<div id="copyright_logo">原创</div><div id="js_content"><p>这是正文，不是搜索摘要。本文讨论环境异常和验证码。</p>
<h2>方法</h2><pre><code>print(1)</code></pre><table><tr><td>样本</td><td>结论</td></tr></table>
<img data-src="https://mmbiz.qpic.cn/test.png?wx_fmt=png" alt="流程图">
<a href="https://example.org/paper.pdf">原始资料</a></div></html>"""
URL = "https://mp.weixin.qq.com/s/test-article"


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    for key, folder in {
        "KR_STATE_DIR": "state",
        "KR_EVIDENCE_ROOT": "evidence",
        "KR_MEDIA_CACHE_DIR": "media",
        "KR_LOG_DIR": "logs",
        "KR_RUNTIME_LEASE_DB_PATH": "leases.sqlite3",
        "KR_RUNTIME_ENV_PATH": "absent.env",
    }.items():
        monkeypatch.setenv(key, str(tmp_path / folder))
    return tmp_path


def test_challenge_never_becomes_generic_article(monkeypatch):
    from generic_web import collector
    from generic_web.models import GenericWebRequest

    url = "https://mp.weixin.qq.com/s/test-article"
    response = httpx.Response(
        200,
        text="<html><h2>环境异常</h2><p>当前环境异常，完成验证后即可继续访问。</p><a>去验证</a></html>",
        headers={"content-type": "text/html"},
        request=httpx.Request("GET", url),
    )
    monkeypatch.setattr(collector, "public_addresses", lambda *a, **k: None)
    monkeypatch.setattr(collector, "_fetch_html", lambda *a, **k: (response, "text/html"))
    result = collector.collect_url(GenericWebRequest(url=url))
    assert result.error and result.error["type"] == "anti_bot_verification"
    assert not result.content


def test_specific_fields_media_and_no_duplicate_title():
    from collectors.platform.wechat import parse_html

    data = parse_html(ARTICLE, URL)
    assert data["body_verified"] and data["body_complete"]
    assert data["author"] == "张研究员" and data["account_name"] == "研究公众号"
    assert data["provenance"]["publisher"] == "研究公众号"
    assert data["published_at"] == "2026-11-05 08:30"
    assert data["title"] not in data["content"]
    assert "print(1)" in data["content"] and "| 样本 | 结论 |" in data["content"]
    assert data["media_manifest"][0]["url"].endswith("wx_fmt=png")
    assert data["media_manifest"][0]["locator"] == "wechat:image:0"
    assert data["attachments"][0]["url"] == "https://example.org/paper.pdf"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('<div class="weui-msg">该内容已被发布者删除</div>', "article_deleted"),
        ('<div id="js_content">预览正文</div><div id="js_pay_preview">付费阅读</div>', "paid_preview"),
        ('<div class="page_msg">请先登录</div>', "authentication_required"),
        ('<div id="app"></div>', "article_structure_unrecognized"),
    ],
)
def test_non_article_never_has_body(body, expected):
    from collectors.platform.wechat import parse_html

    data = parse_html(body, URL)
    assert data["failure_type"] == expected and not data["content"] and not data["body_verified"]


def test_url_identity_no_fake_hosts_or_overmerge():
    from collectors.platform.wechat import matches_url, article_identity

    assert not matches_url("https://mp.weixin.qq.com.evil.org/s/test")
    assert not matches_url("https://mp.weixin.qq.com@evil.org/s/test")
    assert not matches_url("https://mp.weixin.qq.com/s?mid=1")
    assert not matches_url("https://weixin.sogou.com/weixin?query=test")
    assert article_identity(URL + "?scene=1") == article_identity(URL + "?scene=2")
    long = "https://mp.weixin.qq.com/s?__biz=abc&mid=1&idx=1&sn=xx"
    assert article_identity(long) != article_identity(long.replace("idx=1", "idx=2"))
    assert article_identity(URL) != article_identity(long)


def test_search_registers_unverified_and_preserves_configuration(isolated, monkeypatch):
    from use_cases.wechat_search import execute
    from kr_core.search_cache import SearchCache
    from collectors.platform import weread

    calls = []
    recorded = []

    def public(request, **kwargs):
        calls.append(request)
        return {
            "provider": "fake",
            "items": [
                {"url": URL + "?scene=1", "title": "芯片研究"},
                {"url": "https://mp.weixin.qq.com/s/opaque", "title": "unknown"},
            ],
            "metadata": {"constraint_application": {"freshness": "week"}},
        }

    monkeypatch.setattr(
        weread,
        "search",
        lambda *a, **k: {
            "items": [{"url": URL + "?scene=2", "title": "芯片研究", "source_provider": "weread_sosou"}],
            "metadata": {"continuation": None},
        },
    )
    result = execute(
        "芯片研究",
        limit=1,
        cache=SearchCache(),
        record_evidence=lambda *a: recorded.append(a),
        web_execute=public,
        account_hint="研究号",
    )
    assert len(result["items"]) == 1 and len(result["acquired_items"]) == 2
    assert result["metadata"]["unverified_candidate_count"] == 1
    assert result["metadata"]["account_scope"]["relaxed"] is False
    assert all("研究号" in r.query for r in calls)
    assert result["metadata"]["source_outcomes"][0]["metadata"]["constraint_application"]
    assert result["acquired_items"][0]["discovery_sources"] == ["web", "weread_sosou"]
    assert recorded[0][0] == "微信"


def test_search_cancellation_not_swallowed(isolated):
    from use_cases.wechat_search import execute
    from kr_core.search_cache import SearchCache
    from runtime.execution_context import ExecutionStopped

    def cancelled(*a, **k):
        raise ExecutionStopped("deadline_exceeded")

    with pytest.raises(ExecutionStopped):
        execute("芯片", source="web", cache=SearchCache(), record_evidence=lambda *a: None, web_execute=cancelled)


def test_all_sources_failed_is_not_empty(isolated, monkeypatch):
    from use_cases.wechat_search import execute
    from kr_core.search_cache import SearchCache
    from collectors.platform import weread

    monkeypatch.setattr(weread, "search", lambda *a, **k: {"items": [], "error": {"type": "access_forbidden"}})
    data = execute("芯片", source="weread", cache=SearchCache(), record_evidence=lambda *a: None)
    assert data["status"] == "failed" and data["error"]["type"] == "search_sources_failed"


def test_staged_ocr_resume_keeps_sha_and_denominator(isolated, monkeypatch):
    from dataclasses import replace
    from kr_core import DetailRequest
    from collectors.platform import wechat
    from detail_strategies import wechat as strategy
    from runtime import media_acquisition, media_execution
    from runtime.optional_media import file_sha
    from runtime.model_roles import CLOUD_OCR_COMPONENT

    calls = []
    monkeypatch.setattr(wechat, "collect_article", lambda *a, **k: wechat.parse_html(ARTICLE, URL))
    asset = isolated / "original.png"
    asset.write_bytes(b"fixture-image")
    monkeypatch.setattr(
        media_acquisition,
        "acquire_http_asset",
        lambda *a, **k: {
            "type": "image",
            "status": "acquired",
            "path": str(asset),
            "sha256": file_sha(asset),
            "source_url": a[1],
        },
    )

    def process(asset, component, **kwargs):
        cached = kwargs.get("cached")
        if cached and cached.get("status") == "ok":
            return {**cached, "reused": True}
        calls.append(component)
        return {"status": "ok", "text": "图中研究结论", "input_sha256": asset["sha256"]}

    monkeypatch.setattr(media_execution, "process_local_asset", process)
    request = DetailRequest(
        URL,
        platform="微信",
        task_scope_id="same-task",
        options={"include_ocr": True, "_detail_stage": "collection", "wechat_checkpoint_id": "fixed"},
    )
    data = strategy.collect(request)
    assert data["_enhancement_pending"] and calls == []
    result = strategy.enhance_detail(request, data)
    assert result["ocr"]["request_complete"] and "图中研究结论" in result["ocr_text"]
    assert calls == [CLOUD_OCR_COMPONENT]
    resumed = strategy.enhance_detail(request, data)
    assert resumed["ocr"]["request_complete"] and len(calls) == 1
    limited = replace(request, options={**request.options, "wechat": {"image_indexes": [0, 1]}})
    result = strategy.enhance_detail(limited, data)
    assert not result["ocr"]["request_complete"] and result["ocr"]["coverage"]["requested_images"] == 2
    assert any(g.get("image_index") == 1 for g in result["media_gaps"])


def test_body_without_media_does_not_start_ocr(isolated, monkeypatch):
    from kr_core import DetailRequest
    from collectors.platform import wechat
    from detail_strategies import wechat as strategy
    from runtime import media_execution

    monkeypatch.setattr(
        wechat, "collect_article", lambda *a, **k: wechat.parse_html('<div id="js_content">纯文本正文</div>', URL)
    )
    monkeypatch.setattr(media_execution, "process_local_asset", lambda *a, **k: pytest.fail("must not invoke model"))
    data = strategy.collect(
        DetailRequest(URL, platform="微信", options={"include_ocr": True, "include_transcript": True})
    )
    assert data["ocr"]["status"] == "not_applicable" and data["transcript_result"]["status"] == "not_applicable"


def test_opencli_explicit_read_only_and_no_auto_fallback(monkeypatch):
    from collectors.platform import wechat
    from runtime import opencli_bridge

    calls = []

    def invoke(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "data": {"html": ARTICLE, "url": URL, "account_name": "研究公众号"}}

    monkeypatch.setattr(opencli_bridge, "invoke_page", invoke)
    data = wechat.collect_article(URL, transport="opencli")
    assert data["body_verified"] and calls[0]["isolated_work"] is True
    assert calls[0]["operation"] == "detail"
    assert not any(k in calls[0] for k in ("steps", "manual_wait", "capture_pattern"))


def test_registered_detail_evidence_version_and_source(isolated, monkeypatch):
    from kr_core import DetailRequest
    from platform_adapters.factory import register_default_adapters
    from kr_core.registry import registry
    from runtime.detail_composition import resolve_strategy
    from use_cases.content_detail import extract_detail_response
    from collectors.platform import wechat
    from kr_core.evidence_store import EvidenceStore
    from runtime.research_archive import source_label

    register_default_adapters()
    assert registry.platform_for_url(URL) == "微信"
    monkeypatch.setattr(wechat, "collect_article", lambda *a, **k: wechat.parse_html(ARTICLE, URL))
    data = extract_detail_response(
        DetailRequest(URL, platform="微信"), strategy=resolve_strategy("微信")
    ).to_legacy_dict()
    store = EvidenceStore()
    store.record_detail(platform="微信", url=URL, response=data)
    ref = next(r for r in data["evidence_assets"] if r["kind"] == "content")
    reread = store.assets.fetch(ref["version_id"], max_chars=24000)
    assert reread["text"] == data["content"]
    assert source_label({"metadata": "{}", "kind": "content", "source_url": URL}) == "微信"
    from runtime.research_archive import asset_header

    with store.assets.connection() as c:
        row = dict(c.execute("SELECT * FROM assets WHERE version_id=?", (ref["version_id"],)).fetchone())
    header = asset_header(row)
    assert "发布者：研究公众号" in header and "署名：张研究员" in header


def test_interrupted_second_image_resumes_first_without_model_resend(isolated, monkeypatch):
    from kr_core import DetailRequest
    from collectors.platform import wechat
    from detail_strategies import wechat as strategy
    from runtime import media_acquisition, media_execution
    from runtime.execution_context import ExecutionStopped
    from runtime.optional_media import file_sha

    raw = ARTICLE.replace("</div></html>", '<img data-src="https://mmbiz.qpic.cn/second.png"></div></html>')
    monkeypatch.setattr(wechat, "collect_article", lambda *a, **k: wechat.parse_html(raw, URL))

    def acquire(kind, url, *a, **k):
        path = isolated / ("second.png" if "second" in url else "first.png")
        path.write_bytes(url.encode())
        return {"path": str(path), "source_url": url, "sha256": file_sha(path)}

    monkeypatch.setattr(media_acquisition, "acquire_http_asset", acquire)
    sends = []
    interrupted = [False]

    def process(asset, component, **kwargs):
        if kwargs.get("cached") and kwargs["cached"].get("status") == "ok":
            return {**kwargs["cached"], "reused": True}
        if "second" in asset["source_url"] and not interrupted[0]:
            interrupted[0] = True
            raise ExecutionStopped("cancelled")
        sends.append(asset["source_url"])
        return {"status": "ok", "text": "识别文字", "input_sha256": asset["sha256"]}

    monkeypatch.setattr(media_execution, "process_local_asset", process)
    request = DetailRequest(
        URL,
        platform="微信",
        task_scope_id="resume-images",
        options={"include_ocr": True, "_detail_stage": "collection", "wechat_checkpoint_id": "resume"},
    )
    collected = strategy.collect(request)
    with pytest.raises(ExecutionStopped):
        strategy.enhance_detail(request, collected)
    result = strategy.enhance_detail(request, collected)
    assert result["ocr"]["request_complete"] and len(sends) == 2
    assert result["ocr"]["results"][0]["reused"]
    assert [row["image_index"] for row in result["ocr"]["results"]] == [0, 1]


def test_successful_silent_audio_is_complete_not_a_missing_transcript(isolated, monkeypatch):
    from kr_core import DetailRequest
    from collectors.platform import wechat
    from detail_strategies import wechat as strategy
    from runtime import media_acquisition, media_execution

    raw = '<div id="js_content"><p>音频资料</p><audio src="https://example.org/audio.mp3"></audio></div>'
    monkeypatch.setattr(wechat, "collect_article", lambda *a, **k: wechat.parse_html(raw, URL))
    monkeypatch.setattr(media_acquisition, "acquire_media_ref", lambda *a, **k: {"sha256": "a" * 64})
    monkeypatch.setattr(
        media_execution, "enhance_asset_batch", lambda *a, **k: [{"status": "ok", "text": "", "input_sha256": "a" * 64}]
    )
    data = strategy.collect(DetailRequest(URL, platform="微信", options={"include_transcript": True}))
    assert data["transcript_result"]["request_complete"]
    assert data["transcript_result"]["processed_media"] == 1


def test_new_audio_widget_keeps_gap_instead_of_claiming_no_audio():
    from collectors.platform import wechat
    result=wechat.parse_html('<div id="js_content"><p>故事</p><mp-common-claudio data-type="audio"></mp-common-claudio></div>',URL)
    assert len(result['media_manifest'])==1
    assert result['media_manifest'][0]['type']=='audio'
    assert result['media_manifest'][0]['media_access']=='embedded_player_not_media'


def test_video_maps_native_chunk_times_and_resumes(isolated,monkeypatch):
    from kr_core import DetailRequest
    from collectors.platform import wechat,wechat_media
    from detail_strategies import wechat as strategy
    from runtime import media_acquisition,media_execution
    from runtime.optional_media import file_sha
    raw='<div id="js_content"><p>视频</p><iframe src="https://mp.weixin.qq.com/player?vid=one"></iframe></div>'
    monkeypatch.setattr(wechat,'collect_article',lambda *a,**k:wechat.parse_html(raw,URL))
    monkeypatch.setattr(wechat_media,'resolve',lambda url,refs:{'items':[{**refs[0],'url':'https://mpvideo.qpic.cn/a.mp4?vid=one','media_access':'direct_candidate','resolution_method':'original_article_browser_dom'}]})
    asset=isolated/'video.mp4';asset.write_bytes(b'video-fixture')
    monkeypatch.setattr(media_acquisition,'acquire_http_asset',lambda *a,**kw:{'path':str(asset),'sha256':file_sha(asset),'source_url':a[1],'status':'acquired','type':'video'})
    sends=[]
    def process(asset,component,**kw):
        if kw.get('cached'): return {**kw['cached'],'reused':True}
        sends.append(1)
        return {'status':'acquired','text':'真实语音','input_sha256':asset['sha256'],
                'content_locators':[{'start':0,'end':4,'start_s':0,'end_s':30}]}
    monkeypatch.setattr(media_execution,'process_local_asset',process)
    request=DetailRequest(URL,platform='微信',options={'include_transcript':True,'_detail_stage':'collection','wechat_checkpoint_id':'native-times'})
    collected=strategy.collect(request)
    result=strategy.enhance_detail(request,collected)
    assert result['transcript_metadata']['status']=='aligned'
    assert result['transcript_metadata']['segments'][0]['media_locator']=='wechat:video:0'
    assert result['transcript_metadata']['segments'][0]['end_s']==30
    again=strategy.enhance_detail(request,collected)
    assert again['transcript_result']['request_complete'] and len(sends)==1


@pytest.mark.parametrize("coverage,complete", [("complete_selectable_text", True), ("partial", False), (None, False)])
def test_attachment_coverage_explicit_and_fixed_evidence(isolated, monkeypatch, coverage, complete):
    from kr_core import DetailRequest
    from collectors.platform import wechat
    from detail_strategies import wechat as strategy
    from runtime import pdf_text
    from kr_core.evidence_store import EvidenceStore

    monkeypatch.setattr(wechat, "collect_article", lambda *a, **k: wechat.parse_html(ARTICLE, URL))
    monkeypatch.setattr(
        pdf_text,
        "fetch_pdf_document",
        lambda *a, **k: {
            "text_extractable": True,
            "input_sha256": "b" * 64,
            "text_probe": {"text": "附件正文", "coverage": coverage, "locators": [{"page": 1, "start": 0, "end": 4}]},
            "pdf_processing": {"request_satisfied": complete,
                "page_ledger": [{"page": 1, "state": "text_layer_read" if complete else "blank_or_scan_unresolved"}]},
        },
    )
    data = strategy.collect(DetailRequest(URL, platform="微信", options={"wechat": {"include_attachments": True}}))
    attachment = data["attachment_results"][0]
    assert attachment["request_complete"] is complete
    assert attachment["provenance"]["discovered_from"] == URL
    ref = next(r for r in attachment["evidence_assets"] if r["kind"] == "content")
    assert EvidenceStore().assets.fetch(ref["version_id"])["text"] == "附件正文"
    ledger = next(r for r in attachment["evidence_assets"] if r["kind"] == "document_receipt")
    assert json.loads(EvidenceStore().assets.fetch(ledger["version_id"])["text"])["request_satisfied"] is complete
    assert attachment["content_locators"][0]["page"] == 1
    assert data["attachment_result"]["request_complete"] is complete


def test_attachment_storage_failure_is_resumable_gap(isolated, monkeypatch):
    from kr_core import DetailRequest
    from collectors.platform import wechat
    from detail_strategies import wechat as strategy
    from runtime import pdf_text
    from kr_core.evidence_store import EvidenceStore

    monkeypatch.setattr(wechat, "collect_article", lambda *a, **k: wechat.parse_html(ARTICLE, URL))
    monkeypatch.setattr(
        pdf_text,
        "fetch_pdf_document",
        lambda *a, **k: {
            "text_extractable": True,
            "text_probe": {"text": "附件正文", "coverage": "complete_selectable_text"},
        },
    )

    def failed(self, **kwargs):
        kwargs["response"]["evidence_storage_gaps"] = [{"part": "repository", "reason": "OSError"}]

    monkeypatch.setattr(EvidenceStore, "record_detail", failed)
    data = strategy.collect(DetailRequest(URL, platform="微信", options={"wechat": {"include_attachments": True}}))
    assert data["status"] == "partial" and not data["attachment_results"][0]["request_complete"]
    assert data["attachment_result"]["processed_attachments"] == 0


def test_retired_sogou_never_calls_browser_or_network(monkeypatch):
    from collectors.platform import wechat
    from runtime import opencli_bridge
    from use_cases.wechat_search import execute
    from kr_core.search_cache import SearchCache
    monkeypatch.setattr(opencli_bridge, "invoke_page", lambda **k: pytest.fail("retired source must not execute"))
    url = "https://weixin.sogou.com/link?url=old"
    assert not wechat.matches_url(url)
    assert wechat.collect_article(url)["failure_type"] == "unsupported_url"
    assert execute("研究",source="sogou",cache=SearchCache(),record_evidence=lambda *a:None)["error"]["type"] == "source_retired"


def test_article_embedded_media_never_silently_disappears():
    from collectors.platform.wechat import parse_html

    data = parse_html(
        '<div id="js_content"><p>视频</p><mp-video vid="v1"></mp-video><mpvoice voice_encode_fileid="a1"></mpvoice></div>',
        URL,
    )
    assert {r["type"] for r in data["media_manifest"]} == {"audio", "video"}
    assert all(r["status"] == "unresolved" for r in data["media_manifest"])


def test_registry_search_uses_same_public_entry_and_keeps_source_receipt(monkeypatch):
    from kr_core import SearchRequest
    from platform_adapters.wechat_adapter import WechatAdapter
    from runtime import application

    calls = []

    def invoke(name, args):
        calls.append((name, args))
        return {
            "items": [{"title": "研究", "url": URL}],
            "status": "partial",
            "metadata": {"source_outcomes": [{"source": "weread"}]},
        }

    monkeypatch.setattr(application, "invoke", invoke)
    result = WechatAdapter().search(SearchRequest("研究", limit=2, page=1, options={"source": "weread"}))
    assert calls == [("search_wechat_articles", {"query": "研究", "limit": 2, "page": 1, "source": "weread"})]
    assert result.metadata["status"] == "partial" and result.items[0].url == URL


def test_explicit_refresh_bypasses_stale_failure_only_when_requested():
    from use_cases.content_detail import request_content_detail

    requests = []
    for refresh in (False, True):
        request_content_detail(
            URL,
            infer_platform=lambda u: "微信",
            execute_sync=lambda r: requests.append(r) or {},
            wechat={"refresh": refresh},
            include_transcript=False,
            include_comments=False,
            include_ocr=False,
        )
    assert requests[0].options.get("resume_checkpoint") is None
    assert requests[1].options["resume_checkpoint"] is True


def test_serialized_checkpoint_reuses_real_download_after_identity_key_reordering(isolated, monkeypatch):
    import json
    import hashlib
    from PIL import Image
    from kr_core import DetailRequest
    from collectors.platform import wechat
    from detail_strategies import wechat as strategy
    from runtime.media_execution import processing_identity
    from runtime.model_roles import CLOUD_OCR_COMPONENT

    monkeypatch.setattr(wechat, "collect_article", lambda *a, **k: wechat.parse_html(ARTICLE, URL))
    request = DetailRequest(
        URL,
        platform="微信",
        options={
            "include_ocr": True,
            "_detail_stage": "collection",
            "wechat_checkpoint_id": "serialized",
            "wechat": {"image_indexes": [0]},
        },
    )
    collected = strategy.collect(request)
    path = isolated / "media/wechat/legacy-candidate/image.png"
    path.parent.mkdir(parents=True)
    Image.new("RGB", (16, 16), "white").save(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    previous = {
        "type": "image",
        "status": "acquired",
        "path": str(path),
        "source_url": collected["images"][0]["url"],
        "sha256": digest,
    }
    collected.update(
        downloaded_media=[previous],
        ocr={
            "results": [
                {
                    "status": "acquired",
                    "text": "已识别原图",
                    "input_sha256": digest,
                    "processing_identity": processing_identity(CLOUD_OCR_COMPONENT),
                }
            ]
        },
    )
    # 模拟实际任务的JSON排序，而非只在同一进程中传原字典。
    collected = json.loads(json.dumps(collected, sort_keys=True))
    strategy._checkpoint(request).write(collected)
    monkeypatch.setattr("httpx.Client.send", lambda *a, **k: pytest.fail("must reuse downloaded original"))
    monkeypatch.setattr("runtime.optional_media.run_optional", lambda *a, **k: pytest.fail("must reuse successful OCR"))
    result = strategy.enhance_detail(request, collected)
    assert result["ocr"]["request_complete"] and result["ocr"]["results"][0]["reused"]


@pytest.mark.parametrize("location", ["http://mp.weixin.qq.com/s/sample", "http://evil.example/s/sample"])
def test_retirement_restores_unconditional_https_downgrade_guard(monkeypatch, location):
    from urllib.parse import urlsplit
    from generic_web import url_policy as policy
    monkeypatch.setattr(policy,"public_addresses",lambda u,**k:(urlsplit(u),urlsplit(u).hostname,["93.184.216.34"]))
    monkeypatch.setattr("runtime.proxy_config.get_httpx_proxy",lambda:None)
    actual=httpx.Client
    sent=[]
    def respond(req):
        sent.append(str(req.url))
        return httpx.Response(302,headers={"location":location})
    monkeypatch.setattr(policy.httpx,"Client",lambda **kw:actual(transport=httpx.MockTransport(respond),**kw))
    with pytest.raises(policy.URLPolicyError,match="downgrade"):
        policy.guarded_fetch("https://mp.weixin.qq.com/s/sample")
    assert len(sent)==1
