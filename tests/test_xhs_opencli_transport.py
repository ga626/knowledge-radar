"""固定OpenCLI合同：错误、租约、恢复与内容保留。"""
import json
import pytest
from contextlib import contextmanager
from kr_core import DetailRequest
from runtime import xhs_opencli as oc

NOTE="abcdef123456789012345678"
URL="https://www.xiaohongshu.com/explore/"+NOTE


def test_checkpoint_cross_process_compare_and_swap(tmp_path):
    import os, subprocess, sys, time
    from pathlib import Path
    code = '''
import os,sys,time
sys.path.insert(0,os.environ['PYTHONPATH'])
from pathlib import Path
from runtime.operation_checkpoint import OperationCheckpoint
root=Path(sys.argv[1]); writer=sys.argv[2]
checkpoint=OperationCheckpoint(root, {'operation':'same'}, {'identity':'same'})
(root/(writer+'.ready')).touch()
while not (root/'go').exists(): time.sleep(.01)
try:
    checkpoint.write({'writer':writer})
    (root/(writer+'.out')).write_text('written')
except ValueError as exc:
    (root/(writer+'.out')).write_text(str(exc))
'''
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
    processes = [subprocess.Popen([sys.executable, '-c', code, str(tmp_path), str(i)], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(2)]
    try:
        deadline = time.monotonic() + 8
        while len(list(tmp_path.glob('*.ready'))) != 2 and time.monotonic() < deadline:
            time.sleep(.02)
        assert len(list(tmp_path.glob('*.ready'))) == 2
    finally:
        (tmp_path / 'go').touch()
        for process in processes:
            _, err = process.communicate(timeout=10)
            assert process.returncode == 0, err.decode('utf-8')
    assert sorted(path.read_text() for path in tmp_path.glob('*.out')) == ['checkpoint_revision_conflict', 'written']
    assert json.loads(next(tmp_path.glob('*.json')).read_text(encoding='utf-8'))['revision'] == 1


def test_media_downloads_overlap_keep_order_and_obey_existing_slots(monkeypatch, tmp_path):
    from runtime import leases, resource_concurrency
    monkeypatch.setenv('KR_DATA_ROOT', str(tmp_path/'data'))
    coordinator = leases.RuntimeLeaseCoordinator(tmp_path/'leases.sqlite3')
    monkeypatch.setattr(resource_concurrency, 'get_runtime_lease_coordinator', lambda: coordinator)
    from threading import Barrier, Lock
    from PIL import Image
    import io, httpx
    from generic_web import url_policy
    content = io.BytesIO()
    Image.new('RGB', (30, 40), 'white').save(content, format='PNG')
    rendezvous, lock, active, peak = Barrier(2), Lock(), 0, 0
    def fetch(url, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            rendezvous.wait(timeout=3)
            return httpx.Response(200, request=httpx.Request('GET', url), content=content.getvalue())
        finally:
            with lock: active -= 1
    monkeypatch.setattr(url_policy, 'guarded_fetch', fetch)
    monkeypatch.setenv('KR_MEDIA_DOWNLOAD_CONCURRENCY', '2')
    manifest = [{'type':'image','url':f'https://sns-webpic.xhscdn.com/{i}.png'} for i in range(4)]
    # Selection remains bounded by the existing request budget; set it to four here.
    monkeypatch.setenv('KR_XHS_OCR_MAX_IMAGES', '4')
    progress = []
    result = oc.acquire_media(manifest, NOTE, on_progress=lambda rows: progress.append(list(rows)))
    assert [r.get('source_url') for r in result] == [r['url'] for r in manifest]
    assert all(r['status'] == 'acquired' for r in result)
    assert peak == 2 and len(progress) == 4


def test_cloud_and_local_lanes_overlap_preserve_trace_and_checkpoint_progress(monkeypatch, tmp_path):
    from threading import Barrier, Lock
    from runtime import optional_media, tool_trace
    from runtime.operation_checkpoint import OperationCheckpoint
    rendezvous = Barrier(2)
    snapshots, lock = [], Lock()
    original_write = OperationCheckpoint.write
    def checkpoint(self, data):
        with lock: snapshots.append(json.loads(json.dumps(data)))
        return original_write(self, data)
    monkeypatch.setattr(OperationCheckpoint, 'write', checkpoint)
    monkeypatch.setattr(oc, 'invoke', lambda *a, **k: {'ok':True, 'data':{
        'note_id':NOTE,'content':'正文','completed_stages':['body','media'],
        'media_manifest':[{'type':'image','url':'cover'},{'type':'video','url':'video'}]}})
    assets=[{'type':kind,'status':'acquired','path':str(tmp_path / (kind + suffix)), 'sha256':kind}
            for kind,suffix in [('image','.jpg'),('video','.mp4')]]
    monkeypatch.setattr(oc, 'acquire_media', lambda *a, **k: assets)
    tool_trace.set_current_tool_trace({'trace_id':'parent-trace','research_task_id':'task-test'})
    def model(payload):
        assert tool_trace.current_tool_trace()['trace_id'] == 'parent-trace'
        rendezvous.wait(timeout=3)
        return {'status':'acquired','text':payload['component']}
    monkeypatch.setattr(optional_media, 'run_optional', model)
    try:
        result = oc.detail(DetailRequest(url=URL,options={'include_ocr':True,'include_transcript':True}))
    finally:
        tool_trace.set_current_tool_trace({})
    assert result['enhancement_status'] == 'ok'
    assert result['ocr_text'] and result['transcript']
    # The barrier proves overlap without a flaky millisecond speed assertion.
    assert all(result['timings'][k] >= 0 for k in ('enhancement_wall_s', 'ocr_s', 'asr_s'))
    assert any(s.get('downloaded_media') and not s.get('ocr') for s in snapshots)
    assert any(bool(s.get('ocr')) != bool(s.get('transcript_result')) for s in snapshots)


def test_ocr_failure_keeps_usage_in_detail(monkeypatch, tmp_path):
    from runtime import optional_media
    from runtime.cloud_ocr import CloudOcrError
    monkeypatch.setattr(oc, 'invoke', lambda *a, **k: {'ok':True,'data':{
        'note_id':NOTE,'content':'正文','completed_stages':['body','media']}})
    monkeypatch.setattr(oc, 'acquire_media', lambda *a, **k: [
        {'type':'image','status':'acquired','path':str(tmp_path/'cover.jpg'),'sha256':'image'}])
    def model(_):
        raise CloudOcrError('cloud_ocr_output_truncated', diagnostics={'finish_reason':'length','usage':{'total_tokens':5396}})
    monkeypatch.setattr(optional_media, 'run_optional', model)
    result=oc.detail(DetailRequest(url=URL, options={'include_ocr':True}))
    assert result['ocr']['results'][0]['diagnostics']['usage']['total_tokens']==5396
    assert result['enhancement_status']=='partial'


def test_successful_enhancements_resume_without_model_calls(monkeypatch, tmp_path):
    from runtime import optional_media
    for kind in ('image', 'video'):
        (tmp_path / kind).write_text(kind, encoding='utf-8')
    monkeypatch.setattr(oc, 'invoke', lambda *a, **k: {'ok':True,'data':{
        'note_id':NOTE,'content':'正文','completed_stages':['body','media'],
        'media_manifest':[{'type':'image','url':'cover'},{'type':'video','url':'video'}]}})
    monkeypatch.setattr(oc, 'acquire_media', lambda *a, **k: [
        {'type':kind,'status':'acquired','path':str(tmp_path/kind),'sha256':optional_media.file_sha(tmp_path/kind)} for kind in ('image','video')])
    monkeypatch.setattr(optional_media, 'run_optional', lambda p:{'status':'acquired','text':p['component']})
    options={'include_ocr':True,'include_transcript':True}
    first=oc.detail(DetailRequest(url=URL,options=options))
    monkeypatch.setattr(oc, 'invoke', lambda *a, **k: pytest.fail('browser must be reused'))
    monkeypatch.setattr(optional_media, 'run_optional', lambda *a: pytest.fail('model must be reused'))
    result=oc.detail(DetailRequest(url=URL,options={**options,'resume_checkpoint':True}))
    assert result['transcript']==first['transcript'] and result['ocr_text']==first['ocr_text']
    assert result['ocr']['results'][0]['reused'] and result['transcript_result']['reused']


def test_unreachable_ocr_provider_stops_batch_then_resume_processes_all_requested_images(monkeypatch, tmp_path):
    from runtime import optional_media
    from runtime.cloud_ocr import CloudOcrError
    monkeypatch.setattr(oc,'invoke',lambda *a,**k:{'ok':True,'data':{
        'note_id':NOTE,'content':'正文','completed_stages':['body','media'],
        'media_manifest':[{'type':'image','url':str(i)} for i in range(3)]}})
    monkeypatch.setattr(oc,'acquire_media',lambda *a,**k:[
        {'type':'image','status':'acquired','path':str(tmp_path/str(i)),'sha256':str(i)} for i in range(3)])
    calls=[]
    def unavailable(payload):
        calls.append(payload['input_sha256'])
        raise CloudOcrError('cloud_ocr_timeout')
    monkeypatch.setattr(optional_media,'run_optional',unavailable)
    result=oc.detail(DetailRequest(url=URL,options={'include_ocr':True}))
    assert calls==['0'] and result['enhancement_status']=='partial'
    assert [r['status'] for r in result['ocr']['results']]==['failed','skipped','skipped']
    monkeypatch.setattr(oc,'invoke',lambda *a,**k:pytest.fail('body must resume'))
    monkeypatch.setattr(optional_media,'run_optional',lambda p: calls.append(p['input_sha256']) or {'status':'acquired','text':'识别结果'})
    result=oc.detail(DetailRequest(url=URL,options={'include_ocr':True,'resume_checkpoint':True}))
    assert calls==['0','0','1','2'] and result['enhancement_status']=='ok'
    assert result['ocr']['images_processed']==3


@pytest.fixture(autouse=True)
def isolated(monkeypatch,tmp_path):
    monkeypatch.setenv("KR_XHS_OPENCLI_STATE_DIR",str(tmp_path/"state"))
    monkeypatch.setenv("KR_DATA_ROOT",str(tmp_path/"data"))
    monkeypatch.setenv("KR_STATE_DIR",str(tmp_path/"runtime"))
    monkeypatch.setenv("KR_RUNTIME_LEASE_DB_PATH",str(tmp_path/"leases.sqlite3"))
    monkeypatch.setenv("KR_XHS_TIKHUB_USAGE_PATH",str(tmp_path/"paid.json"))
    (tmp_path/"disabled-policy.json").write_text('{"schema":"knowledgeradar-paid-policy/v2","providers":{"tikhub":{"enabled":false}}}',encoding="utf-8")
    monkeypatch.setenv("KR_PAID_POLICY_PATH",str(tmp_path/"disabled-policy.json"))
    monkeypatch.delenv("KR_XHS_OPENCLI_CONTEXT_ID",raising=False)


def test_signed_candidates_keep_private_handoff_without_fabricated_dates(monkeypatch):
    monkeypatch.setattr(oc,"invoke",lambda *a,**k:{"ok":True,"data":[{"title":"通勤包",
        "url":URL+"?xsec_token=private","author":"甲","published_at":"2026-01-01"}]})
    result=oc.search("通勤包",3,"image")
    assert "private" not in result["items"][0]["url"]
    assert not result["items"][0]["published_at"]
    from collectors.platform.xiaohongshu import protected_detail_url
    assert "xsec_token=private" in protected_detail_url(URL)
    from platform_adapters.common import response_from_legacy
    assert "private" not in json.dumps(response_from_legacy("小红书",result).to_mcp_dict())


def test_search_does_not_fabricate_note_type_when_browser_did_not_return_one(monkeypatch):
    monkeypatch.setattr(oc, "invoke", lambda *a, **k: {"ok": True, "data": [
        {"title": "图文", "url": URL + "?xsec_token=private", "author": "甲"},
    ]})
    result = oc.search("通勤包", 3, "image")
    assert result['items'][0]['type'] == 'unknown'
    assert result['metadata']['filter_verification'] == 'unverified'


def test_risk_latch_stops_before_browser(monkeypatch):
    oc.latch("SECURITY_BLOCK")
    monkeypatch.setattr(oc,"_run",lambda *a,**k:pytest.fail("risk must stop"))
    assert oc.invoke("search")["request_executed"] is False


def test_legacy_entry_cannot_dispatch_account_pool(monkeypatch):
    from collectors.platform import xiaohongshu as xhs
    monkeypatch.setenv("KR_XHS_TRANSPORT","current") # stale config has no effect
    monkeypatch.setattr(oc,"search",lambda *a,**k:{"items":[],"metadata":{"transport":"opencli"}})
    assert not hasattr(xhs,"_search_xhs_account_task")
    assert xhs.legacy_search_xiaohongshu("test")["metadata"]["transport"]=="opencli"


def test_partial_body_survives_comment_restriction_and_is_checkpointed(monkeypatch):
    monkeypatch.setattr(oc,"invoke",lambda *a,**k:oc._failure("SECURITY_BLOCK",data={
        "title":"包","content":"正文","note_id":NOTE,"completed_stages":["body"]}))
    result=oc.detail(DetailRequest(url=URL,options={"include_comments":True}))
    assert result["content"]=="正文"
    assert result["failure_type"]=="anti_bot_verification"
    assert result["resume_checkpoint"]["stages"]==["body"]
    assert "正文" in next((oc.state_dir()/"checkpoints").glob("*.json")).read_text(encoding="utf-8")


@pytest.mark.parametrize("url",["https://example.org/a.jpg","https://xhscdn.com.evil.test/a.jpg","file:///tmp/a",
    "https://user:password@sns-img.xhscdn.com/a.jpg"])
def test_media_rejects_untrusted_sources(url):
    with pytest.raises(ValueError):
        oc.validate_media_url(url)


def test_invalid_id_cannot_escape_media_root(tmp_path):
    with pytest.raises(ValueError):
        oc.media_destination("../../escape","image",tmp_path)


def test_missing_binding_never_launches_managed_chrome(monkeypatch):
    from runtime import chrome_manager
    monkeypatch.setattr(chrome_manager,"_ensure_chrome_debugging",lambda *_:pytest.fail("not owned"))
    assert oc.invoke("search")["failure_type"]=="opencli_daily_browser_binding_required"


def test_borrowed_browser_lease_excludes_second_writer_and_releases(monkeypatch,tmp_path):
    from runtime.browser_channel import BorrowedBrowser
    from runtime.leases import RuntimeLeaseCoordinator
    import runtime.leases as leases
    coordinator=RuntimeLeaseCoordinator(tmp_path/"isolated.sqlite3")
    monkeypatch.setattr(leases,"get_runtime_lease_coordinator",lambda:coordinator)
    browser=BorrowedBrowser("daily","kr:xiaohongshu","one")
    with browser.lease("search"):
        with pytest.raises(RuntimeError,match="busy"):
            with browser.lease("detail"):
                pytest.fail("second writer")
    with browser.lease("detail"):
        pass
    assert browser.ownership=="borrowed"


def test_models_and_download_do_not_hold_browser_lease(monkeypatch,tmp_path):
    from runtime import optional_media
    active=[]
    @contextmanager
    def lease(*_):
        active.append(True)
        try: yield
        finally: active.pop()
    from runtime.browser_channel import BorrowedBrowser
    monkeypatch.setattr(BorrowedBrowser,"lease",lease)
    monkeypatch.setenv("KR_XHS_OPENCLI_CONTEXT_ID","daily")
    monkeypatch.setattr(oc,"_run",lambda *a,**k:{"ok":True,"data":{"note_id":NOTE,
        "content":"正文","completed_stages":["body","media"]}})
    def download(*a, **kwargs):
        assert not active
        return [{"type":"image","status":"acquired","path":str(tmp_path/"image.jpg"),"sha256":"abc"}]
    def model(*a):
        assert not active
        return {"status":"ok","text":"图中文字"}
    monkeypatch.setattr(oc,"acquire_media",download)
    monkeypatch.setattr(optional_media,"run_optional",model)
    result=oc.detail(DetailRequest(url=URL,options={"include_ocr":True}))
    assert result["ocr_text"]=="图中文字"


def test_collection_returns_before_download_and_enhancement_never_reopens_browser(monkeypatch, tmp_path):
    from runtime import optional_media
    from dataclasses import replace
    calls = []
    def invoke(*args, **kwargs):
        calls.append("browser")
        return {"ok": True, "data": {"note_id": NOTE, "content": "可先阅读的正文",
            "completed_stages": ["body", "media"], "media_manifest": [{"type": "image", "url": "cover"}]}}
    def download(*args, **kwargs):
        calls.append("download")
        return [{"type": "image", "status": "acquired", "path": str(tmp_path / "image.jpg"), "sha256": "image"}]
    monkeypatch.setattr(oc, "invoke", invoke)
    monkeypatch.setattr(oc, "acquire_media", download)
    monkeypatch.setattr(optional_media, "run_optional", lambda _: {"status": "acquired", "text": "识别文字"})
    request = DetailRequest(url=URL, options={"include_ocr": True, "_detail_stage": "collection"})
    base = oc.detail(request)
    assert base["_enhancement_pending"] is True and base["content"]
    assert calls == ["browser"]
    monkeypatch.setattr(oc, "invoke", lambda *a, **k: pytest.fail("second browser collection"))
    final = oc.enhance_detail(replace(request, options={"include_ocr": True}), base)
    assert calls == ["browser", "download"]
    assert final["content"] == base["content"] and final["ocr_text"] == "识别文字"
    assert final["enhancement_status"] == "ok"


def test_late_models_and_checkpoint_cannot_overwrite_new_login_failure(monkeypatch):
    observations = []
    monkeypatch.setenv("KR_XHS_OPENCLI_CONTEXT_ID", "daily")
    monkeypatch.setattr(oc, "_observe_account", lambda state, reason="": observations.append(state))
    monkeypatch.setattr(oc, "_run", lambda *a, **k: {"ok": True, "data": {
        "note_id": NOTE, "content": "正文", "completed_stages": ["body", "media"]}})
    def download(*a, **k):
        observations.append("needs_login")
        return []
    monkeypatch.setattr(oc, "acquire_media", download)
    oc.detail(DetailRequest(url=URL, options={"include_comments": False}))
    assert observations == ["business_verified", "needs_login"]
    oc.detail(DetailRequest(url=URL, options={"include_comments": False, "resume_checkpoint": True}))
    assert observations == ["business_verified", "needs_login", "needs_login"]


def test_login_opens_once_and_releases_caller(monkeypatch):
    calls=[]
    def invoke(operation,**kwargs):
        calls.append(operation)
        if operation=="login": return {"ok":True,"data":{"authenticated_ui":False}}
        return oc._failure("AUTH_REQUIRED")
    monkeypatch.setattr(oc,"invoke",invoke)
    first=oc._recover_or_fallback(oc._failure("AUTH_REQUIRED"),"detail",note_id=NOTE)
    second=oc._recover_or_fallback(oc._failure("AUTH_REQUIRED"),"detail",note_id=NOTE)
    assert first["login_recovery"]["status"]=="waiting_for_user"
    assert calls==["login"]
    assert second["manual_action_required"]


def test_expired_login_tries_paid_and_finishes_with_reason(monkeypatch):
    from runtime import xhs_tikhub_fallback as paid
    oc._save(oc.state_dir()/"login.json",{"expires_at":0,"context":""})
    calls=[]
    monkeypatch.setattr(paid,"execute_paid_operation",lambda *a,**k:calls.append(k) or {"ok":False,"reason_code":"TIKHUB_FALLBACK_DISABLED"})
    result=oc._recover_or_fallback(oc._failure("AUTH_REQUIRED"),"detail",note_id=NOTE,task_id="r1")
    assert calls and result["manual_action_required"] is False
    assert result["status"]=="failed"


def test_auth_confirmation_requires_observed_login_and_changes_identity(monkeypatch):
    monkeypatch.setattr(oc,"invoke",lambda *a,**k:{"ok":True,"data":{"authenticated_ui":True}})
    oc.latch("SECURITY_BLOCK")
    before=oc.runtime_identity()
    assert oc.complete_login()["authenticated"]
    assert not (oc.state_dir()/"stop.json").exists()
    assert oc.runtime_identity()["auth_revision"]!=before["auth_revision"]


@pytest.mark.parametrize("failure",["AUTH_REQUIRED","SECURITY_BLOCK","CAPTCHA_REQUIRED","RATE_LIMITED","empty_results"])
def test_shared_error_classifier_retains_nonretryable_boundary(failure):
    from platform_adapters.common import response_from_legacy
    response=response_from_legacy("小红书",{"items":[],"error":failure})
    assert response.error and response.error.retryable is False
    assert "object has no attribute" not in response.error.message


def test_checkpoint_resume_skips_completed_browser_work(monkeypatch):
    request=DetailRequest(url=URL,options={"include_comments":True})
    monkeypatch.setattr(oc,"invoke",lambda *a,**k:{"ok":True,"data":{"note_id":NOTE,
        "content":"已有正文","comments":[{"text":"评论"}],"completed_stages":["body","comments","media"]}})
    monkeypatch.setattr(oc,"acquire_media",lambda *a,**k:[])
    oc.detail(request)
    request=DetailRequest(url=URL,options={"include_comments":True,"resume_checkpoint":True})
    monkeypatch.setattr(oc,"invoke",lambda *a,**k:pytest.fail("completed browser stages replayed"))
    assert oc.detail(request)["content"]=="已有正文"


def test_worker_path_does_not_import_mcp_facade(monkeypatch):
    from runtime.platform_execution import worker_detail, ensure_bindings
    ensure_bindings()
    monkeypatch.setattr(oc,"detail",lambda req:{"title":"报告","content":"可归档正文","url":req.url,"platform":"小红书"})
    import sys
    before="server" in sys.modules
    result=worker_detail(DetailRequest(url=URL,platform="小红书",auto_multimodal=False))
    assert result["content"]=="可归档正文" and result["evidence"]["source_url"]==URL
    assert ("server" in sys.modules)==before


def test_binding_requires_chrome_and_exact_connected_context(monkeypatch):
    import runpy
    from pathlib import Path
    bind = runpy.run_path(str(Path(__file__).resolve().parents[1]/"scripts/kr_opencli.py"))["bind"]
    import httpx
    with pytest.raises(ValueError, match="daily_chrome_confirmation_required"):
        bind("daily", "edge")
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, *args, **kwargs):
            return httpx.Response(200, request=httpx.Request("GET", "http://127.0.0.1:19825/status"),
                json={"extensionConnected": True, "daemonVersion": "2.0.2", "extensionVersion": "1.0.26", "contextId": "other"})
    monkeypatch.setattr(httpx, "Client", Client)
    with pytest.raises(ValueError, match="selected_browser_not_connected"):
        bind("daily", "chrome")
    assert not (oc.state_dir()/"binding.json").exists()


def test_worker_does_not_start_shared_daemon(monkeypatch):
    import httpx
    from runtime import process
    monkeypatch.setenv("KR_DURABLE_WORKER", "1")
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, *args, **kwargs): raise httpx.ConnectError("offline")
    monkeypatch.setattr(httpx, "Client", Client)
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def setsockopt(self, *args): pass
        def bind(self, *args): pass
    import socket
    monkeypatch.setattr(socket, "socket", lambda *a, **k: Socket())
    monkeypatch.setattr(process, "silent_subprocess_popen", lambda *a, **k: pytest.fail("worker must not own daemon"))
    assert oc._ensure_daemon(oc.state_dir(), "node") == "opencli_daemon_not_running"


