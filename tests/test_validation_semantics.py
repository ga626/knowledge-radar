import importlib.util
import sys
from pathlib import Path

from capabilities import build_capabilities, validation_semantics_manifest
from runtime.status_schema import ValidationStatus, classify_runtime_payload, classify_validation_status, validation_status_classes
from search_providers.models import WebSearchRequest
from search_providers.providers import AnySearchProvider, SearxngSearchProvider


def _load_validation_harness():
    path = Path(__file__).resolve().parents[1] / "tools" / "kr_full_validation_harness.py"
    spec = importlib.util.spec_from_file_location("kr_full_validation_harness_for_test", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_validation_harness_marks_optional_provider_cases() -> None:
    harness = _load_validation_harness()
    cases = {case.case_id: case for case in harness.low_risk_cases()}

    assert cases["web_search_auto"].required is True
    assert cases["web_search_auto"].role == "main_chain"
    assert cases["web_search_searxng"].required is False
    assert cases["web_search_searxng"].role == "optional_provider"
    assert cases["web_search_anysearch"].required is False
    assert cases["expand_keywords"].role == "legacy_helper"


def test_anysearch_status_declares_optional_fallback(monkeypatch) -> None:
    monkeypatch.delenv("ANYSEARCH_API_KEY", raising=False)
    monkeypatch.delenv("ANYSEARCH_SEARCH_ENDPOINT", raising=False)

    status = AnySearchProvider().status()

    assert status["role"] == "optional_fallback"
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


def test_searxng_status_declares_optional_fallback(monkeypatch) -> None:
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://127.0.0.1:1")

    status = SearxngSearchProvider(timeout=0.2).status()

    assert status["role"] == "optional_fallback"
    assert status["degraded_ok"] is True
    assert status["configured"] is True
    assert status["available"] is False


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


def test_required_blockers_are_strict() -> None:
    harness = _load_validation_harness()

    results = [
        harness.ValidationResult("required-ok", "tool", "PASS", 0.1, "low_risk", True, "main_chain"),
        harness.ValidationResult("required-degraded", "tool", "EXPECTED_DEGRADED", 0.1, "low_risk", True, "main_chain"),
        harness.ValidationResult("required-manual", "tool", "NEEDS_INTERACTION", 0.1, "low_risk", True, "main_chain"),
        harness.ValidationResult("optional-degraded", "tool", "EXPECTED_DEGRADED", 0.1, "low_risk", False, "optional_provider"),
    ]

    blockers = harness.find_required_blockers(results)

    assert [item.case_id for item in blockers] == ["required-manual"]


def test_verify_all_tools_requires_academic_capability_profiles() -> None:
    spec = importlib.util.spec_from_file_location(
        "verify_all_tools_for_test",
        Path(__file__).resolve().parents[1] / "scripts" / "verify_all_tools.py",
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    ok_payload = {
        "tools": {
            "search_academic": {
                "profile_schema": "knowledgeradar-academic-provider-profiles/v1",
                "provider_capability_profiles": {
                    "nssd": {},
                    "pubscholar": {},
                    "sciengine": {},
                    "vip_oa": {},
                },
            }
        }
    }
    bad_payload = {"tools": {"search_academic": {"provider_capability_profiles": {}}}}

    assert module._academic_profile_payload_error("get_capabilities", ok_payload) == ""
    assert "profile_schema" in module._academic_profile_payload_error("get_capabilities", bad_payload)
