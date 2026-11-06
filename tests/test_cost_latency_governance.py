from __future__ import annotations

import json
from types import SimpleNamespace

import server
from collectors.platform import gh_cli_sidecar
import understanding.image as image_mod
from runtime.cost_latency import budget_manifest, cache_registry_summary, capability_cost_profiles, get_ttl_cache
from runtime.resource_concurrency import acquire_resource, resource_concurrency_summary
from runtime.tasks import TaskStore


def test_cost_latency_profiles_and_budget_manifest_are_complete() -> None:
    profiles = capability_cost_profiles()
    assert "github.search" in profiles
    assert profiles["xhs.detail.image_ocr"]["latency_class"] == "background_preferred"
    budget = budget_manifest()
    assert set(budget["modes"]) >= {"fast", "balanced", "deep", "diagnostic"}
    assert budget["modes"]["fast"]["max_sync_wait_s"] < budget["modes"]["deep"]["max_sync_wait_s"]


def test_ttl_cache_summary_tracks_hit_rate_and_saved_time() -> None:
    cache = get_ttl_cache("unit.cache.stats", ttl_s=60, max_items=4)
    cache.get("missing")
    cache.set("key", {"status": "ok"}, compute_elapsed_s=1.25)
    cached = cache.get("key")
    summary = cache_registry_summary()["caches"]["unit.cache.stats"]

    assert cached is not None
    assert cached["runtime"]["cache"]["hit"] is True
    assert summary["stats"]["hits"] >= 1
    assert summary["stats"]["misses"] >= 1
    assert summary["stats"]["sets"] >= 1
    assert summary["stats"]["estimated_saved_s"] >= 1.25


def test_health_summary_uses_cache_for_sidecar_health(monkeypatch) -> None:
    calls = {"gh": 0}
    monkeypatch.setenv("KR_HEALTH_SUMMARY_TTL_S", "60")
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
    monkeypatch.setattr(server, "chrome_runtime_quick_summary", lambda: {"status": "ok", "platforms": {}})

    def fake_health(*, stale_ok=False, force_refresh=False):
        calls["gh"] += 1
        return {"status": "degraded", "available": False, "failure_code": "LOGIN_REQUIRED", "runtime": {"cache": {"hit": False}}}

    monkeypatch.setattr(server.gh_cli_sidecar, "health", fake_health)

    first = server._health_check_agent_summary()
    second = server._health_check_agent_summary()

    assert first["schema_version"] == "knowledgeradar-health-agent-summary/v1"
    assert second["runtime"]["cache"]["hit"] is True
    assert calls["gh"] == 1


def test_github_search_cache_and_rest_metadata(monkeypatch) -> None:
    from collectors.platform.github_api import GitHubClient
    from runtime.cost_latency import TTLCache
    calls = []
    class PublicClient:
        transport = 'rest'
        identity = 'cache-regression-public'
        attempts = []
        def json(self, endpoint, *, params):
            calls.append((endpoint, params))
            return {'items':[{'id':1, 'full_name':'owner/project', 'private':False,
                'description':'Public implementation', 'stargazers_count':100}],
                'total_count':1, 'incomplete_results':False}, {}
    monkeypatch.setattr(GitHubClient, 'configured', lambda **kwargs: PublicClient())
    monkeypatch.setattr(gh_cli_sidecar, '_SEARCH_CACHE', TTLCache('github-cache-regression', ttl_s=300))
    first = gh_cli_sidecar.search_repositories('repo:owner/project', limit=1)
    second = gh_cli_sidecar.search_repositories('repo:owner/project', limit=1)
    assert first['fallback_used'] is False
    assert first['metadata']['attempted_queries_count'] == 1
    assert second['runtime']['cache']['hit'] is True
    assert second['metadata']['network_reverified'] is False
    assert len(calls) == 1


