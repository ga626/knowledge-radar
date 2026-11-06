"""独立配额的发送边界；只用隔离账本，不消耗真实额度。"""
import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
import pytest
from runtime import paid_execution as paid


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.delenv("KR_PAID_POLICY_PATH", raising=False)
    monkeypatch.setenv("KR_PAID_USAGE_PATH", str(tmp_path / "usage.json"))


def policy_file(monkeypatch, tmp_path, providers):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"schema": paid.DEFAULT_POLICY["schema"], "providers": providers}), encoding="utf-8")
    monkeypatch.setenv("KR_PAID_POLICY_PATH", str(path))


def send(provider, index=0, **kwargs):
    with paid.paid_send(provider, {"index": index, **kwargs.pop("payload", {})}, bound=20, task_id="research", **kwargs) as result:
        return result


def rows():
    return [row for day in paid.read_usage().values() for row in day.get("reservations", {}).values()]


def test_defaults_are_independent_and_usable():
    policy = paid.load_policy()
    assert policy["enabled"] is True and "daily_limit_cny" not in policy
    assert policy["providers"]["bailian"]["limit"] == "10"
    assert "serpapi_scholar" not in policy["providers"]
    assert paid.policy_summary()["global_budget"] is False


def test_legacy_policy_discards_global_amount_without_resetting_ledger(monkeypatch, tmp_path):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"schema": "knowledgeradar-paid-policy/v2", "daily_limit_cny": ".0001",
                               "providers": {"tikhub": {"enabled": False}}}), encoding="utf-8")
    monkeypatch.setenv("KR_PAID_POLICY_PATH", str(path))
    send("siliconflow_ocr")
    assert paid.load_policy()["providers"]["bailian"]["limit"] == "10"
    with pytest.raises(paid.PaidAdmissionError, match="provider_disabled"):
        send("tikhub")


def test_free_ocr_and_unknown_price_never_consume_bailian_budget(monkeypatch, tmp_path):
    policy_file(monkeypatch, tmp_path, {"bailian": {"limit": ".01"}})
    send("bailian", payload={"model": "qwen3.8-omni-flash", "max_tokens": 1})
    with pytest.raises(paid.PaidAdmissionError, match="money_limit"):
        send("bailian", 1, payload={"model": "qwen3.8-omni-flash", "max_tokens": 1})
    for index in range(4):
        send("siliconflow_ocr", index)
        send("unknown_service", index)
    assert {r["accounted_cny"] for r in rows() if r["provider"] != "bailian"} == {"0"}
    assert {r["cost_status"] for r in rows() if r["provider"] == "unknown_service"} == {"unknown_price"}


def test_search_and_tikhub_do_not_share_cap(monkeypatch, tmp_path):
    policy_file(monkeypatch, tmp_path, {"tavily": {"limit": "1"}})
    send("tavily")
    with pytest.raises(paid.PaidAdmissionError, match="free_quota_limit"):
        send("tavily", 1)
    from runtime.xhs_tikhub_usage import claim_paid_send, tikhub_usage_summary
    assert claim_paid_send("search", reservation_id="xhs1", task_id="r")["send"]
    assert claim_paid_send("detail", reservation_id="xhs2", task_id="r")["send"]
    assert claim_paid_send("comments", reservation_id="xhs3", task_id="other")["reason_code"] == "TIKHUB_CALL_LIMIT"
    assert tikhub_usage_summary()["reserved_calls"] == 2
    assert "global_daily_limit_cny" not in tikhub_usage_summary()
    send("brave")


def test_advanced_tavily_costs_two_credits_and_parallel_reservations_are_atomic(monkeypatch, tmp_path):
    policy_file(monkeypatch, tmp_path, {"tavily": {"limit": "3"}})
    send("tavily", payload={"search_depth": "advanced"})
    def attempt(index):
        try:
            send("tavily", index + 1)
            return 1
        except paid.PaidAdmissionError:
            return 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(16))) == 1
    assert sum(Decimal(r["quota_units"]) for r in rows()) == 3


