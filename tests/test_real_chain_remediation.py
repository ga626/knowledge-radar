from __future__ import annotations

from detail_strategies.recruitment import _normalize_recruitment_detail
from generic_web.collector import collect_rendered_html
from generic_web.models import GenericWebRequest
from runtime.tool_trace import ToolTraceRecorder, record_trace_child, set_current_tool_trace


def test_recruitment_login_shell_is_never_normalized_as_success() -> None:
    result = _normalize_recruitment_detail(
        "猎聘",
        "https://www.liepin.com/job/example.shtml",
        {
            "status": "ok",
            "title": "Python",
            "content": "首页 职位 校园 海归 简历优化 猎聘APP 我是猎头 我是招聘方 NEW 登录/注册",
        },
    )

    assert result["status"] == "needs_interaction"
    assert result["failure_type"] == "login_required"
    assert result["manual_action_required"] is True


def test_recruitment_short_non_login_text_is_not_usable_detail() -> None:
    result = _normalize_recruitment_detail(
        "BOSS直聘",
        "https://www.zhipin.com/job_detail/example.html",
        {"status": "ok", "title": "Python", "content": "页面暂时没有可用职位正文"},
    )

    assert result["status"] == "empty"
    assert result["failure_type"] == "empty_detail"


def test_rendered_html_prefers_long_semantic_content_container() -> None:
    long_text = "这是动态加载后的正文。" * 40
    html = f"""
    <html><head><title>动态文章</title></head><body>
      <main><p>页面壳很短。</p></main>
      <div class='article-body'><p>{long_text}</p></div>
    </body></html>
    """

    result = collect_rendered_html(
        GenericWebRequest(url="https://example.com/article", timeout=5),
        html=html,
        render_metadata={"networkidle": "not_reached:TimeoutError", "html_chars": len(html)},
    )

    assert result.error is None
    assert long_text[:30] in result.content
    assert result.metadata["content_selector"] == "semantic_class_or_id"
    assert result.metadata["render"]["networkidle"] == "not_reached:TimeoutError"


def test_trace_child_receipt_links_to_active_execution_without_sensitive_payload(monkeypatch, tmp_path) -> None:
    import runtime.tool_trace as tool_trace

    recorder = ToolTraceRecorder(str(tmp_path / "trace.jsonl"))
    monkeypatch.setattr(tool_trace, "get_tool_trace_recorder", lambda: recorder)
    set_current_tool_trace({"trace_id": "root-123", "tool_name": "kr_web_search", "strategy": "provider_fallback"})

    child = record_trace_child(
        "provider_attempt",
        metadata={
            "status": "ok",
            "wave_index": 0,
            "provider_id": "anysearch",
            "result_count": 3,
            "outcome": "usable_results",
            "query": "must_not_be_written",
        },
    )

    assert child["event_type"] == "provider_attempt"
    assert child["parent_trace_id"] == "root-123"
    assert child["metadata"]["provider_id"] == "anysearch"
    assert "query" not in child["metadata"]


def test_provider_receipt_uses_wave_and_provider_without_query(monkeypatch) -> None:
    import search_providers.service as service

    calls = []
    monkeypatch.setattr(service, "record_trace_child", lambda event_type, **kwargs: calls.append((event_type, kwargs)))

    service._record_provider_attempt_receipts(
        wave_index=2,
        attempted=["anysearch", "codex_web_search"],
        results={"anysearch": [object()]},
        errors=[{"provider": "codex_web_search", "type": "not_callable", "expected_degraded": True}],
    )

    assert [(event, item["metadata"]["provider_id"], item["metadata"]["wave_index"]) for event, item in calls] == [
        ("provider_attempt", "anysearch", 2),
        ("provider_attempt", "codex_web_search", 2),
    ]
    assert all("query" not in item["metadata"] for _, item in calls)