def test_unresolved_short_link_is_explicit_failure(monkeypatch):
    from generic_web import url_policy
    from platform_adapters.xiaohongshu_adapter import prepare_detail
    monkeypatch.setattr(url_policy, "guarded_fetch", lambda *a, **k: (_ for _ in ()).throw(ValueError("invalid redirect")))
    request = prepare_detail(DetailRequest(url="https://xhslink.com/a/abc", platform="小红书"))
    assert request.options["preparation_failure"] == "short_link_resolution_failed"
    assert not oc.detail(request)["request_executed"]


def test_explicit_zero_comment_observation_does_not_become_missing():
    from kr_core.evidence_coverage import content_coverage
    result = content_coverage({"content": "正文", "comments": [], "comments_coverage": "observed_zero_comments"}, {"include_comments": True})
    assert result["parts"]["comments"]["state"] == "acquired"
    assert result["parts"]["comments"]["coverage"] == "observed_zero_comments"
    assert "comments" not in result["required_gaps"]
    unknown = content_coverage({"content": "正文", "comments": []}, {"include_comments": True})
    assert "comments" in unknown["required_gaps"]


def test_image_note_does_not_require_asr(monkeypatch):
    monkeypatch.setattr(oc, "invoke", lambda *a, **k: {"ok": True, "data": {
        "note_id": NOTE, "content": "图文", "completed_stages": ["body", "media"],
        "media_manifest": [{"type": "image", "url": "https://sns-img.xhscdn.com/a.jpg"}]}})
    monkeypatch.setattr(oc, "acquire_media", lambda *a, **k: [])
    result = oc.detail(DetailRequest(url=URL, options={"include_transcript": True}))
    assert result["transcript_result"]["status"] == "not_applicable"
    from kr_core.evidence_coverage import content_coverage
    assert "transcript" not in content_coverage(result, {"include_transcript": True})["required_gaps"]


