"""本轮公共边界的失败/恢复与证据保留检查；不使用真实账号或云调用。"""

from io import BytesIO
import json

import pytest


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for key, part in {
        "KR_STATE_DIR": "state",
        "KR_EVIDENCE_ROOT": "assets",
        "KR_MEDIA_CACHE_DIR": "media",
        "KR_RUNTIME_LEASE_DB_PATH": "leases.sqlite3",
    }.items():
        monkeypatch.setenv(key, str(tmp_path / part))
    from runtime import leases

    monkeypatch.setattr(leases, "_COORDINATOR", None)


def blank_pdf(count=25):
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(count):
        writer.add_blank_page(width=200, height=200)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_pdf_25_missing_pages_batch_and_reuse(monkeypatch):
    from runtime import pdf_text, cloud_ocr

    calls = []

    def recognize(source, **kwargs):
        batch = kwargs["page_numbers"]
        calls.append(batch)
        for page in batch:
            kwargs["on_progress"]({"stage": "sending", "page": page})
            kwargs["on_progress"](
                {
                    "stage": "completed",
                    "page": page,
                    "block": {"page": page, "text": f"page {page}"},
                    "usage": {"total_tokens": 1},
                }
            )
        return {"document_blocks": [], "usage": {"total_tokens": len(batch)}, "page_errors": []}

    monkeypatch.setattr(cloud_ocr, "run_cloud_ocr", recognize)
    original = blank_pdf()
    first = pdf_text.process_pdf(original)
    second = pdf_text.process_pdf(original)
    assert list(map(len, calls)) == [20, 5]
    assert first["ocr_usage"]["total_tokens"] == 25
    assert first["request_satisfied"] and second["processing_reused"]
    assert first["page_ledger"][24]["state"] == "ocr_complete"
    assert first["original_asset"]["sha256"] == first["input_sha256"]


def test_pdf_crash_keeps_success_and_unknown_without_replay(monkeypatch):
    from runtime import pdf_text, cloud_ocr
    from runtime.execution_context import ExecutionStopped

    calls = []

    def crash(source, **kwargs):
        calls.append(kwargs["page_numbers"])
        progress = kwargs["on_progress"]
        progress({"stage": "sending", "page": 1})
        progress({"stage": "completed", "page": 1, "block": {"page": 1, "text": "success"}, "usage": {}})
        progress({"stage": "sending", "page": 2})
        raise ExecutionStopped("cancelled")

    monkeypatch.setattr(cloud_ocr, "run_cloud_ocr", crash)
    original = blank_pdf(3)
    with pytest.raises(ExecutionStopped):
        pdf_text.process_pdf(original)

    def resume(source, **kwargs):
        calls.append(kwargs["page_numbers"])
        return {"document_blocks": [{"page": 3, "text": "pending done"}], "page_errors": []}

    monkeypatch.setattr(cloud_ocr, "run_cloud_ocr", resume)
    result = pdf_text.process_pdf(original)
    assert calls == [[1, 2, 3], [3]]
    assert result["ocr_pages"] == [1, 3] and result["resume"]["unknown_pages"] == [2]
    assert "success" in result["text"] and not result["request_satisfied"]


def test_pdf_selection_permissions_and_no_store(monkeypatch, tmp_path):
    from runtime import pdf_text, cloud_ocr

    monkeypatch.setattr(cloud_ocr, "run_cloud_ocr", lambda *a, **k: pytest.fail("OCR explicitly forbidden"))
    result = pdf_text.process_pdf(blank_pdf(4), page_numbers=[4, 2], allow_ocr=False, storage_allowed=False)
    assert result["requested_pages"] == [2, 4] and not result["source_pages_exhausted"]
    assert result["resume"]["pending_pages"] == [2, 4]
    assert result["original_asset"]["availability"] == "not_persisted_by_policy"
    assert not (tmp_path / "assets").exists()
    with pytest.raises(ValueError, match="not_authorized"):
        pdf_text.process_pdf(blank_pdf(), read_allowed=False)
    with pytest.raises(ValueError, match="page_numbers"):
        pdf_text.process_pdf(blank_pdf(), page_numbers=[26], storage_allowed=False)


