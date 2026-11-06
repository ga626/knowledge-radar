"""代表链独立装配与浏览器权限，不调用真实账号。"""
import os
import json
from pathlib import Path
import subprocess
import sys
import pytest


@pytest.fixture(autouse=True)
def preserve_tool_trace(tmp_path, monkeypatch):
    from runtime.tool_trace import current_tool_trace, set_current_tool_trace
    for key, value in {"KR_DATA_ROOT": "data", "KR_STATE_DIR": "state", "KR_LOG_DIR": "logs",
                       "KR_TASK_DB_PATH": "tasks.sqlite3", "KR_EVIDENCE_ROOT": "assets"}.items():
        monkeypatch.setenv(key, str(tmp_path / value))
    previous = current_tool_trace()
    try:
        yield
    finally:
        set_current_tool_trace(previous)


def test_boss_worker_assembles_without_importing_mcp_facade(tmp_path):
    checkout = Path(__file__).resolve().parents[1]
    code = '''
import builtins, sys
sys.path.insert(0, sys.argv[1])
from contextlib import contextmanager
from runtime import chrome_manager
from collectors.platform import boss as boss
from generic_web import url_policy
from runtime.platform_execution import worker_detail, ensure_bindings
from kr_core import DetailRequest
from kr_core.registry import registry
events = []
@contextmanager
def lease(platform):
    events.append("lease:" + platform)
    try: yield
    finally: events.append("released")
chrome_manager.chrome_active_operation = lease
boss.detail = lambda url: {"content": "岗位职责和任职要求。" * 30, "title": "真实正文合同", "status": "ok"}
url_policy.validate_detail_target = lambda *args: None
ensure_bindings()
registry.set_after_detail("BOSS直聘", lambda result: result)
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == "server": raise AssertionError("reverse facade import")
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
result = worker_detail(DetailRequest(url="https://www.zhipin.com/job_detail/fixture.html", platform="BOSS直聘"))
assert result["content"] and result["evidence_assets"], result
assert events == [], events  # no managed browser ownership in a borrowed adapter
assert "server" not in sys.modules
'''
    env = dict(os.environ, KR_STATE_DIR=str(tmp_path / "state"), KR_LOG_DIR=str(tmp_path / "logs"),
               KR_TASK_DB_PATH=str(tmp_path / "tasks.sqlite3"), PYTHONIOENCODING="utf-8")
    completed = subprocess.run([sys.executable, "-X", "utf8", "-c", code, str(checkout / "src")], cwd=checkout, env=env,
                               capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert completed.returncode == 0, completed.stderr


def test_borrowed_browser_cannot_be_promoted_to_owned():
    from runtime.browser_channel import BorrowedBrowser, browser_resource_contract
    with pytest.raises(ValueError, match="ownership"):
        BorrowedBrowser("daily", "session", "generation", ownership="owned")
    assert BorrowedBrowser("daily", "session", "generation").resource_contract()["may_close_process"] is False
    assert browser_resource_contract(ownership="owned", resource_id="boss", generation="", driver="cdp")["may_close_process"] is False


def test_xhs_transport_borrowed_lease_releases_on_failure(monkeypatch, tmp_path):
    from runtime import leases, opencli_bridge, process, xhs_opencli as oc
    from runtime.browser_channel import BorrowedBrowser
    monkeypatch.setenv('KR_STATE_DIR', str(tmp_path/'state'))
    monkeypatch.setenv('KR_OPENCLI_STATE_DIR', str(tmp_path/'opencli'))
    monkeypatch.setenv('KR_XHS_OPENCLI_CONTEXT_ID', 'same-daily-chrome')
    monkeypatch.setenv('KR_XHS_OPENCLI_ADAPTER_SHA256', 'fixture-adapter')
    monkeypatch.setenv('KR_NODE_EXE', 'fixture-node')
    coordinator = leases.RuntimeLeaseCoordinator(tmp_path/'leases.sqlite3')
    monkeypatch.setattr(leases, '_COORDINATOR', coordinator)
    package = tmp_path/'package.json'
    package.write_text('{"version":"2.0.2"}', encoding='utf-8')
    monkeypatch.setattr(opencli_bridge, 'connection_config', lambda:{'root':str(tmp_path),
        'context_id':'same-daily-chrome','state_root':tmp_path/'opencli'})
    monkeypatch.setattr(opencli_bridge, 'ensure_daemon', lambda *a,**k:'')
    session = 'kr:xiaohongshu'
    def fail_transport(*args, **kwargs):
        active = coordinator.resource_leases('browser_site', 'daily-chrome:xiaohongshu')
        assert len(active)==1 and active[0]['metadata']['ownership']=='borrowed'
        assert active[0]['metadata']['may_close_process'] is False
        raise subprocess.TimeoutExpired('isolated', 1)
    monkeypatch.setattr(oc, '_run', fail_transport)
    monkeypatch.setattr(process, 'silent_subprocess_run', fail_transport)
    def invoke():
        return oc.invoke('detail')
    with BorrowedBrowser('same-daily-chrome',session,'fixture').lease('other-request'):
        busy = invoke()
        assert busy.get('failure_type', busy.get('code'))=='browser_session_busy'
    with pytest.raises(subprocess.TimeoutExpired):
        invoke()
    assert coordinator.resource_leases('browser_site')==[]


def test_worker_preserves_body_and_precise_budget_failure(tmp_path, monkeypatch):
    from runtime.tasks import TaskStore
    from runtime.task_process import execute_job
    from runtime import optional_media
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("KR_TASK_INPUT_STDIN", raising=False)
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    store.upsert_task("budget-body", "durable_content_detail", "小红书")
    job = {"task_id": "budget-body", "attempt_id": "one", "db_path": store.db_path,
           "operation": "optional_media", "identity": {}, "payload": {}}
    path = tmp_path / "input.json"
    path.write_text(json.dumps(job), encoding="utf-8")
    monkeypatch.setattr(optional_media, "run_optional", lambda _: {
        "content": "正文已保留", "enhancement_status": "partial",
        "ocr": {"status": "partial", "results": [{"status": "failed", "reason": "paid_money_limit"}]}})
    execute_job(path)
    task = store.get_task("budget-body")
    assert task["status"] == "failed" and task["error_code"] == "paid_money_limit"
    assert json.loads(Path(task["result_path"]).read_text(encoding="utf-8"))["result"]["content"] == "正文已保留"


@pytest.mark.parametrize("platform,module,method", [
    ("B站", "bilibili", "legacy_search_bilibili"),
    ("YouTube", "youtube", "search_youtube"),
    ("知乎", "zhihu", "legacy_search_zhihu"),
])
def test_default_search_bindings_do_not_depend_on_facade(monkeypatch, platform, module, method):
    from importlib import import_module
    from kr_core import SearchRequest
    from kr_core.registry import PlatformRegistry
    from platform_adapters.factory import register_default_adapters
    from runtime import account_business
    calls = []
    def search(keyword, **kwargs):
        calls.append((keyword, kwargs))
        return {"items": [{"title": "fixture", "url": "https://example.org/item"}], "total": 1}
    monkeypatch.setattr(import_module("collectors.platform." + module), method, search)
    observations = []
    monkeypatch.setattr(account_business, "observe_business",
                        lambda name, result, **kw: observations.append((name, kw["source"])) or result)
    target = register_default_adapters(target_registry=PlatformRegistry())
    result = target.get(platform).search(SearchRequest(keyword="同一输入", platform=platform, limit=3))
    assert result.items[0].title == "fixture"
    assert calls == [("同一输入", {"page_size" if platform == "B站" else "limit": 3})]
    assert observations == []
    if platform == "知乎":
        target.after_detail(platform, {"content": "fixture"})
        assert observations == []


def test_concurrent_composition_is_idempotent_and_keeps_explicit_bindings(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    from kr_core.registry import PlatformRegistry
    from platform_adapters.factory import register_default_adapters
    target = PlatformRegistry()
    adapter = SimpleNamespace(platform="知乎")
    def factory():
        return "explicit_strategy"
    def hook(data):
        return {**data, "observed": True}
    target.register(adapter, detail_factory=factory, after_detail=hook)
    calls = []
    original = target.register
    def register(binding, **kw):
        time.sleep(.001)
        calls.append(binding.platform)
        original(binding, **kw)
    import time
    monkeypatch.setattr(target, "register", register)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: register_default_adapters(target_registry=target), range(24)))
    assert all(result is target for result in results)
    expected = {"B站", "YouTube", "知乎", "小红书", "BOSS直聘", "猎聘", "脉脉", "V2EX", "GitHub", "微信"}
    assert len(calls) == len(set(calls)) and set(calls) == expected - {"知乎"}
    assert 'GitHub' in calls
    assert target.get("知乎") is adapter and target._details["知乎"] is factory
    assert target.after_detail("知乎", {}) == {"observed": True}
    assert set(target.platforms()) == expected
    target.clear()
    register_default_adapters(target_registry=target)
    assert set(target.platforms()) == expected and callable(target.get("知乎")._search_func)