def test_cloud_configuration_failure_is_explicit_and_stops_batch(monkeypatch):
    from runtime import optional_media
    from runtime.cloud_ocr import CloudOcrError
    monkeypatch.setattr(oc, "invoke", lambda *a, **k: {"ok": True, "data": {
        "note_id": NOTE, "content": "正文", "completed_stages": ["body", "media"]}})
    monkeypatch.setattr(oc, "acquire_media", lambda *a, **k: [
        {"type": "image", "status": "acquired", "path": "image.jpg", "sha256": str(i)} for i in range(3)])
    calls = []
    def unavailable(payload):
        calls.append(payload)
        raise CloudOcrError("siliconflow_api_key_missing")
    monkeypatch.setattr(optional_media, "run_optional", unavailable)
    result = oc.detail(DetailRequest(url=URL, options={"include_ocr": True}))
    assert len(calls) == 1
    assert result["ocr"]["results"][0]["reason"] == "siliconflow_api_key_missing"
    assert result["ocr"]["results"][1]["status"] == "skipped"
    assert result["enhancement_status"] == "partial"


def test_enhancement_failure_reasons_are_safe_and_actionable():
    from generic_web.url_policy import URLPolicyError
    assert oc._media_failure_reason(ValueError('ffmpeg_not_installed')) == 'ffmpeg_not_installed'
    assert oc._media_failure_reason(ValueError('private-path-and-secret')) == 'ValueError'
    assert oc._media_failure_reason(URLPolicyError('target DNS unavailable')) == 'target_dns_unavailable'