def test_comments_keep_image_only_and_unknown_parent(tmp_path):
    from runtime.comments_contract import normalize_comments
    from runtime.comment_media import resolve_media_source

    data = {
        "comments": [
            {"id": "1", "content": '<img src="https://example.org/chart.png?signature=secret">'},
            {"id": "2", "content": "reply", "is_reply": True, "root_comment_id": "1"},
        ]
    }
    normalize_comments(data, platform="知乎", source_url="https://www.zhihu.com/answer/1")
    image = data["comments"][0]["media"][0]
    assert image["url"].startswith("urn:kr:comment-media:")
    assert resolve_media_source(image["url"], source_url="https://www.zhihu.com/answer/1").endswith("signature=secret")
    assert data["comments"][1]["parent_comment_id"] is None
    assert data["comments"][1]["relationship_confidence"] == "unknown"
    assert not data["comments_complete"]
    with pytest.raises(ValueError, match="expired_resource"):
        resolve_media_source(image["url"], source_url="https://www.zhihu.com/answer/2")


def test_comment_view_retains_short_counterexample_and_parent_and_originals():
    from runtime.comments_view import comment_view

    rows = [
        {"comment_id": "1", "content": "root"},
        *[{"comment_id": str(i), "content": "赞同，非常好"} for i in range(2, 30)],
        {
            "comment_id": "30",
            "parent_comment_id": "1",
            "is_reply": True,
            "content": "不对：v2 fails, https://example.org/issue",
        },
    ]
    before = json.dumps(rows)
    result = comment_view(rows, query="v2 fails", limit=2)
    assert any(row["comment_id"] == "30" for row in result["items"])
    assert any(row["comment_id"] == "1" for row in result["items"])
    assert result["folded_count"] > 0 and json.dumps(rows) == before


def test_comments_fixed_version_keeps_unselected_and_stream_ids():
    from runtime.comments_contract import normalize_comments
    from kr_core.evidence_store import EvidenceStore
    from kr_core.evidence_assets import EvidenceAssets

    data = {
        "comments": [
            {"comment_id": "1", "discussion_kind": "issue_comments", "body": "原始正文"},
            {"comment_id": "1", "discussion_kind": "reviews", "body": "独立审核正文"},
        ]
    }
    normalize_comments(data, platform="GitHub", source_url="https://github.com/a/b/issues/1")
    EvidenceStore().record_detail(platform="GitHub", url="https://github.com/a/b/issues/1", response=data)
    ref = next(row for row in data["evidence_assets"] if row["kind"] == "comments")
    text = EvidenceAssets().fetch(ref["version_id"])["text"]
    assert "原始正文" in text and "独立审核正文" in text
    assert len(data["comments"]) == 2


def test_youtube_fetches_reply_pages_and_image_reference(monkeypatch):
    from collectors.platform import youtube

    calls = []
    monkeypatch.setattr(youtube, "youtube_api_key", lambda: "fixture")

    def get(resource, params):
        calls.append((resource, params))
        if resource == "commentThreads":
            return {
                "items": [
                    {
                        "id": "thread-not-comment",
                        "snippet": {
                            "topLevelComment": {
                                "id": "root",
                                "snippet": {"textOriginal": "原文", "imageUrl": "https://example.org/p.png"},
                            },
                            "totalReplyCount": 2,
                        },
                    }
                ]
            }
        if not params.get("pageToken"):
            return {
                "items": [{"id": "child1", "snippet": {"textOriginal": "one", "parentId": "root"}}],
                "nextPageToken": "next",
            }
        return {"items": [{"id": "child2", "snippet": {"textOriginal": "two", "parentId": "root"}}]}

    monkeypatch.setattr(youtube, "_youtube_get", get)
    result = youtube.collect_comments("fixture", options={"root_limit": 1, "reply_limit": 2, "max_pages": 4})
    assert [row["comment_id"] for row in result["comments"]] == ["root", "child1", "child2"]
    assert len(calls) == 3 and result["comments_exhausted"] and result["comments_complete"]
    assert result["comments"][0]["images"] == ["https://example.org/p.png"]


