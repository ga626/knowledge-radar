from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import server
from scripts.compare_research_budget_profiles import compare
from scripts.kr_mcp_payload_probe import run_probe


def test_payload_probe_supports_cold_mode(monkeypatch) -> None:
    monkeypatch.setattr(server, "provider_status", lambda: (_ for _ in ()).throw(AssertionError("cold summary should defer provider_status")))
    monkeypatch.setattr(server, "academic_provider_status", lambda: (_ for _ in ()).throw(AssertionError("cold summary should defer academic status")))

    result = run_probe(cold=True)

    assert result["mode"] == "cold"
    assert result["status"] == "PASS", result
    assert all(check["elapsed_s"] <= check["max_elapsed_s"] for check in result["checks"])


def test_health_summary_default_defers_live_provider_probe(monkeypatch) -> None:
    monkeypatch.setenv("KR_HEALTH_SUMMARY_COLD_STATIC", "true")
    monkeypatch.setattr(server, "provider_status", lambda: (_ for _ in ()).throw(AssertionError("provider_status should be deferred")))
    monkeypatch.setattr(server, "academic_provider_status", lambda: (_ for _ in ()).throw(AssertionError("academic status should be deferred")))
    monkeypatch.setattr(server.gh_cli_sidecar, "health", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("github health should be deferred")))

    result = server._health_check_agent_summary_uncached()

    assert result["checks"]["summary_probe_policy"]["mode"] == "deferred_static_summary"
    assert result["checks"]["web_search"]["status"] == "deferred"


def test_fast_deep_profile_comparison_declares_cost_and_depth_delta() -> None:
    result = compare("同一个问题")

    assert result["status"] == "PASS"
    assert [row["profile"] for row in result["profiles"]] == ["fast", "deep"]
    assert result["delta"]["tool_call_count"] > 0
    assert result["delta"]["evidence_surface_count"] > 0
    assert result["delta"]["max_wall_time_s"] > 0
