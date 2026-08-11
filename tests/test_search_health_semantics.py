from pathlib import Path

from runtime.health_checks import HealthCheckDeps, HealthCheckService


def _deps(provider_status):
    noop_dict = lambda: {}
    return HealthCheckDeps(
        bili_headers={},
        xhs_bridge_path="",
        project_root=str(Path.cwd()),
        runtime_log_dir=str(Path.cwd()),
        mcp_server_log_path=str(Path.cwd() / "mcp.log"),
        decision_log_path=str(Path.cwd() / "decision.jsonl"),
        decision_logger=type("DecisionLogger", (), {"health": lambda self: {"status": "ok"}})(),
        provider_status=provider_status,
        academic_provider_status=noop_dict,
        chrome_debug_port=lambda platform: "",
        chrome_debug_url=lambda platform: "",
        chrome_runtime_summary=noop_dict,
        ensure_chrome_debugging=lambda platform: False,
        finish_chrome_automation=lambda platform, reason: None,
        find_chrome_exe=lambda: None,
        cleanup_chrome_platform=lambda platform: None,
        read_zhihu_cookies_from_cdp=lambda: None,
        read_zhihu_cookies_from_profile=lambda: None,
        zhihu_sign=lambda cookie, path: {},
        zhihu_search_api=lambda keyword, cookie, limit: [],
        inspect_zhihu_cookie_health=lambda warn_within_hours: {},
        inspect_xhs_login_health=lambda cdp_url, warn_within_hours: {},
        probe_xhs_page_state=lambda cdp_url: {},
        bring_chrome_to_front=lambda platform: {},
        background_chrome=lambda platform: {},
        legacy_search_bilibili=lambda *args, **kwargs: {},
        legacy_search_zhihu=lambda *args, **kwargs: {},
        legacy_search_xiaohongshu=lambda *args, **kwargs: {},
        youtube_configured=lambda: False,
        platform_capabilities=noop_dict,
        task_queue_summary=noop_dict,
        usage_summary=noop_dict,
        monitor_summary=noop_dict,
        degradation_summary=noop_dict,
        xhs_detail_health_summary=noop_dict,
        xhs_chain_health_summary=noop_dict,
        evidence_store_health=noop_dict,
        academic_evidence_summary=noop_dict,
        search_cache_summary=noop_dict,
        gh_cli_sidecar_health=noop_dict,
    )


def test_web_search_provider_quota_exhausted_is_expected_degraded():
    service = HealthCheckService(
        _deps(
            lambda: {
                "tavily": {"configured": True, "available": False, "status": "daily_exhausted"},
                "brave": {"configured": False, "available": False, "degraded_ok": True},
                "exa": {"configured": False, "available": False, "degraded_ok": True},
                "_quota": {"tavily": {"status": "daily_exhausted", "remaining_today": 0}},
                "_host_search_cards": {"cards": [{"platform": "codex", "state": "absent"}]},
            }
        )
    )

    result = service._check_web_search_provider()

    assert result["status_class"] == "EXPECTED_DEGRADED"
    assert result["degraded_reason"] == "quota_exhausted"
    assert result["quota"]["tavily"]["status"] == "daily_exhausted"


def test_web_search_provider_available_free_backend_keeps_optional_failures_non_blocking():
    service = HealthCheckService(
        _deps(
            lambda: {
                "anysearch": {"configured": True, "available": True},
                "tavily": {"configured": True, "available": False, "status": "daily_exhausted"},
                "brave": {"configured": False, "available": False, "degraded_ok": True},
                "_quota": {"tavily": {"status": "daily_exhausted", "remaining_today": 0}},
            }
        )
    )

    result = service._check_web_search_provider()

    assert result["status_class"] == "PASS"
    assert "tavily" in result["optional_degraded_providers"]