def test_bilibili_api_rejection_is_not_empty_success(monkeypatch):
    import httpx
    from collectors.platform import bilibili

    monkeypatch.setattr(
        bilibili.httpx,
        "get",
        lambda *a, **k: httpx.Response(
            200, json={"code": -412}, request=httpx.Request("GET", "https://api.bilibili.com")
        ),
    )
    result = bilibili.collect_comments("BVfixture")
    assert not result["comments_complete"] and not result["comments_empty_confirmed"]
    assert result["comments_metadata"]["gaps"][0]["reason"] == "anti_bot_verification"


def test_wechat_reader_gap_and_native_relation():
    from collectors.platform.wechat import collect_comments

    url = "https://mp.weixin.qq.com/s/example"
    missing = collect_comments(url, invoke=lambda **kw: {"ok": True, "data": {"url": url}})
    assert missing["comments_gap"] == "reader_comments_not_exposed" and not missing["comments_complete"]
    data = {
        "url": url,
        "container_observed": True,
        "exhausted": True,
        "comments": [
            {"native_comment_id": "10", "content": "留言"},
            {"content": "作者回复", "is_reply": True, "root_index": 0},
        ],
    }
    result = collect_comments(url, invoke=lambda **kw: {"ok": True, "data": data})
    assert result["comments"][1]["parent_comment_id"] == "10"
    assert result["comments"][1]["id_scope"] == "snapshot" and result["comments_complete"]
    wrong = collect_comments(
        url, invoke=lambda **kw: {"ok": True, "data": {**data, "url": "https://mp.weixin.qq.com/s/other"}}
    )
    assert wrong["comments_gap"] == "page_identity_changed"


def test_cross_driver_site_lease_uses_same_key():
    from runtime.browser_channel import browser_site_key, BorrowedBrowser
    from runtime.leases import get_runtime_lease_coordinator

    assert browser_site_key("kr:xiaohongshu") == browser_site_key("xhs")
    assert browser_site_key("wechat-weread") == browser_site_key("wechat")
    with BorrowedBrowser("profile1", "wechat", "gen", driver="opencli").lease("detail"):
        assert len(get_runtime_lease_coordinator().resource_leases("browser_site", "daily-chrome:wechat")) == 1
        with pytest.raises(RuntimeError, match="busy"):
            with BorrowedBrowser("profile2", "wechat-weread", "other", driver="browser_link").lease("competing"):
                pytest.fail("cross-driver second writer")


def test_comment_normalization_is_idempotent_and_does_not_extend_signed_url(monkeypatch):
    import time
    from runtime.comments_contract import normalize_comments
    from runtime.comment_media import resolve_media_source

    url = "https://www.youtube.com/watch?v=fixture"
    data = {
        "comments": [
            {
                "id": "a",
                "content": '<img src="https://example.org/img?sig=private">',
                "images": ["https://example.org/img?sig=private"],
            }
        ]
    }
    normalize_comments(data, platform="YouTube", source_url=url)
    ref = data["comments"][0]["media"][0]["url"]
    normalize_comments(data, platform="YouTube", source_url=url)
    assert len(data["comments"][0]["media"]) == 1
    later = time.time() + 22000
    monkeypatch.setattr(time, "time", lambda: later)
    normalize_comments(data, platform="YouTube", source_url=url)
    with pytest.raises(ValueError, match="expired_resource"):
        resolve_media_source(ref, source_url=url)

    # 同一原始位置被平台重新观察到，可以取得新的有限有效期；旧快照自身不能续期。
    fresh = {"comments":[{"id":"a","images":["https://example.org/img?sig=private"]}]}
    normalize_comments(fresh, platform="YouTube", source_url=url)
    assert fresh['comments'][0]['media'][0]['url'] == ref
    assert resolve_media_source(ref, source_url=url).endswith('sig=private')