@pytest.mark.parametrize("order", ["worker_first", "facade_first"])
def test_composition_order_cannot_replace_bindings_or_start_collectors(tmp_path, order):
    checkout = Path(__file__).resolve().parents[1]
    code = '''
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
import httpx
from runtime import chrome_manager, opencli_bridge
from runtime.browser_link import BrowserLink
def forbidden(*args, **kw): raise AssertionError("composition started external work")
httpx.Client.send = forbidden
chrome_manager._ensure_chrome_debugging = forbidden
opencli_bridge.ensure_daemon = forbidden
BrowserLink.request = forbidden
from runtime.platform_execution import ensure_bindings
from kr_core.registry import registry
def snapshot():
    return {p: (id(registry.get(p)), id(registry._details[p]), id(registry._after_detail.get(p))) for p in registry.platforms()}
if sys.argv[2] == "worker_first":
    ensure_bindings()
    before = snapshot()
    import server
else:
    import server
    before = snapshot()
    ensure_bindings()
assert before == snapshot()
assert set(before) == {"B站", "YouTube", "知乎", "小红书", "BOSS直聘", "猎聘", "脉脉", "V2EX", "GitHub", "微信"}
assert "知乎" not in registry._after_detail
assert all(callable(registry.get(p).search) for p in registry.platforms())
assert all(callable(registry.get(p)._search_func) for p in {"B站", "YouTube", "知乎", "BOSS直聘", "猎聘", "脉脉", "V2EX"})
assert type(registry.get("微信")).__name__ == "WechatAdapter"
assert type(registry.detail_strategy("GitHub")).__name__ == "GitHubDetailStrategy"
assert Path(server.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve())
assert type(registry.detail_strategy("B站")).__name__ == "VideoDetailStrategy"
assert type(registry.detail_strategy("YouTube")).__name__ == "VideoDetailStrategy"
'''
    env = dict(os.environ, KR_DATA_ROOT=str(tmp_path / "data"), KR_STATE_DIR=str(tmp_path / "state"),
               KR_LOG_DIR=str(tmp_path / "logs"), KR_TASK_DB_PATH=str(tmp_path / "tasks.sqlite3"),
               PYTHONIOENCODING="utf-8")
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", code, str(checkout / "src"), order],
                            cwd=checkout, env=env, capture_output=True, text=True, encoding="utf-8", timeout=25)
    assert result.returncode == 0, result.stderr + result.stdout