def test_checkpoint_rejects_late_cross_instance_overwrite(tmp_path):
    from runtime.operation_checkpoint import OperationCheckpoint
    first = OperationCheckpoint(tmp_path, {"url": URL}, {"source": "same"})
    stale = OperationCheckpoint(tmp_path, {"url": URL}, {"source": "same"})
    first.write({"content": "first"})
    with pytest.raises(ValueError, match="checkpoint_revision_conflict"):
        stale.write({"content": "late"})
    assert first.read()["content"] == "first"


def test_media_wait_does_not_erase_completed_ocr_checkpoint(monkeypatch):
    from runtime.resource_concurrency import ResourceDeferred
    request = DetailRequest(url=URL, options={"include_ocr": True, "include_transcript": True})
    base = {"note_id": NOTE, "content": "已保存正文", "completed_stages": ["body", "media"],
            "media_manifest": [{"type": "image", "url": "cover"}, {"type": "video", "url": "video"}]}
    checkpoint = oc._detail_checkpoint(request)
    checkpoint.write({**base, "ocr": {"status": "ok", "results": [
        {"status": "acquired", "text": "已识别", "input_sha256": "image"}]}, "ocr_text": "已识别"})
    def busy(*args, on_progress, **kwargs):
        on_progress([{"type": "image", "status": "acquired", "sha256": "image"}])
        raise ResourceDeferred("media_download")
    monkeypatch.setattr(oc, "acquire_media", busy)
    with pytest.raises(ResourceDeferred):
        oc.enhance_detail(request, base)
    saved = oc._detail_checkpoint(request).read()
    assert saved["ocr_text"] == "已识别"
    assert saved["content"] == base["content"]


