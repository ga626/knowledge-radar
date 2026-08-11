import importlib.util
from pathlib import Path
import sys

from capabilities import build_tool_surface, capability_atlas_manifest, media_policy_manifest, research_quality_contract_manifest, source_ecology_manifest, validation_semantics_manifest
from capabilities import build_capabilities
from runtime.runtime_environment import planning_tools_manifest, runtime_environment_manifest


_VERIFY_ALL_CAPABILITIES = Path(__file__).resolve().parents[1] / "scripts" / "verify_all_capabilities.py"
_VERIFY_SPEC = importlib.util.spec_from_file_location("verify_all_capabilities", _VERIFY_ALL_CAPABILITIES)
assert _VERIFY_SPEC and _VERIFY_SPEC.loader
_VERIFY_MODULE = importlib.util.module_from_spec(_VERIFY_SPEC)
sys.modules[_VERIFY_SPEC.name] = _VERIFY_MODULE
_VERIFY_SPEC.loader.exec_module(_VERIFY_MODULE)
_server_tools = _VERIFY_MODULE._server_tools


def test_tool_surface_defaults_to_planning_tools_hidden(monkeypatch) -> None:
    monkeypatch.delenv("KR_EXPOSE_PLANNING_TOOLS", raising=False)

    surface = build_tool_surface()

    assert surface["actual_mcp_tool_count"] == 22
    assert "kr_research" in surface["actual_mcp_tools"]
    assert "finalize_research_task" in surface["actual_mcp_tools"]
    assert surface["workflow_tools"]["kr_research"]["role"] == "high_level_research_perception_entry"
    assert surface["workflow_tools"]["kr_research"]["host_internal_web_policy"]["wave_id"] == "host_internal_web_wave"
    assert surface["workflow_tools"]["kr_research"]["host_internal_web_policy"]["strategy_tree"] == "web_search.provider_wave"
    assert "expand_keywords" not in surface["actual_mcp_tools"]
    assert "plan_research" not in surface["actual_mcp_tools"]
    assert "search_github_repositories" in surface["actual_mcp_tools"]
    assert "search_youtube" in surface["actual_mcp_tools"]
    assert "search_wechat_articles" in surface["actual_mcp_tools"]
    assert "manage_xiaohongshu_accounts" in surface["actual_mcp_tools"]
    assert "record_research_candidates_tool" in surface["actual_mcp_tools"]
    assert "advance_research_candidate" in surface["actual_mcp_tools"]
    assert "review_research_progress" in surface["actual_mcp_tools"]
    assert surface["virtual_capabilities"] == []
    assert surface["deprecated_provider_aliases"]["kr_web_search(provider='github')"] == "search_github_repositories"
    assert surface["deprecated_provider_aliases"]["kr_web_search(provider='youtube')"] == "search_youtube"
    assert surface["planning_tools"]["enabled"] is False
    assert surface["legacy_hidden_tools"] == ["expand_keywords", "plan_research"]


def test_tool_surface_does_not_advertise_local_legacy_helpers(monkeypatch) -> None:
    monkeypatch.setenv("KR_EXPOSE_PLANNING_TOOLS", "true")

    surface = build_tool_surface()

    assert surface["actual_mcp_tool_count"] == 22
    assert "kr_research" in surface["actual_mcp_tools"]
    assert "expand_keywords" not in surface["actual_mcp_tools"]
    assert "plan_research" not in surface["actual_mcp_tools"]
    assert "search_github_repositories" in surface["actual_mcp_tools"]
    assert "search_youtube" in surface["actual_mcp_tools"]
    assert "search_wechat_articles" in surface["actual_mcp_tools"]
    assert "manage_xiaohongshu_accounts" in surface["actual_mcp_tools"]
    assert surface["legacy_hidden_tools"] == ["expand_keywords", "plan_research"]
    assert surface["planning_tools"]["enabled"] is False
    assert surface["planning_tools"]["configured_enabled"] is True
    assert surface["planning_tools"]["mcp_registered"] is False


def test_capability_surface_matches_server_registered_tools(monkeypatch) -> None:
    monkeypatch.delenv("KR_EXPOSE_PLANNING_TOOLS", raising=False)
    import server

    surface = build_tool_surface()
    registered = sorted(server.mcp._tool_manager._tools)

    assert sorted(surface["actual_mcp_tools"]) == registered