def test_comment_media_failure_prevents_complete_text_from_masking_gap():
    from kr_core.evidence_coverage import content_coverage

    result = content_coverage(
        {
            "content": "body",
            "comments": [{"content": "all requested text"}],
            "comments_complete": True,
            "comments_metadata": {"media_gaps": [{"comment_id": "x", "reason": "expired_resource"}]},
        },
        {"include_comments": True},
    )
    assert not result["request_satisfied"] and "comments" in result["required_gaps"]
    assert result["parts"]["comments"]["missing_reason"] == "requested_comment_media_incomplete"


def test_comment_view_bounds_long_text_without_mutating_original_or_duplicating_projection():
    from runtime.comments_view import comment_view
    from runtime.agent_contracts import compact_evidence

    rows = [{"comment_id": str(n), "content": "不对，实测失败" + ("原始正文" * 3000)} for n in range(60)]
    before = json.dumps(rows)
    view = comment_view(rows, limit=50)
    assert sum(len(row["content"]) for row in view["items"]) <= 8000
    assert any(row["content_is_excerpt"] for row in view["items"]) and json.dumps(rows) == before
    compact = compact_evidence(
        {"comments": rows, "comments_view": view, "evidence_assets": [{"kind": "comments", "version_id": "fixed"}]}
    )
    assert "items" not in compact["comments_view"] and compact["disclosure"]["parts"][0]["version_id"] == "fixed"