@pytest.mark.parametrize("platform", ["B站", "YouTube", "知乎", "小红书", "BOSS直聘", "猎聘", "脉脉", "V2EX"])
@pytest.mark.parametrize("blocked", [False, True])
def test_foreground_worker_share_detail_fields_and_hook_order(monkeypatch, platform, blocked):
    from types import SimpleNamespace
    import server
    from kr_core import DetailRequest, DetailResponse
    from kr_core.registry import registry
    from kr_core.evidence_store import EvidenceStore
    from runtime import platform_execution, failure_cache, task_scope
    from generic_web import url_policy
    url = "https://example.org/detail"
    events = []
    def extract(request):
        events.append("extract")
        data = {"platform": platform, "url": url, "title": "fixture", "content": "固定正文", "salary": "20-30K",
                "comments": [{"content": "原始评论"}], "transcript": "hello 中文", "transcript_metadata": {"language": "auto"}}
        if blocked:
            data.update(error="请完成验证", failure_type="anti_bot_verification", manual_action_required=True)
        return DetailResponse.from_legacy(platform, url, data, metadata={"strategy": "fixture"})
    monkeypatch.setattr(registry, "detail_strategy", lambda _: SimpleNamespace(extract=extract))
    monkeypatch.setattr(registry, "runtime_identity", lambda _: {"generation": "fixture"})
    monkeypatch.setattr(registry, "prepare_detail", lambda req: events.append("prepare") or req)
    monkeypatch.setattr(registry, "after_detail", lambda _, result: events.append("observe") or result)
    monkeypatch.setattr(platform_execution, "ensure_bindings", lambda: None)
    monkeypatch.setattr(url_policy, "validate_detail_target", lambda *a: events.append("validate"))
    monkeypatch.setattr(server, "_record_detail_decision", lambda *a: None)
    monkeypatch.setattr(server, "_attach_detail_degradation", lambda p, u, d, m: m)
    monkeypatch.setattr(server, "_attach_research_session_tasks", lambda result, *a: result)
    monkeypatch.setattr(task_scope, "attach_detail_task_scope", lambda result, *a: result)
    def archive(*args, **kwargs):
        events.append("archive")
    monkeypatch.setattr(server, "_record_detail_evidence", archive)
    monkeypatch.setattr(EvidenceStore, "record_detail", archive)
    cache = failure_cache.FailureStateCache()
    monkeypatch.setattr(server, "detail_failure_cache", cache)
    request = DetailRequest(url=url, platform=platform, options={"include_comments": True, "include_transcript": True})
    front = server._execute_detail_request(request)
    assert events == ["prepare", "validate", "extract", "observe", "archive"]
    events.clear()
    monkeypatch.setattr(failure_cache, "detail_failure_cache", failure_cache.FailureStateCache())
    back = platform_execution.worker_detail(request)
    assert events == ["validate", "extract", "observe", "archive"]
    front.pop("timings", None)
    back.pop("timings", None)
    # 两次独立执行的采集时间必须各自保留，比较其余合同字段。
    from datetime import datetime
    for result in (front, back):
        datetime.fromisoformat(result["evidence"].pop("retrieved_at"))
    assert front == back