def test_verify_all_capabilities_covers_actual_tool_surface(monkeypatch) -> None:
    monkeypatch.delenv("KR_EXPOSE_PLANNING_TOOLS", raising=False)

    surface = build_tool_surface()
    verifier_calls = _server_tools(object(), safe=True, wait_multimodal_s=1.0, include_multimodal=False, include_xhs_fallback=False)

    missing = sorted(set(surface["actual_mcp_tools"]) - set(verifier_calls))
    assert missing == []


def test_verify_all_capabilities_wait_for_task_accepts_single_task_response() -> None:
    class SingleTaskStatusServer:
        def get_task_status(self, **_: object) -> dict[str, object]:
            return {
                "task": {
                    "task_id": "task-1",
                    "status": "completed",
                    "result": {"available": True},
                }
            }

    task = _VERIFY_MODULE._wait_for_task(SingleTaskStatusServer(), "task-1", timeout_s=0.1, poll_s=0.01)

    assert task["task_id"] == "task-1"
    assert task["status"] == "completed"
    assert task["result"]["available"] is True


def test_runtime_environment_manifest_declares_productization_boundaries(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("KR_LOG_DIR", str(tmp_path / "runtime" / "logs"))
    monkeypatch.setenv("KR_BROWSER_DATA_DIR", str(tmp_path / "browser_data"))
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "runtime" / "ms-playwright"))
    monkeypatch.setenv("KR_PROFILE_REGISTRY_PATH", str(tmp_path / "config" / "profile_registry.json"))
    monkeypatch.setenv("KR_PROXY_RULE_CACHE_DIR", str(tmp_path / "runtime" / "proxy_rules"))
    monkeypatch.setenv("KR_WHISPER_MODEL_DIR", str(tmp_path / "runtime" / "models" / "whisper"))
    monkeypatch.setenv("KR_TASK_DB_PATH", str(tmp_path / "runtime" / "logs" / "tasks.sqlite3"))
    monkeypatch.setenv("KR_EXPOSE_PLANNING_TOOLS", "0")

    manifest = runtime_environment_manifest()

    assert manifest["schema"] == "knowledgeradar-runtime-environment/v1"
    assert manifest["paths"]["state_dir"]["configured_by"] == "KR_STATE_DIR"
    assert manifest["paths"]["log_dir"]["configured_by"] == "KR_LOG_DIR"
    assert manifest["paths"]["browser_data_dir"]["release_policy"] == "exclude_runtime_or_secret"
    assert manifest["paths"]["proxy_rule_cache_dir"]["configured_by"] == "KR_PROXY_RULE_CACHE_DIR"
    assert manifest["paths"]["whisper_model_cache_dir"]["configured_by"] == "KR_WHISPER_MODEL_DIR"
    assert manifest["planning_tools"]["enabled"] is False
    assert manifest["proxy_rules"]["schema"] == "knowledgeradar-proxy-rules/v1"
    assert manifest["task_worker"]["schema"] == "knowledgeradar-task-worker/v1"
    assert manifest["dependencies"]["external"]["schema"] == "knowledgeradar-external-dependency-preflight/v1"
    assert "runtime/media_cache/**" in manifest["release_boundaries"]["exclude"]
    assert "config/package.env.example" in manifest["release_boundaries"]["include_templates_only"]


def test_media_policy_notes_video_url_spike_not_main_chain(monkeypatch) -> None:
    monkeypatch.setenv("KR_FRAME_VISION_MODELS", "bailian:qwen3-vl-flash")
    monkeypatch.setenv("KR_COMMENT_FILTER_MODELS", "bailian:qwen-turbo,bailian:qwen3.5-flash")

    policy = media_policy_manifest()

    assert policy["storage_policy_status"] == "implemented_p0_4"
    assert policy["provider_primitives"]["bailian"]["status"] == "implemented_main_provider_p1_1_to_p1_4"
    assert policy["provider_primitives"]["bailian"]["default_model"] == "qwen3-vl-flash"
    assert "cached_tokens" in policy["provider_primitives"]["bailian"]["usage_fields"]
    assert policy["model_configuration"]["KR_FRAME_VISION_MODELS"]["models"] == ["bailian:qwen3-vl-flash"]
    assert policy["model_configuration"]["KR_COMMENT_FILTER_MODELS"]["models"][0] == "bailian:qwen-turbo"
    assert policy["provider_primitives"]["siliconflow"]["status"] == "implemented_manual_fallback_p1_5"
    assert policy["provider_primitives"]["mimo"]["status"] == "explicit_fallback_only_p1_5"
    assert policy["direct_media_probe"]["status"] == "implemented_p1_2"
    assert policy["direct_media_probe"]["platforms"]["bilibili"]["provider_downloadability"].startswith("provider_blocked")
    assert "validation_status" in policy["direct_media_probe"]["generic_rule"]
    assert any("provider_downloadability" in note for note in policy["notes"])