def test_month_quota_survives_day_restart_but_resets_next_month(tmp_path):
    policy = paid.load_policy()
    policy["providers"]["tavily"]["limit"] = "2"
    path = tmp_path / "month.json"
    estimate = paid.estimate_request("tavily", {}, 1, policy)
    def claim(identity, day):
        return paid.claim_send("tavily", provider="tavily", reservation_id=identity, task_id="r",
                               ceiling="0", currency="CNY", path=path, day=day, policy=policy, estimate=estimate)
    assert claim("one", "2026-10-01")["send"]
    assert claim("two", "2026-10-20")["send"]
    assert claim("three", "2026-10-31")["reason_code"] == "FREE_QUOTA_LIMIT"
    assert claim("four", "2026-11-01")["send"]
    assert claim("one", "2026-11-01")["reason_code"] == "ALREADY_SENT_OR_RESERVED"


def test_external_same_account_usage_reduces_free_allowance(monkeypatch, tmp_path):
    current = paid._period(paid.DEFAULT_POLICY["providers"]["tavily"])
    policy_file(monkeypatch, tmp_path, {"tavily": {"external_used": "999", "external_period": current}})
    send("tavily")
    with pytest.raises(paid.PaidAdmissionError, match="free_quota_limit"):
        send("tavily", 1)


def test_free_allowance_cannot_be_increased_or_changed_to_money(monkeypatch, tmp_path):
    policy_file(monkeypatch, tmp_path, {"tavily": {"limit": "1001"}})
    with pytest.raises(paid.PaidAdmissionError, match="policy_invalid"):
        paid.load_policy()
    policy_file(monkeypatch, tmp_path, {"tavily": {"mode": "money"}})
    with pytest.raises(paid.PaidAdmissionError, match="policy_invalid"):
        paid.load_policy()


def test_openalex_utc_quota_and_anonymous_allowance():
    policy = paid.load_policy()
    auth = paid.estimate_request("openalex", {"search": "topic"}, 1, policy, authenticated=True)
    anon = paid.estimate_request("openalex", {"search": "topic"}, 1, policy, authenticated=False)
    assert auth["quota"]["limit"] == "1" and anon["quota"]["limit"] == "0.10"
    assert auth["units"] == anon["units"] == Decimal(".001")
    assert auth["quota"]["period"] == "utc_day"


def test_openalex_anonymous_cannot_spend_key_allowance(monkeypatch, tmp_path):
    policy_file(monkeypatch, tmp_path, {"openalex": {"external_used": ".099", "external_period": paid._period({"period": "utc_day"})}})
    send("openalex", payload={"search": "topic"}, authenticated=False)
    with pytest.raises(paid.PaidAdmissionError, match="free_quota_limit"):
        send("openalex", 1, payload={"search": "topic"}, authenticated=False)


def test_exa_auto_counts_extra_results_and_rejects_unpriced_variants():
    estimate = paid.estimate_request("exa", {"numResults": 20}, 1, paid.load_policy())
    assert estimate["units"] == Decimal(".017") and estimate["quota"]["limit"] == "10"
    with pytest.raises(paid.PaidAdmissionError, match="unpriced_search_variant"):
        send("exa", payload={"type": "deep"})


def test_custom_anysearch_is_unknown_not_standard_free():
    send("anysearch", endpoint="https://custom.example/search")
    send("anysearch", 1, endpoint="https://api.anysearch.com/v1/search")
    assert rows()[0]["quota_mode"] == "record" and rows()[0]["quota_units"] == "0"
    assert rows()[1]["quota_mode"] == "free_quota" and rows()[1]["quota_units"] == "1"


def test_standard_anysearch_rate_cap_does_not_apply_to_custom_endpoint(monkeypatch, tmp_path):
    policy_file(monkeypatch, tmp_path, {"anysearch": {"per_second_limit": 1}})
    send("anysearch", endpoint="https://api.anysearch.com/v1/search")
    with pytest.raises(paid.PaidAdmissionError, match="paid_rate_limit"):
        send("anysearch", 1, endpoint="https://api.anysearch.com/v1/search")
    send("anysearch", 2, endpoint="https://custom.example/search")


def test_unknown_send_never_retries_or_refunds():
    with pytest.raises(TimeoutError):
        with paid.paid_send("bailian", {"model": "qwen3.8-omni-flash"}, bound=20, task_id="research"):
            raise TimeoutError("outcome unknown")
    with pytest.raises(paid.PaidAdmissionError, match="already_sent"):
        _same_request()
    assert rows()[0]["outcome_status"] == "sent" and Decimal(rows()[0]["reserved_amount"]) > 0


