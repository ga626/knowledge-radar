from __future__ import annotations

from runtime.campaign_gates import (
    campaign_agent_sentinel_prompt,
    campaign_profile_manifest,
    run_agent_sentinel_contract,
    run_campaign_deep_checks,
    run_campaign_destructive_heavy_runner,
    run_campaign_destructive_checks,
    run_campaign_fault_injection,
    run_campaign_tool_readiness,
)
from runtime.quality_gates import classify_campaign_status
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_campaign_manifest_defines_unified_profiles() -> None:
    manifest = campaign_profile_manifest()

    assert manifest["schema"] == "knowledgeradar-campaign-profiles/v1"
    assert manifest["status"] == "pass"
    assert manifest["default_profile"] == "smoke"
    assert manifest["profiles"]["smoke"]["token_cost"] == "none"
    assert manifest["profiles"]["agent-sentinel"]["token_cost"] == "low_when_run_by_agent"


def test_campaign_fault_injection_keeps_known_degraded_non_blocking() -> None:
    result = run_campaign_fault_injection()

    assert result["status"] == "pass"
    by_case = {row["case"]: row["classification"] for row in result["results"]}
    assert by_case["provider_timeout"] == "EXPECTED_DEGRADED"
    assert by_case["unknown_candidate"] == "EXPECTED_DEGRADED"
    assert by_case["login_required"] == "NEEDS_INTERACTION"
    assert by_case["anti_bot_manual"] == "NEEDS_INTERACTION"
    assert by_case["main_chain_error"] == "FAIL"
    assert by_case["unknown_main_chain"] == "FAIL"


def test_campaign_deep_checks_cover_core_contracts() -> None:
    result = run_campaign_deep_checks(ROOT)

    assert result["schema"] == "knowledgeradar-campaign-deep-checks/v1"
    assert result["status"] == "pass"
    names = {check["name"] for check in result["checks"]}
    assert {
        "status_classification_matrix",
        "route_policy_matrix",
        "url_parsing_fuzz",
        "provider_status_matrix",
        "task_fanin",
        "patrol_side_effect_and_verdict_contract",
    } <= names
    assert not result["failures"]


def test_campaign_destructive_checks_are_local_and_nonblocking_when_tools_missing() -> None:
    result = run_campaign_destructive_checks(ROOT, auto_install=False, run_heavy=False)

    assert result["schema"] == "knowledgeradar-campaign-destructive-checks/v1"
    assert result["status"] in {"pass", "expected_degraded"}
    names = {check["name"] for check in result["checks"]}
    assert {
        "destructive_url_fuzz",
        "destructive_status_mutation",
        "destructive_task_scope_collision_probe",
    } <= names
    assert not result["failures"]


def test_destructive_heavy_runner_degrades_when_docker_unavailable(monkeypatch) -> None:
    import runtime.campaign_gates as campaign_gates

    monkeypatch.setattr(campaign_gates, "_docker_available", lambda: {"available": False, "reason": "docker_missing"})

    result = run_campaign_destructive_heavy_runner(ROOT)

    assert result["status"] == "expected_degraded"
    assert result["detail"]["reason"] == "docker_unavailable"


def test_destructive_heavy_runner_passes_when_docker_smoke_passes(monkeypatch) -> None:
    import runtime.campaign_gates as campaign_gates

    monkeypatch.setattr(campaign_gates, "_docker_available", lambda: {"available": True, "version": "Docker"})
    monkeypatch.setattr(
        campaign_gates,
        "_docker_heavy_runner_smoke",
        lambda _root: {"command": "docker", "returncode": 0, "stdout": "ok", "stderr": ""},
    )

    result = run_campaign_destructive_heavy_runner(ROOT)

    assert result["status"] == "pass"
    assert result["detail"]["smoke"]["returncode"] == 0


def test_campaign_status_unknown_optional_is_expected_degraded() -> None:
    result = classify_campaign_status({"status": "mystery", "main_chain": False, "role": "candidate"})

    assert result["classification"] == "EXPECTED_DEGRADED"


def test_tool_readiness_declares_optional_heavy_tools() -> None:
    readiness = run_campaign_tool_readiness(auto_install=False)

    assert readiness["schema"] == "knowledgeradar-campaign-tool-readiness/v1"
    assert "atheris" in readiness["tools"]
    assert "mutmut" in readiness["tools"]
    assert "details" in readiness
    assert readiness["details"]["atheris"]["runner"] == "docker"
    assert readiness["details"]["mutmut"]["native_windows_path"] == "removed"


def test_tool_readiness_uses_docker_only_for_heavy_tools(monkeypatch) -> None:
    import runtime.campaign_gates as campaign_gates

    monkeypatch.setattr(campaign_gates, "_docker_available", lambda: {"available": True, "version": "Docker"})

    readiness = run_campaign_tool_readiness(auto_install=True)
    assert readiness["status"] == "pass"
    assert readiness["expected_degraded"] == []
    assert readiness["tools"]["atheris"] is True
    assert readiness["tools"]["mutmut"] is True
    assert readiness["details"]["atheris"]["install_status"] == "not_applicable"
    assert readiness["details"]["mutmut"]["install_status"] == "not_applicable"


def test_tool_readiness_degrades_heavy_tools_when_docker_unavailable(monkeypatch) -> None:
    import runtime.campaign_gates as campaign_gates

    monkeypatch.setattr(campaign_gates, "_docker_available", lambda: {"available": False, "reason": "docker_missing"})

    readiness = run_campaign_tool_readiness(auto_install=True)

    assert readiness["status"] == "expected_degraded"
    assert readiness["expected_degraded"] == ["atheris", "mutmut"]
    assert readiness["details"]["atheris"]["install_attempted"] is False
    assert readiness["details"]["atheris"]["reason"] == "docker_unavailable"
    assert readiness["details"]["mutmut"]["install_attempted"] is False


def test_agent_sentinel_prompt_is_minimal_mcp_contract() -> None:
    prompt = campaign_agent_sentinel_prompt()
    contract = run_agent_sentinel_contract()

    assert "health_check(mode='summary')" in prompt
    assert "get_capabilities(summary=true)" in prompt
    assert "Do not use built-in web_search/web_fetch" in prompt
    assert contract["token_budget"]["target_total_tokens"] <= 20000