def test_validation_semantics_declares_bilibili_fallback_acceptance() -> None:
    semantics = validation_semantics_manifest(
        {},
        {
            "nssd": {
                "validation_status": "PASS",
                "provider_tier": "p2_4_default_chinese_fulltext",
                "capability_profile": {"role": "default_chinese_fulltext"},
            },
            "baidu_scholar": {"validation_status": "EXPECTED_DEGRADED", "provider_tier": "expected_degraded_official_api_when_unprovisioned", "requires_api_key": True},
            "wanfang": {"validation_status": "EXPECTED_DEGRADED", "provider_tier": "expected_degraded_external_login_or_subscription", "requires_login": True},
        },
    )

    assert semantics["bilibili_multimodal_acceptance"]["native_video_url_provider_blocked"] == "EXPECTED_DEGRADED"
    assert semantics["academic_provider_matrix"]["status_counts"]["EXPECTED_DEGRADED"] == 2
    assert semantics["academic_provider_matrix"]["profile_schema"] == "knowledgeradar-academic-provider-profiles/v1"
    assert semantics["academic_provider_matrix"]["default_chinese_fulltext"] == ["nssd"]
    assert semantics["academic_provider_matrix"]["p2_4_default_chinese_fulltext"] == ["nssd"]
    assert "wanfang" in semantics["optional_expected_degraded"]["academic_login_or_entitlement_boundaries"]


def test_planning_tools_manifest_is_agent_readable(monkeypatch) -> None:
    monkeypatch.setenv("KR_EXPOSE_PLANNING_TOOLS", "false")

    manifest = planning_tools_manifest()

    assert manifest["mode"] == "legacy_local_helpers_not_requested"
    assert manifest["default"] == "not_mcp_registered"
    assert manifest["enabled"] is False
    assert manifest["mcp_registered"] is False
    assert "trace" in manifest["canary_requirement"]


def test_capabilities_declares_p2_2_asr_strategy(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path / "runtime"))
    caps = build_capabilities(decision_log_path=str(tmp_path / "decision.jsonl"), provider_status=lambda: {})

    assert caps["strategy_trees"]["asr"]["status"] == "implemented_p2_2"
    assert caps["resource_concurrency"]["status"] == "implemented_p2_2"
    assert caps["task_scope_fanin"]["status"] == "implemented"
    assert caps["task_scope_fanin"]["legacy_alias"]["field"] == "research_session_id"
    assert caps["tools"]["get_task_status"]["polling"]["task_scope_fanin"].startswith("get_task_status")
    assert caps["asr_lifecycle"]["status"] == "implemented_p2_2_policy_probe"
    assert caps["asr_lifecycle"]["default_binding"].startswith("server_run")
    assert caps["asr_benchmark"]["schema"] == "knowledgeradar-asr-benchmark/v2"
    assert caps["tools"]["get_content_detail"]["bilibili_asr_fast_path"]["status"] == "implemented_p2_2_strategy_tree"


def test_research_quality_contract_declares_budget_profiles() -> None:
    contract = research_quality_contract_manifest()

    assert contract["schema"] == "knowledgeradar-research-quality-contract/v1"
    assert contract["status"] == "implemented_p1_questioner_and_p2_to_p5_governance"
    assert contract["evidence_sidecar_schema"] == "knowledgeradar-research-evidence/v1"
    assert [item["id"] for item in contract["research_budget_profiles"]] == ["fast", "balanced", "deep"]
    assert contract["questioner_checkpoints"]["minimum_stages"] == [
        "after_local_archaeology",
        "after_first_external_round",
        "before_final_report",
    ]
    assert contract["repair_request_contract"]["agent_autonomy"] == "agent_decides_how_to_repair"
    assert "Agent" in contract["repair_request_contract"]["agent_autonomy_zh"]
    assert "local_code" in contract["evidence_surfaces"]
    assert "media_detail" in contract["evidence_surfaces"]
    assert contract["layer_boundaries"]["quality_check"].startswith("检查")
    assert contract["detail_gap_governance"]["schema"] == "knowledgeradar-detail-gap-governance/v1"
    assert "source_ecology" in contract["evidence_surfaces"]
    assert "来源生态" in contract["layer_boundaries"]["source_ecology"]
    assert "最小稳定边界" in contract["layer_boundaries"]["capability_atlas"]


