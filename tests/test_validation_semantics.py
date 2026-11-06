from pathlib import Path

from capabilities import build_capabilities, validation_semantics_manifest
from runtime.status_schema import ValidationStatus, classify_runtime_payload, classify_validation_status, validation_status_classes
from search_providers.models import WebSearchRequest
from search_providers.providers import AnySearchProvider


def test_current_evaluation_suite_has_explicit_layers_and_optional_provider_policy():
    from scripts.kr_eval.core import load_suite, inventory

    root = Path(__file__).resolve().parents[1]
    suite = load_suite(root)
    assert inventory(root, suite)["issues"] == []
    cases = {case["case_id"]: case for case in suite["cases"]}
    assert cases["content.contracts"]["layer"] == "fixture"
    assert cases["flow.search_to_detail"]["executor"] == "pytest"
    assert cases["live.get_content_detail"]["auto"] is False
    assert not (root / "tools/kr_full_validation_harness.py").exists()


def test_anysearch_status_declares_optional_fallback(monkeypatch) -> None:
    monkeypatch.delenv("ANYSEARCH_API_KEY", raising=False)
    monkeypatch.delenv("ANYSEARCH_SEARCH_ENDPOINT", raising=False)

    status = AnySearchProvider().status()

    assert status["role"] == "preferred"
    assert status["degraded_ok"] is True
    assert status["configured"] is False
    assert status["available"] is False

    provider = AnySearchProvider()
    try:
        provider.search(WebSearchRequest(query="should not call network", limit=1))
    except Exception as exc:
        assert getattr(exc, "error_type", "") == "not_configured_optional"
    else:
        raise AssertionError("AnySearchProvider without endpoint/key should not run a network request")


def test_capabilities_explain_expected_degraded_paths(tmp_path) -> None:
    provider_status = {
        "tavily": {"configured": True, "available": True},
        "searxng": {"configured": False, "available": False, "role": "optional_fallback", "degraded_ok": True},
    }
    caps = build_capabilities(
        decision_log_path=str(tmp_path / "decision.jsonl"),
        provider_status=lambda: provider_status,
    )

    semantics = caps["validation_semantics"]

    assert semantics["schema"] == "knowledgeradar-validation-semantics/v1"
    assert "searxng" in semantics["optional_expected_degraded"]["web_search_providers"]
    assert "bilibili_raw_cdn_native_media" in semantics["designed_fallbacks"]
    assert "native_media_requires_provider_downloadable_url" in semantics["designed_fallbacks"]
    assert "xiaohongshu_image_ocr_background" in semantics["designed_fallbacks"]
    assert validation_semantics_manifest(provider_status)["overall_pass_rule"].startswith("Only FAIL")
    assert semantics["status_classes"]["EXPECTED_DEGRADED"]["blocks_overall_pass"] is False
    assert semantics["blocking_statuses"] == ["FAIL", "NEEDS_INTERACTION"]


def test_shared_validation_status_schema_keeps_expected_degraded_non_blocking() -> None:
    classes = validation_status_classes()

    assert set(classes) == {item.value for item in ValidationStatus}
    assert classify_validation_status("EXPECTED_DEGRADED", required=False)["blocks_overall_pass"] is False
    assert classify_validation_status("EXPECTED_DEGRADED", required=True)["blocks_overall_pass"] is False
    assert classify_validation_status("unknown", required=True)["status"] == "FAIL"
    degraded = classify_runtime_payload({"status": "degraded", "degraded_reason": "quota"}, required=True, main_chain=True)
    assert degraded["classification"] == "EXPECTED_DEGRADED"
    assert degraded["blocks_overall_pass"] is False


def test_manual_login_and_captcha_errors_require_interaction() -> None:
    cases = [
        {"status": "error", "error_type": "login_required"},
        {"status": "failed", "error": {"type": "anti_bot_verification"}},
        {"status": "error", "manual_action": "captcha"},
        {"status": "error", "reason": "captcha_required"},
    ]

    for payload in cases:
        result = classify_runtime_payload(payload)
        assert result["classification"] == "NEEDS_INTERACTION"
        assert result["blocks_overall_pass"] is True


def test_available_main_chain_login_provider_is_not_interaction_blocker() -> None:
    result = classify_runtime_payload(
        {
            "status": "available",
            "requires_login": True,
            "manual_action": "Use managed browser login if the session expires.",
        },
        required=True,
        main_chain=True,
    )

    assert result["classification"] == "PASS"
    assert result["blocks_overall_pass"] is False


def test_runtime_error_normalization_keeps_manual_and_network_boundaries() -> None:
    from runtime.status_schema import normalize_error_code

    assert normalize_error_code("request timed out") == "network_timeout"
    assert normalize_error_code("HTTP 429 rate limit") == "rate_limited"
    assert normalize_error_code("cookie unauthorized") == "login_required"
    assert normalize_error_code("captcha verification required") == "anti_bot_verification"
    assert normalize_error_code("policy blocked") == "policy_denied"


def test_current_comparison_blocks_pass_regression_and_missing_cases():
    from scripts.kr_eval.core import compare

    def run(verdict):
        return {"run_id": verdict, "target": "development_source", "environment": {"fingerprint": "fixture"},
                "results": [{"case_id": "fixture", "verdict": verdict, "comparison_identity": "same", "layer": "fixture"}]}

    assert compare(run("PASS"), run("FAIL"))["gate"] == "BLOCKED"
    missing = run("PASS")
    missing["results"] = []
    assert compare(run("PASS"), missing)["gate"] == "INCOMPLETE"


def test_current_capabilities_require_academic_provider_profiles(tmp_path):
    payload = build_capabilities(decision_log_path=str(tmp_path / "decisions.jsonl"), provider_status=lambda: {})
    academic = payload["tools"]["search_academic"]
    assert academic["profile_schema"] == "knowledgeradar-academic-provider-profiles/v1"
    assert {"nssd", "pubscholar", "sciengine", "vip_oa"} <= set(academic["provider_capability_profiles"])
    assert not (Path(__file__).resolve().parents[1] / "scripts/verify_all_tools.py").exists()