def test_seven_images_all_requested_and_failed_download_stays_in_denominator(monkeypatch, tmp_path):
    from runtime import optional_media
    manifest=[{'type':'image','url':f'https://sns-img.xhscdn.com/signed/{i}?sig=secret{i}'} for i in range(7)]
    monkeypatch.setattr(oc,'invoke',lambda *a,**k:{'ok':True,'data':{'note_id':NOTE,'content':'正文','completed_stages':['body','media'],'media_manifest':manifest}})
    def download(kind,url,note,previous,*,item):
        if item['image_index']==4:return {'type':kind,'source_url':url,'status':'failed','reason':'media_access_denied'}
        return {'type':kind,'source_url':url,'status':'acquired','path':str(tmp_path/'image.jpg'),'sha256':str(item['image_index'])}
    monkeypatch.setattr(oc,'_acquire_media_one',download)
    monkeypatch.setattr(optional_media,'run_optional',lambda p:{'status':'acquired','text':'图片'+p['input_sha256'],'provider':'fixture'})
    data=oc.detail(DetailRequest(url=URL,options={'include_ocr':True}))
    assert len(data['media_ledger'])==7 and sum(r['status']=='acquired' for r in data['media_ledger'])==6
    assert data['ocr']['coverage']=={'requested':7,'acquired':6,'attempted':6,'succeeded':6,'failed':1,'not_executed':0}
    assert data['enhancement_status']=='partial' and not data['ocr']['request_complete']
    from kr_core.evidence_coverage import content_coverage
    assert not content_coverage(data,{'include_ocr':True})['request_satisfied']
    assert [r['image_index'] for r in data['ocr_text_locators']]==[0,1,2,3,5,6]
    for r in data['ocr_text_locators']: assert data['ocr_text'][r['start']:r['end']]=='图片'+str(r['image_index'])
    assert 'sig=secret' not in json.dumps(data)
    assert 'sig=secret' in (oc.state_dir()/'media-fetch.json').read_text(encoding='utf-8')