def test_source_ecology_manifest_is_open_and_agent_readable() -> None:
    manifest = source_ecology_manifest()

    assert manifest["schema"] == "knowledgeradar-source-ecologies/v1"
    ecologies = manifest["ecologies"]
    wechat = ecologies["wechat_public_article_ecology"]
    assert "search_wechat_articles" in wechat["candidate_tools"]
    assert "kr_web_search" in wechat["candidate_tools"]
    assert "专业机构文章" in wechat["not_limited_to"]
    assert "某一个研究领域" in wechat["not_limited_to"]
    assert "自媒体长文" in wechat["content_forms"]
    assert "bilibili_video_ecology" in ecologies
    assert "xiaohongshu_experience_ecology" in ecologies
    assert "github_repository_ecology" in ecologies


def test_capability_atlas_declares_min_max_boundaries() -> None:
    atlas = capability_atlas_manifest()

    assert atlas["schema"] == "knowledgeradar-capability-atlas/v1"
    assert atlas["status"] == "implemented_p2"
    assert "minimum_stable_boundary" in atlas["upgrade_rules"]
    bilibili = atlas["cards"]["bilibili_video_ecology"]
    assert "search_bilibili" in bilibili["candidate_tools"]
    assert bilibili["minimum_stable_boundary"]
    assert bilibili["maximum_observed_boundary"]
    assert bilibili["stability_rule"].startswith("single success")


def test_platform_onboarding_policy_exposes_agent_judgement_principles() -> None:
    from capabilities import platform_onboarding_policy

    policy = platform_onboarding_policy()

    principles = policy["agent_judgement_principles"]
    assert any("turn uncertainty into evidence" in item for item in principles)
    assert any("real URL" in item for item in principles)
    assert any("KR runtime probes" in item for item in principles)


def test_capabilities_exposes_research_quality_contract(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path / "runtime"))
    caps = build_capabilities(decision_log_path=str(tmp_path / "decision.jsonl"), provider_status=lambda: {})

    assert caps["research_quality_contract"]["schema"] == "knowledgeradar-research-quality-contract/v1"
    assert [item["id"] for item in caps["research_quality_contract"]["research_budget_profiles"]] == ["fast", "balanced", "deep"]
    assert caps["source_ecologies"]["schema"] == "knowledgeradar-source-ecologies/v1"
    assert caps["tools"]["search_wechat_articles"]["source_ecologies"] == ["wechat_public_article_ecology"]


def test_capabilities_exposes_academic_provider_capability_profiles(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path / "runtime"))

    caps = build_capabilities(decision_log_path=str(tmp_path / "decision.jsonl"), provider_status=lambda: {})
    academic = caps["tools"]["search_academic"]
    profiles = academic["provider_capability_profiles"]

    assert academic["profile_schema"] == "knowledgeradar-academic-provider-profiles/v1"
    assert "pubscholar" in academic["providers"]
    assert profiles["pubscholar"]["content"]["direct_read_preferred"] is True
    assert profiles["vip_oa"]["access"]["auth_mode"] == "managed_browser_login"
    assert "serpapi_scholar" not in profiles


def test_capabilities_declares_project_governance(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path / "runtime"))
    caps = build_capabilities(decision_log_path=str(tmp_path / "decision.jsonl"), provider_status=lambda: {})

    governance = caps["project_governance"]

    assert governance["schema"] == "knowledgeradar-project-governance/v1"
    assert governance["status"] == "implemented"
    assert governance["rules_entrypoint"] == "AGENTS.md"
    assert governance["status_dir"] == "project_status"
    assert "scripts/kr_project_state.py" == governance["scripts"]["project_state"]


def test_capabilities_summary_declares_manual_interaction(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path / "runtime"))

    from server import _capabilities_summary

    summary = _capabilities_summary()

    assert summary["summary"] is True
    assert summary["manual_interaction"]["schema"] == "knowledgeradar-manual-interaction/v1"
    assert summary["manual_interaction"]["entrypoints"]["request"].startswith("health_check")
    assert "empty_detail" in summary["manual_interaction"]["non_manual_states"]
    assert summary["research_quality_contract"]["schema"] == "knowledgeradar-research-quality-contract/v1"