def test_enhancement_uses_collected_payload_without_preparation_or_recollection(monkeypatch):
    from types import SimpleNamespace
    from kr_core import DetailRequest, DetailResponse
    from kr_core.registry import registry
    from kr_core.evidence_store import EvidenceStore
    from runtime import platform_execution, failure_cache, task_scope
    from generic_web import url_policy
    collected = {"content": "已经采到的正文", "_enhancement_pending": True}
    events = []
    def forbidden(*a, **kw):
        pytest.fail("enhancement must not prepare or recollect")
    monkeypatch.setattr(platform_execution, "ensure_bindings", lambda: None)
    monkeypatch.setattr(registry, "detail_strategy", lambda _: SimpleNamespace(extract=forbidden))
    monkeypatch.setattr(registry, "prepare_detail", forbidden)
    def enhance(req, data):
        assert data is collected
        events.append("enhance")
        return DetailResponse.from_legacy(req.platform, req.url, {"content": data["content"], "transcript": "hello 中文"})
    monkeypatch.setattr(registry, "enhance_detail", enhance)
    monkeypatch.setattr(registry, "after_detail", lambda _, data: events.append("observe") or data)
    monkeypatch.setattr(registry, "runtime_identity", lambda _: {})
    monkeypatch.setattr(failure_cache, "detail_failure_cache", failure_cache.FailureStateCache())
    monkeypatch.setattr(task_scope, "attach_detail_task_scope", lambda data, *a: data)
    monkeypatch.setattr(url_policy, "validate_detail_target", lambda *a: None)
    monkeypatch.setattr(EvidenceStore, "record_detail", lambda *a, **kw: events.append("archive"))
    result = platform_execution.worker_detail(DetailRequest(url="https://example.org/video", platform="B站"), collected)
    assert result["content"] == collected["content"] and result["transcript"] == "hello 中文"
    assert events == ["enhance", "observe", "archive"]