def test_explicit_selection_and_limit_are_distinct_and_body_only_downloads_nothing(monkeypatch):
    manifest=[{'type':'image','url':f'https://sns-img.xhscdn.com/{i}'} for i in range(7)]+[{'type':'video','url':'https://sns-video.xhscdn.com/v'}]
    def fail(*a,**k):pytest.fail('body only must not download')
    monkeypatch.setattr(oc,'_acquire_media_one',fail)
    rows=oc.acquire_media(manifest,NOTE,options={'include_ocr':False,'include_transcript':False})
    assert len(rows)==8 and all(r['status']=='not_requested' for r in rows)
    rows=oc._media_plan(manifest,{'include_ocr':True,'xhs':{'image_indexes':[1,5,6],'max_images':2}})
    assert [r['image_index'] for r in rows if r['selected']]==[1,5]
    assert rows[6]['requested'] and rows[6]['status']=='not_executed'
    rows=oc._media_plan(manifest,{'include_ocr':True,'xhs':{'image_indexes':[9]}})
    assert rows[-1]['reason']=='requested_image_index_not_observed'


def test_parallel_download_progress_keeps_original_item_identity(monkeypatch):
    import time
    def download(kind,url,note,previous,*,item):
        if item['image_index']==0:time.sleep(.08)
        return {'type':kind,'source_url':url,'status':'failed','reason':str(item['image_index'])}
    monkeypatch.setattr(oc,'_acquire_media_one',download)
    progress=[]
    rows=oc.acquire_media([{'type':'image','url':str(i)} for i in range(2)],NOTE,on_progress=progress.append)
    assert progress[0][0]['status']=='pending' and progress[0][1]['reason']=='1'
    assert [r['reason'] for r in rows]==['0','1']


