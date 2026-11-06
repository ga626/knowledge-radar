"""付费兜底的真实HTTP发送边界；全部使用隔离账本和HTTP替身，不消费额度。"""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
import httpx
import pytest
from runtime import xhs_tikhub_usage as usage
from runtime import xhs_tikhub_fallback as paid

NOTE = "0123456789abcdef01234567"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("KR_XHS_TIKHUB_USAGE_PATH", str(tmp_path / "ledger.json"))
    monkeypatch.setenv("TIKHUB_API_KEY", "fixture-not-a-key")
    monkeypatch.setenv("KR_XHS_TIKHUB_BREAK_GLASS_DRY_RUN", "0")
    monkeypatch.delenv("KR_PAID_USAGE_PATH", raising=False)
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"schema": "knowledgeradar-paid-policy/v3"}), encoding="utf-8")
    monkeypatch.setenv("KR_PAID_POLICY_PATH", str(policy))
    monkeypatch.delenv("TIKHUB_BASE_URL", raising=False)


def claim(key="one", task="r1"):
    return usage.claim_paid_send("search", reservation_id=key, task_id=task)


def test_concurrent_same_reservation_authorizes_exactly_one_send():
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _:claim(), range(24)))
    assert sum(row["send"] for row in results) == 1
    assert usage.tikhub_usage_summary()["reserved_calls"] == 1


def test_one_daily_limit_across_tasks():
    assert claim()["send"]
    assert claim("two")["send"]
    assert claim("three", "r2")["reason_code"] == "TIKHUB_CALL_LIMIT"


def test_unknown_outcome_and_midnight_do_not_authorize_resend(monkeypatch):
    monkeypatch.setattr(usage, "_today", lambda:"2026-10-26")
    assert claim()["send"]
    monkeypatch.setattr(usage, "_today", lambda:"2026-10-27")
    assert claim()["reason_code"] == "TIKHUB_ALREADY_SENT_OR_RESERVED"
    usage.settle_paid_send("one", {"ok":True, "data":{"content":"saved"}})
    assert claim()["cached_result"]["data"]["content"] == "saved"


def test_corrupt_or_unpriced_ledger_never_resets_budget():
    path = usage.default_xhs_tikhub_usage_path()
    path.write_text("broken", encoding="utf-8")
    assert not claim()["send"]
    path.write_text(json.dumps({usage._today():{"reservations":{"old":{"kind":"search"}}}}), encoding="utf-8")
    assert claim()["send"]  # 旧无价格行仍占一次；次数配额不依赖金额。
    assert claim("third")["reason_code"] == "TIKHUB_CALL_LIMIT"


def mock_http(monkeypatch, payload=None, error=None):
    calls=[]
    class Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False
        def __enter__(self): return self
        def __exit__(self,*_): pass
        def get(self, url, **kwargs):
            calls.append((url,kwargs))
            if error: raise error
            return httpx.Response(200,json=payload)
    monkeypatch.setattr(paid.httpx,"Client",Client)
    return calls


def test_http_timeout_counts_once_and_never_retries(monkeypatch):
    calls=mock_http(monkeypatch,error=httpx.ReadTimeout("fixture"))
    first=paid.execute_paid_operation("search",{"keyword":"通勤包"},task_id="r1",trigger="SECURITY_BLOCK")
    second=paid.execute_paid_operation("search",{"keyword":"通勤包"},task_id="r1",trigger="SECURITY_BLOCK")
    assert len(calls)==1 and first["reason_code"]=="TIKHUB_OUTCOME_UNKNOWN"
    assert second["api_call_count"]==0
    assert usage.tikhub_usage_summary()["estimated_cny"]=="0.08"


@pytest.mark.parametrize("reason",["opencli_adapter_identity_mismatch","opencli_daily_browser_binding_required","worker_execution_error"])
def test_configuration_or_code_failure_is_not_a_spending_trigger(monkeypatch,reason):
    monkeypatch.setattr(paid,"_send",lambda *_:pytest.fail("spend on configuration defect"))
    assert not paid.execute_paid_operation("search",{"keyword":"q"},task_id="r1",trigger=reason)["ok"]


def test_empty_comments_are_success_only_with_recognized_schema(monkeypatch):
    calls=mock_http(monkeypatch,{"code":200,"data":{"comments":[]}})
    first=paid.execute_paid_operation("comments",{"note_id":NOTE},task_id="r1",trigger="COMMENTS_NOT_LOADED")
    assert first["ok"] and first["data"]==[] and len(calls)==1
    from runtime.xhs_tikhub_adapter import normalize_tikhub_xhs_comments
    with pytest.raises(ValueError,match="schema"):
        normalize_tikhub_xhs_comments({"data":{"unexpected":[]}})


def test_partial_browser_detail_only_pays_for_missing_comments(monkeypatch):
    calls=mock_http(monkeypatch,{"code":200,"data":{"comments":[{"id":"c1","content":"好用"}]}})
    result=paid.execute_paid_operation("detail",{"note_id":NOTE,"note_type":"image","include_comments":True,
        "partial":{"note_id":NOTE,"content":"已取得正文","completed_stages":["body","media"]}},
        task_id="r1",trigger="COMMENTS_NOT_LOADED")
    assert result["ok"] and result["data"]["content"]=="已取得正文"
    assert len(calls)==1 and calls[0][0].endswith("get_note_comments")


def test_unknown_note_type_never_pays_to_guess(monkeypatch):
    monkeypatch.setattr(paid,"_send",lambda *_:pytest.fail("paid guessing"))
    assert paid.execute_paid_operation("detail",{"note_id":NOTE},task_id="r1",trigger="empty_detail")["reason_code"]=="TIKHUB_NOTE_TYPE_REQUIRED"


def test_explicit_disable_sends_nothing(monkeypatch):
    from pathlib import Path
    import os
    calls=mock_http(monkeypatch,{"code":200,"data":{"items":[]}})
    Path(os.environ["KR_PAID_POLICY_PATH"]).write_text(json.dumps({"schema":"knowledgeradar-paid-policy/v3","providers":{"tikhub":{"enabled":False}}}),encoding="utf-8")
    assert paid.execute_paid_operation("search",{"keyword":"q"},task_id="r1",trigger="AUTH_REQUIRED")["reason_code"]=="TIKHUB_FALLBACK_DISABLED"
    assert calls==[]
