from __future__ import annotations

from pathlib import Path

import httpx

from capability_manifest import manifest_summary
from capabilities import ACTUAL_MCP_TOOLS, build_capabilities, build_tool_surface, research_quality_contract_manifest, source_ecology_manifest, validation_semantics_manifest
from kr_core.search_cache import SearchCache
from mcp_tools import facade_manifest
from runtime.admission import EXPECTED_DEGRADED, NEEDS_INTERACTION, PASS, RETRY_LATER, classify_admission_state
from runtime.failure_cache import FailureStateCache
from runtime.tasks import TaskStore
from search_providers.baseline import compare_snapshots, snapshot_from_response
from search_providers.http_client import close_shared_clients, request_json, shared_client_summary
from search_providers.status_cache import ProviderStatusCache


def test_search_cache_key_includes_request_semantics() -> None:
    cache = SearchCache()

    base = cache.key(platform="web", query="  Foo   Bar ", limit=5, provider="auto")
    same = cache.key(platform="web", query="foo bar", limit=5, provider="auto")
    raw = cache.key(platform="web", query="foo bar", limit=5, provider="auto", include_raw_content=True)
    fresh = cache.key(platform="web", query="foo bar", limit=5, provider="auto", freshness="week")
    options = cache.key(platform="web", query="foo bar", limit=5, provider="auto", options={"domains": ["example.com"]})

    assert base == same
    assert len({base, raw, fresh, options}) == 4


def test_provider_status_cache_uses_short_ttl() -> None:
    calls = {"count": 0}

    def load():
        calls["count"] += 1
        return {"anysearch": {"available": True, "call": calls["count"]}}

    cache = ProviderStatusCache(ttl_s=60)
    first = cache.get(load)
    second = cache.get(load)
    forced = cache.get(load, force_refresh=True)

    assert first["anysearch"]["call"] == 1
    assert second["anysearch"]["call"] == 1
    assert forced["anysearch"]["call"] == 2
    assert cache.summary()["metrics"]["hits"] == 1


def test_shared_http_client_reuses_pool_with_mock_transport(monkeypatch) -> None:
    class FakeClient:
        created = 0

        def __init__(self, *args, **kwargs):
            FakeClient.created += 1
            self.closed = False

        def request(self, method: str, url: str, **kwargs):
            request = httpx.Request(method, url)
            return httpx.Response(200, json={"ok": True, "path": request.url.path}, request=request)

        def close(self):
            self.closed = True

    monkeypatch.setattr(httpx, "Client", FakeClient)
    close_shared_clients()

    assert request_json("GET", "https://example.test/a", timeout=1.0) == {"ok": True, "path": "/a"}
    assert request_json("GET", "https://example.test/b", timeout=1.0) == {"ok": True, "path": "/b"}
    assert shared_client_summary()["client_count"] == 1
    assert FakeClient.created == 1
    close_shared_clients()


def test_admission_state_classification() -> None:
    assert classify_admission_state({})["status_class"] == PASS
    assert classify_admission_state({"failure_type": "empty_detail", "error": "empty"})["status_class"] == EXPECTED_DEGRADED
    assert classify_admission_state({"failure_type": "network_timeout", "error": "timeout"})["status_class"] == RETRY_LATER
    assert classify_admission_state({"failure_type": "login_required", "manual_action_required": True})["status_class"] == NEEDS_INTERACTION


def test_failure_state_cache_marks_hits() -> None:
    cache = FailureStateCache(ttl_s=60)
    key = cache.key(platform="小红书", url="https://www.xiaohongshu.com/explore/abc")
    cache.set(key, {"platform": "小红书", "error": "empty", "failure_type": "empty_detail"})

    cached = cache.get(key)

    assert cached is not None
    assert cached["metadata"]["failure_cache"]["hit"] is True
    assert cache.summary()["metrics"]["hits"] == 1


def test_search_quality_snapshot_and_comparison() -> None:
    before = snapshot_from_response(
        "q",
        {
            "items": [
                {
                    "url": "https://example.com/a",
                    "source_ecology": "generic_web_ecology",
                    "evidence_strength": "depends_on_source_authority_and_extracted_content",
                }
            ],
            "attempted_providers": ["anysearch"],
        },
        elapsed_ms=100,
    )
    after = snapshot_from_response(
        "q",
        {
            "items": [
                {"url": "https://example.com/a", "source_ecology": "generic_web_ecology"},
                {"url": "https://docs.example.org/b", "source_ecology": "generic_web_ecology"},
            ],
            "attempted_providers": ["anysearch", "tavily"],
        },
        elapsed_ms=80,
    )

    comparison = compare_snapshots(before, after)

    assert comparison["deltas"]["total"] == 1
    assert comparison["deltas"]["unique_domains"] == 1
    assert comparison["deltas"]["elapsed_ms"] == -20


def test_task_store_enables_wal_policy(tmp_path: Path) -> None:
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    store.upsert_task("t1", "demo", "web")

    summary = store.summary()

    assert summary["sqlite_policy"]["wal_requested"] is True
    assert summary["sqlite_policy"]["journal_mode"] in {"wal", "memory", "off", "delete", "unavailable"}


def test_mcp_facade_and_capability_manifest_are_exposed() -> None:
    facade = facade_manifest(ACTUAL_MCP_TOOLS)
    manifest = manifest_summary(
        tool_surface=build_tool_surface(),
        source_ecologies=source_ecology_manifest(),
        validation_semantics=validation_semantics_manifest({}, {}),
        research_quality=research_quality_contract_manifest(),
    )

    assert facade["schema"] == "knowledgeradar-mcp-facade/v1"
    assert facade["actual_tool_count"] == len(ACTUAL_MCP_TOOLS)
    assert manifest["sections"]["tool_surface"]["tool_count"] == len(ACTUAL_MCP_TOOLS)


def test_build_capabilities_includes_facade_and_manifest() -> None:
    caps = build_capabilities(decision_log_path="", provider_status=lambda: {})

    assert caps["mcp_facade"]["schema"] == "knowledgeradar-mcp-facade/v1"
    assert caps["capability_manifest"]["schema"] == "knowledgeradar-capability-manifest/v1"
