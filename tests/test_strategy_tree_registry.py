from __future__ import annotations

from pathlib import Path

from capabilities import ACTUAL_MCP_TOOLS, build_capabilities
from search_providers.concurrency import compute_wave_concurrency
from strategy_trees.registry import (
    build_strategy_tree_bundle,
    cache_registry_manifest,
    governance_registry_manifest,
    strategy_tree_summary,
    validate_strategy_tree_bundle,
)


ROOT = Path(__file__).resolve().parents[1]


def test_strategy_tree_registry_validates_against_repo_paths() -> None:
    report = validate_strategy_tree_bundle(actual_tools=ACTUAL_MCP_TOOLS, repo_root=ROOT)

    assert report["status"] == "PASS"
    assert report["tree_count"] >= 10


def test_strategy_tree_registry_validates_module_targets() -> None:
    bundle = build_strategy_tree_bundle(include_nodes=True)
    bundle["trees"][0]["nodes"][0]["module"] += "__typo_probe"

    report = validate_strategy_tree_bundle(bundle, actual_tools=ACTUAL_MCP_TOOLS, repo_root=ROOT)

    assert report["status"] == "FAIL"
    assert any(error["type"] == "module_target_unresolved" for error in report["errors"])


def test_strategy_tree_registry_covers_required_platform_surfaces() -> None:
    bundle = build_strategy_tree_bundle(include_nodes=True)
    trees = {tree["tree_id"]: tree for tree in bundle["trees"]}

    required = {
        "web_search.provider_wave",
        "github.search_sidecar",
        "xiaohongshu.risk_managed_browser",
        "zhihu.cookie_governed_fallback",
        "bilibili.search_detail_transcript_media",
        "youtube.quota_and_media",
        "academic.metadata_then_fulltext",
        "recruitment.platform_specific",
        "detail.dispatch_and_unsupported",
        "generic_web.extraction",
        "multimodal.media_router",
    }
    assert required.issubset(trees)
    assert any(node["node_id"] == "maimai_retired" for node in trees["recruitment.platform_specific"]["nodes"])
    assert any(node["node_id"] == "watch_url_boundary" for node in trees["youtube.quota_and_media"]["nodes"])


def test_strategy_tree_summary_and_governance_are_compact() -> None:
    summary = strategy_tree_summary()
    governance = governance_registry_manifest()
    cache = cache_registry_manifest()

    assert summary["validation_status"] == "PASS"
    assert "nodes" not in summary["trees"][0]
    assert "youtube.quota" in governance["entries"]
    assert "bilibili.transcript_cache" in cache["entries"]


def test_capabilities_expose_strategy_registry_without_replacing_legacy_trees(tmp_path) -> None:
    caps = build_capabilities(decision_log_path=str(tmp_path / "decision.jsonl"), provider_status=lambda: {})

    assert caps["strategy_tree_consistency"]["status"] == "PASS"
    assert caps["strategy_tree_registry"]["schema"] == "knowledgeradar-strategy-tree-registry/v2"
    assert caps["strategy_trees"]["asr"]["status"] == "implemented_p2_2"
    assert caps["governance_registry"]["entries"]["github.sidecar_admission"]["mode"] == "shadow"


def test_wave_concurrency_limits_raw_content_requests() -> None:
    decision = compute_wave_concurrency(
        ["searxng", "anysearch", "brave"],
        provider_status={
            "searxng": {"available": True},
            "anysearch": {"available": True},
            "brave": {"available": True},
        },
        include_raw_content=True,
    )

    assert decision["selected_workers"] == 2
    assert "raw_content_request_limits_parallelism" in decision["reasons"]
