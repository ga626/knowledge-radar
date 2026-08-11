from __future__ import annotations

import server


def test_health_summary_is_agent_facing_and_compact(monkeypatch) -> None:
    monkeypatch.setattr(
        server,
        "provider_status",
        lambda: {
            "anysearch": {"available": True, "configured": True, "status": "ok"},
            "_quota": {"tavily": {"remaining": 0}},
        },
    )
    monkeypatch.setattr(server, "academic_provider_status", lambda: {"openalex": {"available": True, "status": "ok"}})
    monkeypatch.setattr(server.get_task_store(), "summary", lambda recent_limit=3: {"status": "ok", "active": 0, "stale_count": 0, "counts": {}})
    monkeypatch.setattr(server.gh_cli_sidecar, "health", lambda: {"status": "degraded", "available": False, "failure_code": "LOGIN_REQUIRED"})
    monkeypatch.setattr(server, "chrome_runtime_summary", lambda: (_ for _ in ()).throw(AssertionError("summary should not run full Chrome probes")))
    monkeypatch.setattr(server, "chrome_runtime_quick_summary", lambda: {"status": "ok", "platforms": {}})

    result = server.health_check(mode="summary")

    assert result["schema_version"] == "knowledgeradar-health-agent-summary/v1"
    assert result["diagnostic_mode"] == "health_check(mode='diagnostic_summary')"
    assert "xiaohongshu_route_matrix" not in result["checks"]
    assert "native_readonly_runner" not in result["checks"]
    assert result["checks"]["web_search"]["status"] == "deferred"
    assert result["checks"]["summary_probe_policy"]["mode"] == "deferred_static_summary"


def test_health_summary_live_mode_reports_available_providers(monkeypatch) -> None:
    monkeypatch.setenv("KR_HEALTH_SUMMARY_COLD_STATIC", "false")
    monkeypatch.setattr(
        server,
        "provider_status",
        lambda: {
            "anysearch": {"available": True, "configured": True, "status": "ok"},
            "_quota": {"tavily": {"remaining": 0}},
        },
    )
    monkeypatch.setattr(server, "academic_provider_status", lambda: {"openalex": {"available": True, "status": "ok"}})
    monkeypatch.setattr(server.get_task_store(), "summary", lambda recent_limit=3: {"status": "ok", "active": 0, "stale_count": 0, "counts": {}})
    monkeypatch.setattr(server.gh_cli_sidecar, "health", lambda **_kwargs: {"status": "degraded", "available": False, "failure_code": "LOGIN_REQUIRED"})
    monkeypatch.setattr(server, "chrome_runtime_quick_summary", lambda: {"status": "ok", "platforms": {}})

    result = server._health_check_agent_summary_uncached()

    assert result["checks"]["summary_probe_policy"]["mode"] == "live_summary"
    assert result["checks"]["web_search"]["available_providers"] == ["anysearch"]