def _same_request():
    with paid.paid_send("bailian", {"model": "qwen3.8-omni-flash"}, bound=20, task_id="research"):
        pytest.fail("repeated send")


def test_legacy_sent_payload_identity_cannot_be_reissued(tmp_path, monkeypatch):
    import hashlib
    payload, task = {"model": "qwen3.8-omni-flash"}, "research"
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    identity = hashlib.sha256(f"bailian|{task}|{digest}".encode()).hexdigest()
    path = Path(__import__("os").environ["KR_PAID_USAGE_PATH"])
    path.write_text(json.dumps({"2026-10-27": {"reservations": {identity: {"provider": "bailian", "outcome_status": "sent",
                                                                         "accounted_cny": ".5"}}}}), encoding="utf-8")
    with pytest.raises(paid.PaidAdmissionError, match="already_sent"):
        _same_request()


def test_request_bound_blocks_before_reservation():
    with pytest.raises(paid.PaidAdmissionError, match="request_too_large"):
        with paid.paid_send("tavily", {}, bound=32 * 1024 * 1024 + 1):
            pytest.fail("too large")
    assert not rows()


def test_retry_worker_generation_cannot_reset_identity(monkeypatch):
    from runtime.tool_trace import set_current_tool_trace
    set_current_tool_trace({})
    monkeypatch.setenv("KR_PAID_TASK_SCOPE", "logical-request")
    monkeypatch.setenv("KR_DURABLE_TASK_ID", "first")
    with pytest.raises(TimeoutError):
        with paid.paid_send("bailian", {}, bound=3):
            raise TimeoutError()
    monkeypatch.setenv("KR_DURABLE_TASK_ID", "second")
    with pytest.raises(paid.PaidAdmissionError, match="already_sent"):
        with paid.paid_send("bailian", {}, bound=3):
            pytest.fail("new spend")