def test_comment_processing_unknown_does_not_resend_model(monkeypatch, tmp_path):
    import hashlib
    from runtime import media_acquisition, media_execution
    from runtime.comments_contract import normalize_comments
    from runtime.comment_media import process_comment_media
    from runtime.execution_context import ExecutionStopped
    from PIL import Image

    path = tmp_path / "media" / "fixture.png"
    path.parent.mkdir(parents=True)
    Image.new("RGB", (8, 8)).save(path)
    asset = {
        "path": str(path),
        "type": "image",
        "status": "acquired",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    monkeypatch.setattr(media_acquisition, "acquire_http_asset", lambda *a, **k: asset)
    calls = []

    def send(*a, **k):
        calls.append(True)
        raise ExecutionStopped("cancelled")

    monkeypatch.setattr(media_execution, "process_local_asset", send)
    data = {"comments": [{"id": "a", "images": ["https://example.org/img"]}]}
    normalize_comments(data, platform="知乎", source_url="https://www.zhihu.com/answer/1")
    with pytest.raises(ExecutionStopped):
        process_comment_media(data, source_url="https://www.zhihu.com/answer/1", options={"include_ocr": True})
    process_comment_media(data, source_url="https://www.zhihu.com/answer/1", options={"include_ocr": True})
    assert len(calls) == 1 and data["comments_metadata"]["media_gaps"][0]["reason"] == "result_unknown"


def test_bilibili_does_not_exceed_requested_budget_when_upstream_ignores_ps(monkeypatch):
    import httpx
    from collectors.platform import bilibili

    def get(url, **kwargs):
        if url.endswith("/view"):
            data = {"aid": 1}
        elif url.endswith("/main"):
            data = {"replies": [{"rpid": 1, "rcount": 20, "content": {"message": "root"}}], "cursor": {"is_end": True}}
        else:
            data = {"replies": [{"rpid": n, "parent": 1, "content": {"message": "reply"}} for n in range(2, 30)]}
        return httpx.Response(200, json={"code": 0, "data": data}, request=httpx.Request("GET", url))

    monkeypatch.setattr(bilibili.httpx, "get", get)
    result = bilibili.collect_comments(
        "BVfixture", options={"root_limit": 1, "reply_limit": 2, "total_limit": 3, "max_pages": 4}
    )
    assert len(result["comments"]) == 3 and result["comments_complete"] and not result["comments_exhausted"]


def test_bad_document_pages_are_typed_validation_errors():
    from runtime.document_contract import selected_pages

    for pages in ([[1]], [True], [1, 1]):
        with pytest.raises(ValueError, match="invalid_document_page_numbers"):
            selected_pages(5, page_numbers=pages)


def test_youtube_keeps_budgets_when_upstream_overreturns_and_repeats(monkeypatch):
    from collectors.platform import youtube

    monkeypatch.setattr(youtube, "youtube_api_key", lambda: "fixture")

    def get(resource, params):
        if resource == "commentThreads":
            return {"items": [
                {"snippet": {"topLevelComment": {"id": str(n), "snippet": {"textOriginal": "root"}},
                             "totalReplyCount": 20}} for n in range(10)]}
        return {"items": [
            {"id": "child" + str(n // 2), "snippet": {"textOriginal": "reply", "parentId": "0"}}
            for n in range(20)]}

    monkeypatch.setattr(youtube, "_youtube_get", get)
    result = youtube.collect_comments("fixture", options={"root_limit": 1, "reply_limit": 2, "total_limit": 3})
    assert [row["comment_id"] for row in result["comments"]] == ["0", "child0", "child1"]
    assert result["comments_complete"] and not result["comments_exhausted"]


def test_comment_media_checkpoint_is_scoped_to_discussion_stream(monkeypatch, tmp_path):
    import hashlib
    from PIL import Image
    from runtime import media_acquisition, media_execution
    from runtime.comment_media import process_comment_media
    from runtime.comments_contract import normalize_comments

    image = tmp_path / "image.png"
    Image.new("RGB", (8, 8)).save(image)
    asset = {"path": str(image), "sha256": hashlib.sha256(image.read_bytes()).hexdigest(), "type": "image"}
    monkeypatch.setattr(media_acquisition, "acquire_http_asset", lambda *a, **k: asset)
    cached_arguments = []

    def recognize(*a, **kwargs):
        cached_arguments.append(kwargs.get("cached"))
        return {"status": "ok", "text": "original recognized"}

    monkeypatch.setattr(media_execution, "process_local_asset", recognize)
    data = {"comments": [
        {"id": "1", "discussion_kind": stream, "images": ["https://example.org/same.png"]}
        for stream in ("issue_comments", "reviews")]}
    url = "https://github.com/a/b/pull/1"
    normalize_comments(data, platform="GitHub", source_url=url)
    process_comment_media(data, source_url=url, options={"include_ocr": True})
    assert cached_arguments == [None, None]
    assert {row["discussion_kind"] for row in data["comment_media_ledger"]} == {"issue_comments", "reviews"}


def test_comment_only_enhancement_never_requests_zhihu_body_ocr(monkeypatch):
    from types import SimpleNamespace
    from detail_strategies import zhihu
    from runtime import media_acquisition

    monkeypatch.setattr(media_acquisition, "acquire_http_asset", lambda *a, **k: pytest.fail("body OCR not requested"))
    monkeypatch.setattr(zhihu, "_checkpoint", lambda *a: SimpleNamespace(read=lambda: {}))
    request = SimpleNamespace(options={"include_ocr": False}, auto_multimodal=True, enable_deep_analysis=False)
    result = zhihu.enhance_detail(request, {
        "body_verified": True, "collection_identity": {"version": "fixture"},
        "media_manifest": [{"type": "image", "url": "https://p.zhimg.com/image.png"}]})
    assert result["enhancement_status"] == "ok" and "ocr" not in result


def test_public_detail_document_and_comment_scope_reaches_durable_submission(monkeypatch):
    from use_cases import content_detail
    captured = []

    def submit(request, **kwargs):
        captured.append(request)
        return {"task_id": "fixture"}, {"status": "completed", "result": {"scope_received": True}}

    monkeypatch.setattr(content_detail, "submit_detail_task", submit)
    result = content_detail.request_content_detail(
        "https://example.org/paper.pdf", infer_platform=lambda _: "网页", execute_sync=lambda _: pytest.fail("must be durable"),
        include_ocr=False, include_comments=True, document={"page_numbers": [2, 4], "storage_allowed": False},
        comments={"root_limit": 3, "reply_limit": 1, "include_ocr": False, "max_pages": 2})
    assert result["scope_received"]
    assert captured[0].options["document"] == {"page_numbers": [2, 4], "storage_allowed": False, "include_ocr": False}
    assert captured[0].options["comments"]["reply_limit"] == 1
    assert captured[0].options["comments"]["max_pages"] == 2


def test_web_pdf_no_store_forbids_persistent_processing_cache(monkeypatch):
    import httpx
    from generic_web import collector
    from generic_web.models import GenericWebRequest

    monkeypatch.setattr(collector, "public_addresses", lambda *a, **k: None)
    monkeypatch.setattr(collector, "guarded_fetch", lambda url, **k: httpx.Response(
        200, content=b"%PDF-fixture", headers={"content-type": "application/pdf", "cache-control": "no-store"},
        request=httpx.Request("GET", url)))
    calls = []

    def process(source, **kwargs):
        calls.append(kwargs)
        return {"text": "fixture full text", "document_blocks": [], "coverage": "complete_selectable_text"}

    monkeypatch.setattr("runtime.pdf_text.process_pdf", process)
    collector.collect_url(GenericWebRequest("https://example.org/a.pdf", use_jina=False))
    assert len(calls) == 1 and calls[0]["cache_allowed"] is False


def test_youtube_failed_chosen_proxy_never_falls_back_to_direct(monkeypatch):
    import httpx
    from collectors.platform import youtube
    routes = []

    class Client:
        def __init__(self, **kwargs):
            routes.append(kwargs.get("proxy"))
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, *args, **kwargs): raise httpx.ConnectError("fixture TLS EOF")

    monkeypatch.setattr(youtube.httpx, "Client", Client)
    with pytest.raises(httpx.ConnectError):
        youtube._youtube_get_once("https://www.googleapis.com/youtube/v3/comments", {},
                                 api_key="fixture", timeout_s=5, proxy="http://localhost:7897")
    assert routes == ["http://localhost:7897"]


def test_document_receipt_is_fixed_and_not_returned_as_body_search():
    from kr_core.evidence_store import EvidenceStore
    from kr_core.evidence_assets import EvidenceAssets

    data = {"content": "plain body", "document": {"page_ledger": [{"page": 1, "state": "text_layer_read"}],
                                                "input_sha256": "fixture"}}
    EvidenceStore().record_detail(platform="网页", url="https://example.org/doc.pdf", response=data)
    reference = next(row for row in data["evidence_assets"] if row["kind"] == "document_receipt")
    assets = EvidenceAssets()
    assert 'text_layer_read' in assets.fetch(reference["version_id"])["text"]
    assert not any(row.get("kind") == "document_receipt" for row in assets.search("text_layer_read"))


def test_academic_permissions_are_checked_before_cached_body(monkeypatch):
    from types import SimpleNamespace
    from use_cases import academic_fulltext
    monkeypatch.setattr("academic_providers.identity.load_work", lambda _: SimpleNamespace(raw={"read_allowed": False}))
    monkeypatch.setattr(academic_fulltext, "_reuse", lambda _: pytest.fail("must reject before cache"))
    with pytest.raises(ValueError, match="permission_denied"):
        academic_fulltext.execute({"work_reference": "fixture", "timeout_s": 10})


def test_normalized_comment_manifest_is_not_rediscovered_after_url_disclosure():
    from runtime.comments_contract import normalize_comments
    data={'comments':[{'id':'image-comment','content':'','images':['https://example.org/image.webp?sqp=a==&rs=fixture']} ]}
    source='https://www.youtube.com/watch?v=fixture'
    normalize_comments(data,platform='YouTube',source_url=source)
    first=list(data['comments'][0]['media'])
    data['comments'][0]['images']=['https://example.org/image.webp?sqp=a%3D%3D&rs=fixture']
    normalize_comments(data,platform='YouTube',source_url=source)
    assert len(data['comments'][0]['media'])==1
    assert data['comments'][0]['media'][0]['url']==first[0]['url']


def test_anonymous_browser_failure_closes_owned_browser_and_releases_resource(monkeypatch):
    from types import SimpleNamespace
    from generic_web import browser_runtime
    from runtime.leases import get_runtime_lease_coordinator
    from runtime.resource_concurrency import ResourceConcurrencyPolicy
    closed=[]
    browser=SimpleNamespace(close=lambda:closed.append(True))
    monkeypatch.setattr(browser_runtime,'launch_options',lambda:{'headless':True})
    playwright=SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kwargs:browser))
    assert ResourceConcurrencyPolicy.from_env().limit_for('anonymous_browser')==2
    with pytest.raises(ValueError,match='site_parse_failed'):
        with browser_runtime.owned_sync_browser(playwright):
            assert get_runtime_lease_coordinator().resource_leases('runtime_resource','anonymous_browser')
            raise ValueError('site_parse_failed')
    assert closed==[True]
    assert not get_runtime_lease_coordinator().resource_leases('runtime_resource','anonymous_browser')


