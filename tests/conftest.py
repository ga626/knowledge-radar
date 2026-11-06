"""外部发送用例的显式测试预算；不替换HTTP、不配置真实账号。"""
import json
import pytest


@pytest.fixture
def paid_test_policy(monkeypatch, tmp_path):
    from runtime.tool_trace import current_tool_trace, set_current_tool_trace
    policy = {"schema": "knowledgeradar-paid-policy/v3",
              "providers": {"wanfang": {"enabled": True}}}
    path = tmp_path / "paid-test-policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setenv("KR_PAID_POLICY_PATH", str(path))
    monkeypatch.setenv("KR_PAID_USAGE_PATH", str(tmp_path / "paid-test-usage.json"))
    previous = current_tool_trace()
    set_current_tool_trace({"trace_id": "fixture-explicit-authority"})
    try:
        yield
    finally:
        set_current_tool_trace(previous)


def patch_native(monkeypatch, call):
    """Site tests fake only the Native transport, not collector or governance."""
    from runtime.browser_link import BrowserLink
    def invoke(self, operation, payload, *, platform, timeout_s=30):
        result = call(operation=operation, platform=platform, **payload)
        if result.get('ok') and operation == 'search' and platform == 'boss':
            result.setdefault('data', {}).setdefault('query', payload.get('keyword', ''))
        return result
    monkeypatch.setattr(BrowserLink, 'request', invoke)