def test_shared_response_keeps_typed_error_and_does_not_mutate_strategy_payload():
    from types import SimpleNamespace
    from kr_core import DetailRequest, DetailResponse
    from kr_core.errors import ErrorCode, KnowledgeRadarError
    from use_cases.content_detail import extract_detail_response
    request = DetailRequest(url="https://example.org/typed-error", platform="fixture")
    error = KnowledgeRadarError("login required", code=ErrorCode.LOGIN_REQUIRED, platform="fixture", retryable=False)
    original = DetailResponse(platform="fixture", url=request.url, data={"content": "已采正文"}, error=error)
    result = extract_detail_response(request, strategy=SimpleNamespace(extract=lambda _: original))
    assert result.error is error and result.to_legacy_dict()["error"] == "login required"
    assert result.evidence.metadata["retrieval_status"] == "failed"
    assert original.data == {"content": "已采正文"}


def test_v2ex_public_search_keeps_shared_adapter_wiring(monkeypatch, tmp_path):
    import server
    from collectors.platform import v2ex
    calls = []
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path / "state"))
    def search(keyword, limit, **kwargs):
        calls.append((keyword, limit, kwargs))
        return {"platform": "V2EX", "items": [{"title": "fixture", "url": "https://www.v2ex.com/t/123"}]}
    monkeypatch.setattr(v2ex, "legacy_search_v2ex", search)
    monkeypatch.setattr(server, "_with_recruitment_runtime_guards", lambda p, q, c, run: run())
    result = server.search_recruitment("v2ex", "Python", city="上海", limit=3)
    assert result["items"][0]["url"] == "https://www.v2ex.com/t/123"
    assert calls == [("Python", 3, {"city": "上海", "page": 1, "pages": 2, "offset": 0})]


@pytest.mark.parametrize("flags,durable,expected", [
    ({}, False, (False, False, False)),
    ({"include_transcript": False, "include_comments": False, "include_ocr": False}, False, (False, False, False)),
    ({"auto_multimodal": True}, True, (True, False, True)),
    ({"enable_deep_analysis": True}, True, (True, False, True)),
    ({"enable_comment_filtering": True}, True, (False, True, False)),
    ({"include_transcript": True}, True, (True, False, False)),
    ({"include_comments": True}, True, (False, True, False)),
    ({"include_ocr": True}, True, (False, False, True)),
    ({"auto_multimodal": True, "include_transcript": False, "include_ocr": False}, True, (False, False, False)),
])
def test_public_detail_handoff_preserves_flags_scope_and_one_preparation(monkeypatch, flags, durable, expected):
    import server
    from dataclasses import asdict
    from kr_core import DetailRequest
    from kr_core.registry import registry
    from runtime import task_service
    from runtime.task_scope import make_task_scope
    from runtime.redaction import redact_url
    calls = []
    url = "https://www.bilibili.com/video/BV1HhaA6hEsn?token=private-fixture"
    scope = make_task_scope(source_url=redact_url(url), platform="B站", work_scope_id="work",
                            task_scope_id="scope", research_session_id="session")
    monkeypatch.setattr(registry, "prepare_detail", lambda req: calls.append("prepare") or req)
    class Service:
        def submit(self, operation, payload, **kw):
            assert operation == "content_detail" and kw == {"timeout_s": 73, "scope": {**scope.to_metadata(), "research_task_id": "research"}}
            payload = asdict(registry.prepare_detail(DetailRequest(**payload)))
            calls.append(payload)
            return {"task_id": "durable-fixture", "status": "queued"}
        def result(self, task_id):
            assert task_id == "durable-fixture"
            return {"status": "queued"}
    monkeypatch.setattr(task_service, "TaskService", Service)
    def sync(req):
        calls.append(asdict(registry.prepare_detail(req)))
        return {"content": "正文"}
    monkeypatch.setattr(server, "_execute_detail_request", sync)
    result = server.get_content_detail(url, research_session_id="session", work_scope_id="work",
        task_scope_id="scope", research_task_id="research", language="en", deadline_s=73, **flags)
    assert calls.count("prepare") == 1
    payload = next(row for row in calls if isinstance(row, dict))
    assert payload["url"] == url and payload["platform"] == "B站"
    assert payload["options"]["task_scope"] == scope.compact()
    assert payload["options"]["research_task_id"] == "research" and payload["options"]["language"] == "en"
    assert tuple(payload["options"][key] for key in ("include_transcript", "include_comments", "include_ocr")) == expected
    if durable:
        assert result["accepted"] and result["task_id"] == "durable-fixture"
        assert result["scope_binding"] == scope.compact()
    else:
        assert result["content"] == "正文"
        # 门面的追踪装饰器仍追加研究关联回执，公共用例不接管它。
        assert result["research_receipt"]["reason"] == "unknown_task"