def test_conflicting_default_ledgers_fail_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("KR_PAID_USAGE_PATH", raising=False)
    monkeypatch.delenv("KR_XHS_TIKHUB_USAGE_PATH", raising=False)
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path))
    for name in ("paid_usage.json", "xhs_tikhub_usage.json"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    assert paid.claim_send("test", reservation_id="one", task_id="r", ceiling=".01")["send"] is False


def test_cross_process_calls_cap_is_atomic(tmp_path):
    import os
    import subprocess
    import sys
    code = """
import sys
sys.path.insert(0,sys.argv[3])
from runtime.paid_execution import claim_send
print(int(claim_send("fixture", reservation_id=sys.argv[2], task_id="r", ceiling=".01", path=sys.argv[1])["send"]))
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    processes = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path / "multiprocess.json"), str(i), env["PYTHONPATH"]],
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(6)]
    admitted = 0
    for process in processes:
        output, error = process.communicate(timeout=20)
        assert process.returncode == 0, error
        admitted += int(output.strip())
    assert admitted == 2


@pytest.mark.parametrize("bad", ["-1", "NaN", "Infinity"])
def test_corrupt_historical_amount_never_resets_quota(tmp_path, bad):
    path = tmp_path / "broken.json"
    path.write_text(json.dumps({"day": {"reservations": {"old": {"accounted_cny": bad}}}}), encoding="utf-8")
    assert paid.claim_send("fixture", reservation_id="new", task_id="r", ceiling=".1", path=path)["reason_code"] == "BUDGET_OR_LEDGER_INVALID"


@pytest.mark.parametrize("endpoint,expected", [
    ("https://dashscope.aliyuncs.com/compatible-mode/v1", ".00093"),
    ("https://dashscope-intl.aliyuncs.com/compatible-mode/v1", ".0012413"),
])
def test_bailian_returned_usage_includes_cache_and_media(endpoint, expected):
    payload = {"model": "qwen3.8-omni-flash", "input": {"image": "https://example.org/image"}}
    with paid.paid_send("bailian", payload, bound=20, task_id="usage", endpoint=endpoint) as reservation:
        pass
    paid.settle_usage(reservation["reservation_id"], provider="bailian", model=payload["model"],
                      usage={"input_tokens": 1000, "cached_tokens": 200, "output_tokens": 100})
    row = rows()[0]
    # 北京 (800*.8+200*.1+100*2.7)/1e6 = .00093；
    # 新加坡 (800*1.094+200*.117+100*3.427)/1e6 = .0012413。
    assert Decimal(row["accounted_cny"]) == Decimal(expected) and row["cost_status"] == "usage_priced_estimate"
    assert row["quota_units"] == row["accounted_cny"]


def test_missing_usage_keeps_media_reservation():
    result = send("bailian", payload={"model": "qwen3.8-omni-flash", "input": {"audio": "https://example.org/a"}})
    paid.settle_usage(result["reservation_id"], provider="bailian", model="qwen3.8-omni-flash", usage={"usage_reported": False})
    assert Decimal(rows()[0]["accounted_cny"]) == Decimal(".50")


def test_degradation_preserves_nonretryable_quota_reason(tmp_path):
    from runtime.degradation import DegradationPolicy
    policy = DegradationPolicy(str(tmp_path / "degradation.sqlite3"), str(tmp_path / "events.jsonl"))
    def blocked():
        raise paid.PaidAdmissionError("paid_free_quota_limit")
    with pytest.raises(paid.PaidAdmissionError, match="paid_free_quota_limit"):
        policy.retry_with_jitter("fixture", "model", blocked, retryable_exceptions=(RuntimeError,))


def test_disabled_provider_does_not_send():
    with pytest.raises(paid.PaidAdmissionError, match="provider_disabled"):
        send("wanfang")
    assert not rows()


@pytest.mark.parametrize("class_name,provider,units", [
    ("TavilySearchProvider", "tavily", "1"), ("BraveSearchProvider", "brave", ".005"),
    ("ExaSearchProvider", "exa", ".017"), ("AnySearchProvider", "anysearch", "1"),
])
def test_real_search_adapter_reserves_its_own_units(monkeypatch, class_name, provider, units):
    from search_providers import providers
    from search_providers.models import WebSearchRequest
    monkeypatch.setattr(providers, "request_json", lambda *_a, **_k: {"results": [], "web": {"results": []}})
    adapter = getattr(providers, class_name)(api_key="fixture-secret")
    if provider == "anysearch":
        adapter.endpoint = "https://api.anysearch.com/v1/search"
    adapter.search(WebSearchRequest(query="budget adapter regression", limit=20))
    assert len(rows()) == 1 and rows()[0]["provider"] == provider
    assert Decimal(rows()[0]["quota_units"]) == Decimal(units)
    assert "fixture-secret" not in json.dumps(rows())


def test_openalex_http_boundary_uses_anonymous_quota(monkeypatch):
    import httpx
    from academic_providers.openalex import OpenAlexProvider
    from academic_providers.models import AcademicSearchRequest
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def get(self, url, **kwargs):
            return httpx.Response(200, request=httpx.Request("GET", url), json={"results": []})
    monkeypatch.setattr("academic_providers.openalex.httpx.Client", Client)
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    provider = OpenAlexProvider()
    assert provider.status()["quota"]["free_credit_per_day_usd"] == .10
    assert provider.search(AcademicSearchRequest(query="quota wiring")) == []
    assert rows()[0]["provider"] == "openalex" and Decimal(rows()[0]["quota_units"]) == Decimal(".001")


def test_serpapi_retains_adapter_without_entering_cost_manager(monkeypatch, tmp_path):
    import httpx
    from academic_providers.serpapi_scholar import SerpApiScholarProvider
    from academic_providers.models import AcademicSearchRequest
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def get(self, url, **kwargs):
            return httpx.Response(200, request=httpx.Request("GET", url), json={"organic_results": []})
    monkeypatch.setattr("academic_providers.serpapi_scholar.httpx.Client", Client)
    monkeypatch.setenv("SERPAPI_API_KEY", "fixture-secret")
    monkeypatch.setenv("KR_ACADEMIC_SERPAPI_USAGE_PATH", str(tmp_path / "academic.json"))
    monkeypatch.setenv("KR_ACADEMIC_ENABLE_SERPAPI", "true")
    monkeypatch.setattr(paid, "load_policy", lambda: pytest.fail("SerpAPI entered unrelated cost manager"))
    assert SerpApiScholarProvider().search(AcademicSearchRequest(query="adapter isolation")) == []
    assert not rows()
