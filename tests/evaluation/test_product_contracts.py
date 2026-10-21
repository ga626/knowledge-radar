"""业务应有行为；已知缺陷用普通失败断言保留，不能xfail成绿灯。"""
from pathlib import Path

from kr_core.evidence_store import EvidenceStore
from kr_core.models import SearchResponse
from understanding.evidence import attach_detail_evidence, build_detail_evidence, freshness_for_platform


def test_search_detail_evidence_roundtrip(tmp_path):
    url = "https://example.org/articles/fixture"
    search = SearchResponse.from_legacy_items("web", [{"title": "fixture", "url": url, "summary": "candidate only"}]).to_mcp_dict()
    store = EvidenceStore(str(tmp_path / "evidence.jsonl"))
    store.append_search(platform="web", query="fixture", response=search)
    selected = search["items"][0]["url"]
    result = attach_detail_evidence(selected, {"title": "fixture", "content": "正文的独立来源与完整结论。" * 30}, infer_platform=lambda _: "web")
    store.append_detail(platform="web", url=selected, response=result)
    rows = store.recent()
    assert [row["record_type"] for row in rows] == ["search_result", "detail"]
    assert all(row["url"] == url for row in rows)
    assert rows[-1]["payload"]["evidence"]["source_url"] == url
    assert rows[-1]["payload"]["content"].startswith("正文")


def test_empty_detail_never_verified():
    item = build_detail_evidence("https://example.org/empty", "web", {})
    assert item.verification_status != "已验证", "F01: empty detail was silently promoted to verified"


def test_future_publication_is_not_evergreen():
    assert freshness_for_platform("web", {"published_at": "2099-01-01T00:00:00Z"}) in {"future", "unknown"}, "F02: future date is not evergreen"


def test_successful_body_does_not_create_failure_tags(tmp_path, monkeypatch):
    import runtime.tool_trace as trace
    recorder = trace.ToolTraceRecorder(str(tmp_path / "trace.jsonl"))
    monkeypatch.setattr(trace, "_DEFAULT_RECORDER", recorder)
    with trace.trace_tool_call("fixture_success") as capture:
        capture({"content": "A tutorial explains login, timeout, cookie, provider and 404 handling."})
    event = recorder.last_event()
    assert event["status"] == "ok"
    assert event["failure_tags"] == [], "F03: successful body polluted failure labels"


def test_preexisting_evidence_is_renormalized_instead_of_trusted():
    result = attach_detail_evidence("https://example.org/empty", {
        "evidence": {"verification_status": "已验证", "metadata": {"claim_verification": "verified"}},
    }, infer_platform=lambda _: "web")
    assert result["evidence"]["verification_status"] != "已验证"
    assert result["evidence"]["metadata"]["extraction_status"] == "insufficient_content"
    assert result["evidence"]["metadata"]["claim_verification"] == "unverified"


def test_future_date_is_parsed_but_not_trusted():
    result = build_detail_evidence("https://example.org/", "web", {
        "content": "A substantive article. " * 20, "published_at": "2099-01-01",
    })
    assert result.metadata["published_at_confidence"] == "future"


def test_failure_code_is_read_without_error_text():
    from runtime.tool_trace import infer_failure_code, structured_failure_tags
    result = {"status": "failed", "failure_type": "empty_detail"}
    assert infer_failure_code(result) == "empty_detail"
    assert structured_failure_tags(result) == ["empty_detail"]


def test_failed_attempt_uses_code_not_arbitrary_detail_text():
    from runtime.tool_trace import structured_failure_tags
    result = {"content": "success", "metadata": {"collection": {"attempts": [{
        "status": "failed", "failure_type": "empty_detail",
        "detail": "https://provider.example/timeout-cookie-tutorial",
    }]}}}
    assert structured_failure_tags(result) == ["empty_detail"]


def test_failure_parser_tolerates_foreign_metadata():
    from runtime.tool_trace import structured_failure_tags
    assert structured_failure_tags({"metadata": {"collection": "legacy"}}) == []