def test_xhs_ocr_artifact_cache(monkeypatch, tmp_path) -> None:
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    monkeypatch.setattr(image_mod, "get_task_store", lambda: store)
    calls = {"model": 0}

    class _Response:
        content = b"image-bytes"

        def raise_for_status(self) -> None:
            return None

    def fake_model(**_kwargs):
        calls["model"] += 1
        return ('{"text":"图中文字","items":[],"visual_summary":"摘要"}', "test-model")

    monkeypatch.setattr(image_mod, "guarded_fetch", lambda *args, **kwargs: _Response())
    monkeypatch.setattr(image_mod, "image_bytes_to_base64", lambda data: "base64")
    monkeypatch.setattr(image_mod, "call_multimodal_models", fake_model)

    first = image_mod.ocr_first_xhs_image(["https://sns-webpic-qc.xhscdn.com/content-one.jpg"])
    second = image_mod.ocr_first_xhs_image(["https://sns-webpic-qc.xhscdn.com/content-one.jpg"])

    assert first["status"] == "ok"
    assert second["runtime"]["cache"]["hit"] is True
    assert calls["model"] == 1


def test_xhs_ocr_reuses_existing_content_task_for_url_variants(monkeypatch, tmp_path) -> None:
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    monkeypatch.setattr(image_mod, "get_task_store", lambda: store)
    calls = {"model": 0}

    class _Response:
        content = b"image-bytes"

        def raise_for_status(self) -> None:
            return None

    def fake_model(**_kwargs):
        calls["model"] += 1
        return ('{"text":"图中文字","items":[],"visual_summary":"摘要"}', "test-model")

    monkeypatch.setattr(image_mod, "guarded_fetch", lambda *args, **kwargs: _Response())
    monkeypatch.setattr(image_mod, "image_bytes_to_base64", lambda data: "base64")
    monkeypatch.setattr(image_mod, "call_multimodal_models", fake_model)

    first = image_mod.ocr_first_xhs_image(
        ["https://sns-webpic-qc.xhscdn.com/content-two.jpg?xsec_token=one"],
        task_metadata={"content_id": "note-ocr-reuse"},
    )
    second = image_mod.ocr_first_xhs_image(
        ["https://sns-webpic-qc.xhscdn.com/content-two.jpg?xsec_token=two"],
        task_metadata={"content_id": "note-ocr-reuse", "task_scope_id": "different-parent"},
    )

    assert first["status"] == "ok"
    assert second['runtime']['cache']['hit'] is True
    assert second['text'] == first['text'] == '图中文字'
    assert store.get_task(second['task_id']) is not None
    assert second['ocr_signal_strength'] != 'result_external'
    assert calls["model"] == 1


def test_xhs_ocr_empty_model_result_has_signal_strength(monkeypatch, tmp_path) -> None:
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    monkeypatch.setattr(image_mod, "get_task_store", lambda: store)

    class _Response:
        content = b"image-bytes"

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(image_mod, "guarded_fetch", lambda *args, **kwargs: _Response())
    monkeypatch.setattr(image_mod, "image_bytes_to_base64", lambda data: "base64")
    monkeypatch.setattr(image_mod, "call_multimodal_models", lambda **kwargs: ('{"text":"","items":[],"visual_summary":""}', "test-model"))

    result = image_mod.ocr_first_xhs_image(["https://sns-webpic-qc.xhscdn.com/empty-signal.jpg"])

    assert result["status"] == "ok"
    assert result["text"] == ""
    assert result["items"] == []
    assert result["ocr_signal_strength"] == "none_or_weak"
    assert result["ocr_empty_reason"] == "no_text_or_visual_summary_extracted"


def test_resource_concurrency_summary_includes_runtime_stats() -> None:
    with acquire_resource("frame_vision") as lease:
        assert lease["limited"] is True
        assert "wait_s" in lease
    summary = resource_concurrency_summary()
    assert "runtime_stats" in summary
    assert summary["runtime_stats"]["frame_vision"]["acquired"] >= 1