@pytest.mark.parametrize("status,partial", [("completed", False), ("queued", False), ("running", True), ("failed", True), ("cancelled", False)])
def test_public_detail_result_envelope_survives_handoff(monkeypatch, status, partial):
    import server
    from runtime import task_service
    class Service:
        def submit(self, *a, **kw):
            return {"task_id": "existing", "status": status, "metadata": {"artifact_sha256": "sha"}}
        def result(self, task_id):
            return {"status": status, **({"result": {"content": "正文"}} if partial or status == "completed" else {})}
    monkeypatch.setattr(task_service, "TaskService", Service)
    result = server.get_content_detail("https://www.bilibili.com/video/BV1HhaA6hEsn", include_transcript=True)
    assert result["task_id"] == "existing"
    if status == "completed":
        assert result == {"content": "正文", "task_id": "existing", "durable_task": {"status": "completed", "artifact_sha256": "sha", "reused": True}}
    else:
        assert result["status"] == status and result["accepted"] is True
        assert result["result_tool"] == "get_task_status" and result["cancel"] == {"tool":"control_task","arguments":{"task_id":"existing","action":"cancel"}} and result["retry"] == {"tool":"control_task","arguments":{"task_id":"existing","action":"resume"}}
        assert ("result" in result) == partial
        if partial:
            assert result["result"]["content"] == "正文" and result["partial_result"] is True


def test_local_evidence_query_precedes_validation_and_cannot_submit(monkeypatch):
    import server
    from runtime import task_service, optional_media
    class Service:
        def result(self, task_id):
            assert task_id == "existing"
            return {"status": "failed", "partial_result": True, "result": {"content": "保留正文", "evidence_assets": [{"id": "asset"}]}}
        def submit(self, *a, **kw):
            pytest.fail("query resubmitted work")
    monkeypatch.setattr(task_service, "TaskService", Service)
    monkeypatch.setattr(optional_media, "select_component", lambda *a: pytest.fail("query inspected input"))
    result = server.process_local_evidence(task_id="existing", input_path="missing", input_sha256="invalid")
    assert result["status"] == "failed" and result["evidence_assets"] == [{"id": "asset"}]
    assert result["result"]["content"] == "保留正文"


@pytest.mark.parametrize("invalid", ["missing_root", "wrong_sha", "outside_root", "invalid_sha"])
def test_local_evidence_rejects_unauthorized_input_before_submit(tmp_path, monkeypatch, invalid):
    import hashlib
    import server
    from runtime import task_service
    root = tmp_path / "approved"
    root.mkdir()
    source = (tmp_path if invalid == "outside_root" else root) / "evidence.wav"
    source.write_bytes(b"fixture")
    class Service:
        def submit(self, *a, **kw):
            pytest.fail("unapproved input reached submission")
    monkeypatch.setattr(task_service, "TaskService", Service)
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    if invalid == "wrong_sha":
        sha = "0" * 64
    if invalid == "invalid_sha":
        sha = "invalid"
    with pytest.raises(ValueError):
        server.process_local_evidence(input_path=str(source), approved_input_root="" if invalid == "missing_root" else str(root), input_sha256=sha)