def test_evidence_store_never_persists_protected_fetch_url(tmp_path):
    secret = "PRIVATE_XSEC_CANARY"
    url = f"https://www.xiaohongshu.com/explore/fixture?xsec_token={secret}&xsec_source=pc_search"
    store = EvidenceStore(str(tmp_path / "evidence.jsonl"))
    store.append_detail(platform="小红书", url=url, response={
        "url": url, "fetch_url": url, "content": "article", "evidence": {"source_url": url},
    })
    store.append_search(platform="小红书", query="fixture", response={"items": [{
        "url": url, "metadata": {"xsec_token": secret, "fetch_url": url},
    }]})
    raw = Path(store.path).read_text(encoding="utf-8")
    assert secret not in raw
    assert all(row["url"] == "https://www.xiaohongshu.com/explore/fixture?xsec_source=pc_search" for row in store.recent())


def test_legacy_display_field_has_executable_retirement_date():
    from datetime import datetime, timezone
    before = build_detail_evidence("https://example.org/", "web", {}, now=datetime(2026, 12, 31, tzinfo=timezone.utc))
    after = build_detail_evidence("https://example.org/", "web", {}, now=datetime(2027, 1, 1, tzinfo=timezone.utc))
    assert "verification_status" in before.to_mcp_dict()
    assert "verification_status" not in after.to_mcp_dict()
    assert after.metadata["claim_verification"] == "unverified"


def test_search_mcp_output_does_not_export_access_material():
    import json
    raw = "https://www.xiaohongshu.com/explore/fixture?xsec_token=PRIVATE_XSEC_CANARY"
    response = SearchResponse.from_legacy_items("小红书", [{
        "title": "fixture", "url": raw, "canonical_url": "https://www.xiaohongshu.com/explore/fixture",
        "fetch_url": raw, "xsec_token": "PRIVATE_XSEC_CANARY",
    }]).to_mcp_dict()
    assert "PRIVATE_XSEC_CANARY" not in json.dumps(response)
    assert response["items"][0]["url"] == "https://www.xiaohongshu.com/explore/fixture"


def test_canonical_detail_token_lookup_is_profile_scoped_and_expires(monkeypatch):
    from collectors.platform import xiaohongshu as xhs
    from runtime.xhs_task_failover import task_profile
    monkeypatch.setattr(xhs, "_PROTECTED_FETCH", __import__("collections").OrderedDict())
    monkeypatch.setattr(xhs.time, "monotonic", lambda: 100)
    with task_profile("A"):
        xhs._remember_xhs_fetch("fixture", "PRIVATE_XSEC_CANARY")
        assert xhs._recover_xhs_xsec_token("fixture") == "PRIVATE_XSEC_CANARY"
    with task_profile("B"):
        assert xhs._cached_xhs_token("fixture") == ""
    monkeypatch.setattr(xhs.time, "monotonic", lambda: 401)
    with task_profile("A"):
        assert xhs._cached_xhs_token("fixture") == ""


def test_liepin_attributed_jd_without_fixed_heading_and_navigation_are_distinct():
    from collectors.platform.liepin import _job_detail_quality
    body = "负责 Python 后端服务开发，熟悉数据库设计，有分布式系统经验。本科以上学历。" * 4
    assert _job_detail_quality(body, extraction_source="jobposting_jsonld", title="后端工程师")["ok"]
    assert not _job_detail_quality(body)["ok"]
    navigation = "首页 职位 校园 登录/注册 登录获取更匹配职位 岗位职责 " * 20
    assert not _job_detail_quality(navigation, extraction_source="jobposting_jsonld", title="职位")["ok"]


def test_runtime_identity_changes_when_adapter_code_changes(tmp_path):
    from runtime.mcp_runtime import source_fingerprint
    folder = tmp_path / "src" / "runtime"
    folder.mkdir(parents=True)
    module = folder / "adapter.py"
    module.write_text("contract=1", encoding="utf-8")
    before = source_fingerprint(tmp_path)
    module.write_text("contract=2", encoding="utf-8")
    assert before != source_fingerprint(tmp_path)