def test_pdf_acceptance_requires_document_ledger_and_rejects_unknown_pages():
    from scripts.verify_architecture_chain import document_request_satisfied
    document={'request_satisfied':True,'page_ledger':[{'page':1,'state':'text_layer_read'}],
              'resume':{'pending_pages':[],'unknown_pages':[]}}
    assert document_request_satisfied({'document':document,'content_coverage':{'parts':{}}})
    assert document_request_satisfied(document)
    assert not document_request_satisfied({'content_coverage':{'request_satisfied':True}})
    assert not document_request_satisfied({'document':dict(document,resume={'unknown_pages':[1]})})
    assert not document_request_satisfied({'document':document,'error':'failed'})


def test_wechat_attachment_uses_document_scope_and_preserves_fixed_page_receipt(monkeypatch):
    import httpx
    from kr_core import DetailRequest
    from kr_core.evidence_assets import EvidenceAssets
    from collectors.platform import wechat
    from detail_strategies import wechat as strategy
    from runtime import pdf_text
    url='https://mp.weixin.qq.com/s/attachment-fixture'
    html='<h1 id="activity-name">附件</h1><div id="js_content"><a href="https://example.org/paper.pdf">论文</a></div>'
    monkeypatch.setattr(wechat,'collect_article',lambda *a,**kw:wechat.parse_html(html,url))
    monkeypatch.setattr('generic_web.url_policy.guarded_fetch',lambda target,**kw:httpx.Response(
        200,content=b'%PDF-fixture',request=httpx.Request('GET',target)))
    received=[]
    def process(source,**kw):
        received.append(kw)
        return {'text':'第二页正文','request_satisfied':True,'page_count':2,'requested_pages':[2],
                'page_ledger':[{'page':2,'state':'text_layer_read'}],
                'coverage':'complete_selectable_text','locators':[{'page':2,'start':0,'end':5}]}
    monkeypatch.setattr(pdf_text,'process_pdf',process)
    options={'wechat':{'include_attachments':True},'include_ocr':True,
             'document':{'page_numbers':[2],'storage_allowed':False,'include_ocr':False}}
    request=DetailRequest(url,platform='微信',options=options)
    result=strategy.collect(request)
    assert len(received)==1 and received[0]['page_numbers']==[2]
    assert received[0]['allow_ocr'] is False and received[0]['storage_allowed'] is False
    item=result['attachment_results'][0]
    assert item['request_complete'] and item['document']['requested_pages']==[2]
    ref=next(r for r in item['evidence_assets'] if r['kind']=='document_receipt')
    assert json.loads(EvidenceAssets(read_only=True).fetch(ref['version_id'])['text'])['page_ledger'][0]['page']==2
    changed=DetailRequest(url,platform='微信',options={**options,'document':{'page_numbers':[1]}})
    assert not strategy._checkpoint(changed).read()