@pytest.mark.parametrize('suffix,component,document,timeout_s', [
    ('wav', 'qwen3-asr-0.6b', None, 600),
    ('pdf', 'pypdf', {'page_numbers': [1], 'include_ocr': False, 'storage_allowed': False}, 137),
])
def test_local_evidence_submission_keeps_selected_component_identity_and_scope(
    tmp_path, monkeypatch, suffix, component, document, timeout_s,
):
    import hashlib
    import server
    from runtime import task_service
    source = tmp_path / ('evidence.' + suffix)
    source.write_bytes(b"fixture")
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    class Service:
        def submit(self, operation, payload, **kw):
            assert operation == "optional_media"
            assert payload == {"input_path": str(source), "approved_input_root": str(tmp_path), "input_sha256": sha,
                               "component": component, "language": "en", 'timeout_s': timeout_s,
                               'document': document or {'page_numbers': None, 'include_ocr': None, 'storage_allowed': True}}
            assert kw == {"scope": {"research_task_id": "research"}, "timeout_s": timeout_s}
            return {"task_id": "local", "status": "queued"}
    monkeypatch.setattr(task_service, "TaskService", Service)
    result = server.process_local_evidence(input_path=str(source), approved_input_root=str(tmp_path),
        input_sha256=sha, research_task_id="research", language="en", document=document, timeout_s=timeout_s)
    assert result["task"] == {"task_id": "local", "status": "queued"} and result["status"] == "queued"


def test_legacy_video_bridge_uses_same_submission_contract(monkeypatch):
    from runtime.video_tasks import submit_video_detail
    from runtime import task_service
    class Service:
        def submit(self, operation, payload, **kw):
            assert operation == "content_detail" and kw == {"scope": {"task_scope_id": "scope"}, "timeout_s": 600}
            assert payload["platform"] == "YouTube" and payload["enable_deep_analysis"] is True
            assert payload["options"] == {"include_transcript": True, "language": "en", "task_scope": {"task_scope_id": "scope"}}
            return {"task_id": "legacy"}
        def result(self, task_id):
            assert task_id == "legacy"
            return {"status": "failed", "result": {"content": "正文"}}
    monkeypatch.setattr(task_service, "TaskService", Service)
    result = submit_video_detail("YouTube", "https://youtu.be/fixture", language="en", scope={"task_scope_id": "scope"}, include_transcript=True, deep=True)
    assert result == {"content": "正文", "status": "failed", "task_id": "legacy"}


def test_video_combination_accepts_new_collector_without_platform_imports(monkeypatch):
    from detail_strategies.video import VideoDetailStrategy
    from kr_core import DetailRequest
    import builtins
    original = builtins.__import__
    def guarded(name, *args, **kw):
        assert not name.startswith("collectors.platform"), name
        assert name != "server", name
        return original(name, *args, **kw)
    monkeypatch.setattr(builtins, "__import__", guarded)
    seen = []
    def collect(request):
        seen.append(request.url)
        return {"content": "platform-owned text", "title": "fixture", "transcript": "existing subtitle"}
    strategy = VideoDetailStrategy("fixture-site", collect=collect)
    request = DetailRequest(platform="fixture-site", url="https://example.org/video", options={"include_transcript": True})
    response = strategy.extract(request).data
    assert response["transcript"] == "existing subtitle"
    assert seen == [request.url]


def test_runtime_consumes_adapter_resource_contract_without_site_knowledge():
    from types import SimpleNamespace
    from kr_core.registry import PlatformRegistry
    target = PlatformRegistry()
    seen = []
    target.register(SimpleNamespace(platform="fixture", resource_ready=lambda token: seen.append(token) or False))
    assert target.resource_ready("fixture", "site-specific:busy") is False
    assert seen == ["site-specific:busy"]
    assert target.resource_ready("other", "local_heavy") is None
    checkout = Path(__file__).resolve().parents[1]
    public = (checkout / "src/runtime/resource_concurrency.py").read_text(encoding="utf-8")
    assert "BrowserLink" not in public and "recruitment_platform" not in public