def test_signature_changes_reuse_only_native_file_identity_and_private_query_is_retained(monkeypatch,tmp_path):
    first=oc._normalize_manifest([{'type':'image','url':'https://sns-img.xhscdn.com/old?token=one','native_file_id':'native-image'}],NOTE)
    second=oc._normalize_manifest([{'type':'image','url':'https://sns-img.xhscdn.com/new?token=two','native_file_id':'native-image'}],NOTE)
    assert first[0]['media_id']==second[0]['media_id']
    assert _fetch_url(second[0]['url']).endswith('?token=two')
    without=oc._normalize_manifest([{'type':'image','url':'https://sns-img.xhscdn.com/new?token=three'}],NOTE)
    assert without[0]['media_id']!=second[0]['media_id']
    from runtime import media_acquisition
    observed=[]
    monkeypatch.setattr(media_acquisition,'acquire_http_asset',lambda kind,url,*a,**k:observed.append((url,k['previous'])) or {'status':'acquired','type':kind,'sha256':'x','path':str(tmp_path/'x')})
    oc._acquire_media_one('image',second[0]['url'],NOTE,[{**first[0],'type':'image','source_url':first[0]['url'],'status':'acquired'}],item=second[0])
    assert observed[0][0].endswith('?token=two') and observed[0][1]


def _fetch_url(ref):
    return json.loads((oc.state_dir()/'media-fetch.json').read_text(encoding='utf-8'))[ref]['url']


def test_processing_options_do_not_discard_collection_checkpoint():
    a=DetailRequest(url=URL,options={'include_ocr':True,'xhs':{'image_indexes':[0]},'language':'zh'})
    b=DetailRequest(url=URL+'?xsec_token=other',options={'include_ocr':True,'xhs':{'image_indexes':[1,2]},'language':'en'})
    assert oc._detail_checkpoint(a).key==oc._detail_checkpoint(b).key
    assert 'processing' not in oc._detail_checkpoint(a).identity


def test_snapshot_comment_ids_are_honest_and_stable_across_resume():
    data={'comments':[{'text':'根','is_reply':False,'root_index':0},{'text':'回复昵称','is_reply':True,'root_index':0,'reply_to':'同名用户'}]}
    scope=oc.XhsReadOptions(comment_limit=1).model_dump()
    oc._normalize_comments(data,scope)
    root,reply=data['comments']
    assert root['id_scope']=='snapshot' and reply['root_comment_id']==root['comment_id']
    assert reply['parent_comment_id'] is None and reply['relationship_confidence']=='dom_root_only'
    old=json.loads(json.dumps(data));oc._normalize_comments(data,scope);assert data==old
    data={'comments':[{'text':'根','is_reply':False}]*3,'comments_budget_truncated':True}
    oc._normalize_comments(data,scope)
    assert not data['comments_complete'] and data['comments_metadata']['stop_reason']=='total_budget'


def test_ocr_no_text_is_completed_but_partial_text_is_not_completed():
    from kr_core.evidence_coverage import content_coverage
    full={'content':'正文','ocr':{'status':'ok','text':'','request_complete':True}}
    assert content_coverage(full,{'include_ocr':True})['request_satisfied']
    partial={'content':'正文','ocr_text':'部分文字','ocr':{'status':'partial','request_complete':False}}
    assert not content_coverage(partial,{'include_ocr':True})['request_satisfied']