def test_wechat_reader_load_failure_never_claims_empty_or_retries():
    from collectors.platform.wechat import collect_comments
    calls=[]
    url='https://mp.weixin.qq.com/s/example'
    def invoke(**kwargs):
        calls.append(kwargs)
        return {'ok':True,'data':{'url':url,'container_observed':True,'empty_confirmed':True,
            'exhausted':True,'reader_diagnostic':{'visible_load_failure':True}}}
    result=collect_comments(url,invoke=invoke)
    assert result['comments_gap']=='reader_comments_load_failed'
    assert not result['comments_empty_confirmed'] and not result['comments_complete']
    assert len(calls)==1 and len(calls[0]['steps'])==2
    assert result['comments_metadata']['automatic_retry'] is False


def test_wechat_hidden_container_failure_does_not_prove_reader_rejection():
    from collectors.platform.wechat import collect_comments
    url='https://mp.weixin.qq.com/s/example'
    result=collect_comments(url,invoke=lambda **kw:{'ok':True,'data':{'url':url,
        'reader_diagnostic':{'hidden_load_failure':True,'hidden_container_count':1}}})
    assert result['comments_gap']=='reader_comments_not_exposed'
    assert not result['comments_empty_confirmed']


def test_wechat_normal_reader_client_prompt_is_not_login_or_zero():
    from collectors.platform.wechat import collect_comments
    url='https://mp.weixin.qq.com/s/example'
    result=collect_comments(url,invoke=lambda **kw:{'ok':True,'data':{'url':url,
        'reader_diagnostic':{'client_prompt':True}}})
    assert result['comments_gap']=='reader_client_required'
    assert not result['comments_empty_confirmed'] and not result['comments_complete']


def test_zhihu_comments_use_normal_reader_primary_without_api_fallback(monkeypatch):
    from collectors.platform.zhihu import _reader_comments
    calls=[]
    monkeypatch.setattr('runtime.opencli_bridge.invoke_page',lambda **kw:calls.append(kw) or {'ok':False,'code':'SECURITY_BLOCK'})
    result=_reader_comments(url='https://www.zhihu.com/question/123/answer/456')
    assert result['code']=='SECURITY_BLOCK' and len(calls)==1
    assert calls[0]['isolated_work'] and calls[0]['session']=='zhihu'
    assert 'fetch(' not in calls[0]['expression']
    assert 'new AbortController' in calls[0]['expression']


def test_opencli_reuses_outer_site_lease_without_unlocking_other_tasks(monkeypatch,tmp_path):
    import json
    from types import SimpleNamespace
    from runtime import opencli_bridge, browser_channel
    from runtime.browser_channel import BorrowedBrowser
    monkeypatch.setenv('KR_RUNTIME_LEASE_DB_PATH',str(tmp_path/'leases.sqlite3'))
    (tmp_path/'package.json').write_text(json.dumps({'version':'2.0.2'}))
    monkeypatch.setattr(opencli_bridge,'connection_config',lambda:{'root':str(tmp_path),'context_id':'daily','state_root':tmp_path})
    monkeypatch.setattr(opencli_bridge,'ensure_daemon',lambda *a,**kw:'')
    monkeypatch.setattr(browser_channel,'ensure_borrowed_browser',lambda *a,**kw:{'ok':True})
    calls=[]
    monkeypatch.setattr('runtime.process.silent_subprocess_run',lambda *a,**kw:calls.append(kw) or SimpleNamespace(stdout='{"ok":true,"data":{}}'))
    with BorrowedBrowser('daily','zhihu','gen').lease('outer'):
        result=opencli_bridge.invoke_page(session='zhihu',operation='comments',expression='({})')
        assert result['ok'] and len(calls)==1
        import contextvars
        def independent():
            with BorrowedBrowser('other','zhihu','other').lease('other'):
                pytest.fail('must not enter competing lease')
        with pytest.raises(RuntimeError,match='browser_session_busy'):
            contextvars.Context().run(independent)