def test_timed_transcript_and_ocr_locators_and_media_ledger_survive_fixed_version_read(monkeypatch,tmp_path):
    from kr_core.evidence_store import EvidenceStore
    from kr_core.evidence_assets import EvidenceAssets
    store=EvidenceStore(tmp_path/'events.jsonl');store.assets=EvidenceAssets(tmp_path/'assets')
    location={'start':0,'end':2,'unit':'unicode_code_point','start_s':0.0,'end_s':1.2}
    data={'content':'正文','transcript':'原话','transcript_metadata':{'status':'aligned','segments':[location]},
        'ocr_text':'图片','ocr_text_locators':[{'start':0,'end':2,'image_index':6}],
        'ocr_provenance':{'ocr_images':[{'image_index':6,'input_sha256':'abc'}]},
        'media_ledger':[{'type':'image','image_index':6,'status':'failed','reason':'test','path':'private-path'}]}
    store.append_detail(platform='小红书',url=URL,response=data)
    kinds={ref['kind']:ref for ref in data['evidence_assets']}
    assert {'transcript','ocr_text','media_ledger'} <= kinds.keys()
    saved=store.assets.fetch(kinds['media_ledger']['version_id'])
    assert 'private-path' not in saved['text'] and 'test' in saved['text']
    timed=store.assets.fetch(kinds['transcript']['version_id'])
    assert timed['locators'][0]['end_s']==1.2
    ocr=store.assets.fetch(kinds['ocr_text']['version_id'])
    assert ocr['locators'][0]['image_index']==6


@pytest.mark.parametrize('options',[{'image_indexes':[-1]},{'image_indexes':[True]},{'image_indexes':[1,1]},{'comment_budget':201},{'max_images':0}])
def test_xhs_options_reject_invalid_ranges(options):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):oc.XhsReadOptions.model_validate(options)


def test_connection_diagnostic_does_not_blame_running_chrome_when_daemon_is_down(monkeypatch,tmp_path):
    import httpx
    monkeypatch.setattr(oc,'setting',lambda name:'daily' if 'CONTEXT' in name else str(tmp_path))
    (tmp_path/'package.json').write_text('{}',encoding='utf-8')
    class Client:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,*args,**kwargs):raise httpx.ConnectError('no listener')
    monkeypatch.setattr(httpx,'Client',Client)
    before=list(tmp_path.iterdir());result=oc.connection_status()
    assert result['connection_reason']=='daemon_not_running_or_unreachable'
    assert result['execution_prepares_daemon'] is True
    assert list(tmp_path.iterdir())==before


def test_xhs_public_request_reaches_durable_input_without_losing_window(monkeypatch):
    import server
    from runtime import task_service
    captured={}
    class Service:
        def submit(self,operation,payload,**kwargs):
            captured.update(payload);return {'task_id':'xhs-request-contract','metadata':{}}
        def result(self,task_id):return {'status':'queued'}
    monkeypatch.setattr(task_service,'TaskService',Service)
    result=server.get_content_detail(url=URL,include_ocr=True,xhs=oc.XhsReadOptions(image_indexes=[3,6],max_images=1,comment_limit=4,comment_budget=20))
    assert result['task_id']=='xhs-request-contract'
    assert captured['options']['xhs']=={'image_indexes':[3,6],'max_images':1,'comment_limit':4,'comment_budget':20,'refresh_collection':False}


def test_unrelated_collection_cannot_silently_replace_pending_snapshot(monkeypatch):
    request=DetailRequest(url=URL,options={'include_ocr':True})
    oc._detail_checkpoint(request).write({'collection_version':'new','content':'新的正文'})
    monkeypatch.setattr(oc,'acquire_media',lambda *a,**k:pytest.fail('superseded snapshot must not execute'))
    result=oc.enhance_detail(request,{'collection_version':'old','content':'原来的正文'})
    assert result['enhancement_status']=='collection_snapshot_superseded' and result['content']=='原来的正文'


def test_changed_processing_window_reuses_collection_and_refresh_is_explicit(monkeypatch):
    calls=[]
    monkeypatch.setattr(oc,'invoke',lambda *a,**k:calls.append(k) or {'ok':True,'data':{
        'note_id':NOTE,'content':'正文','completed_stages':['body','media'],'media_manifest':[]}})
    first=oc.detail(DetailRequest(url=URL,options={'include_ocr':False}))
    second=oc.detail(DetailRequest(url=URL,options={'include_ocr':True,'xhs':{'image_indexes':[0]}}))
    assert len(calls)==1 and second['collection_reused'] and not second['source_revalidated']
    assert second['captured_at']==first['captured_at'] and second['enhancement_status']=='partial'
    oc.detail(DetailRequest(url=URL,options={'xhs':{'refresh_collection':True}}))
    assert len(calls)==2


def test_detailed_media_ledger_is_fetchable_without_polluting_search_or_context(tmp_path):
    from kr_core.evidence_assets import EvidenceAssets
    from runtime.agent_contracts import compact_evidence
    assets=EvidenceAssets(tmp_path/'assets')
    ledger=assets.put(URL,'internal unique text',kind='media_ledger')
    assert not assets.search('internal unique')
    assert assets.fetch(ledger['version_id'])['text']=='internal unique text'
    result=compact_evidence({'evidence_assets':[ledger],'downloaded_media':[{'path':'local-only','status':'acquired'}]})
    assert 'downloaded_media' not in result
    assert result['downloaded_media_summary']['fixed_ledger_version']==ledger['version_id']


def test_comment_media_signed_paths_are_private():
    data={'note_id':NOTE,'comments':[{'text':'评论','images':['https://sns-img.xhscdn.com/private-signature/image?sig=secret']}]}
    oc._normalize_comments(data,oc.XhsReadOptions().model_dump())
    assert data['comments'][0]['images'][0].startswith('urn:kr:xhs-media:')
    assert 'private-signature' not in json.dumps(data)
