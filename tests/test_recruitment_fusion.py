import importlib.util
from pathlib import Path
import subprocess
import sys

from runtime.recruitment_fusion import fuse_recruitment_results
from runtime.recruitment_sources import recruitment_source_capability
from kr_core.collection import format_search_error, format_search_response

ROOT = Path(__file__).resolve().parents[1]
PROBE_SPEC = importlib.util.spec_from_file_location(
    "kr_recruitment_fusion_probe",
    ROOT / "scripts" / "kr_recruitment_fusion_probe.py",
)
assert PROBE_SPEC is not None
recruitment_fusion_probe = importlib.util.module_from_spec(PROBE_SPEC)
assert PROBE_SPEC.loader is not None
PROBE_SPEC.loader.exec_module(recruitment_fusion_probe)


def test_recruitment_fusion_deduplicates_cross_platform_candidates() -> None:
    result = fuse_recruitment_results(
        {
            "boss": [
                {
                    "title": "AI 产品经理",
                    "company": "Example AI",
                    "city": "杭州",
                    "salary": "20-30K",
                    "url": "https://example.com/job/1?utm_source=x",
                    "strategy": "stealth_cdp_page",
                    "published_at": "2026-06-18",
                }
            ],
            "liepin": [
                {
                    "title": "AI 产品经理",
                    "company": "Example AI",
                    "city": "杭州",
                    "salary": "20-30K",
                    "url": "https://example.com/job/1",
                    "strategy": "chrome_cdp_page",
                    "updated_at": "今天",
                }
            ],
        },
        limit=10,
    )

    assert result["total"] == 1
    item = result["items"][0]
    assert item["fusion"]["platform_provenance"] == ["boss", "liepin"]
    assert item["fusion"]["consistency"] == 0.72
    assert item["fusion_score"] > 0.7


def test_recruitment_fusion_downranks_web_search_fallback() -> None:
    result = fuse_recruitment_results(
        {
            "maimai": [
                {
                    "title": "大厂裁员讨论",
                    "url": "https://maimai.cn/article/1",
                    "snippet": "职场讨论，不是职位详情",
                    "source": "web_search_fallback",
                }
            ],
            "liepin": [
                {
                    "title": "算法工程师",
                    "company": "Example",
                    "city": "杭州",
                    "salary": "30-45K",
                    "url": "https://liepin.example/jobs/1",
                    "strategy": "chrome_cdp_page",
                    "published_at": "2026-06-18",
                }
            ],
        }
    )

    assert result["items"][0]["platform"] == "liepin"
    maimai = next(item for item in result["items"] if item["platform"] == "maimai")
    assert maimai["fusion"]["trust"] < result["items"][0]["fusion"]["trust"]


def test_recruitment_search_response_exposes_claim_contract() -> None:
    result = format_search_response(
        "猎聘",
        [{"title": "AI 产品经理", "company": "Example", "salary": "20-30K", "url": "https://liepin.example/job/1"}],
    )

    item = result["items"][0]
    assert item["evidence_strength"] == "medium_search_summary"
    assert item["market_claim_allowed"] is True
    assert item["salary_claim_allowed"] is False


def test_recruitment_search_response_exposes_address_contract() -> None:
    result = format_search_response(
        "智联招聘",
        [{"title": "AI 产品经理", "company": "Example", "area": "杭州·西湖·翠苑", "url": "https://zhilian.example/job/1"}],
    )

    item = result["items"][0]
    assert item["city"] == "杭州"
    assert item["district"] == "西湖区"
    assert item["street_or_area"] == "翠苑"
    assert item["address_precision"] == "street"
    assert item["district_claim_allowed"] is True


def test_recruitment_source_capability_declares_address_boundaries() -> None:
    boss = recruitment_source_capability("BOSS直聘").to_dict()
    v2ex = recruitment_source_capability("V2EX").to_dict()

    boss_address = boss["metadata"]["address_capability"]
    assert boss_address["default_address_source"] == "list_json"
    assert "areaDistrict" in boss_address["structured_location_fields"]
    assert boss_address["supports_district_claim"] is True
    assert boss_address["detail_enrichment_cost"] == "medium_high"

    v2ex_address = v2ex["metadata"]["address_capability"]
    assert v2ex_address["default_address_source"] == "community_topic_text"
    assert v2ex_address["supports_district_claim"] is False


def test_recruitment_search_response_preserves_strong_network_evidence() -> None:
    result = format_search_response(
        "BOSS直聘",
        [
            {
                "title": "AI 产品经理",
                "company": "Example",
                "url": "https://zhipin.example/job/1",
                "source": "boss_network_search",
                "evidence_strength": "strong_platform_network",
                "market_claim_allowed": True,
                "salary_claim_allowed": False,
            }
        ],
    )

    item = result["items"][0]
    assert item["evidence_strength"] == "strong_platform_network"
    assert item["market_claim_allowed"] is True
    assert item["salary_claim_allowed"] is False
    assert item["district_claim_allowed"] is False


def test_recruitment_web_fallback_is_weak_no_claim_evidence() -> None:
    result = format_search_response(
        "V2EX",
        [
            {
                "title": "酷工作帖子",
                "snippet": "开放网页索引命中，不一定是职位详情",
                "url": "https://v2ex.com/t/1",
                "source": "web_search_fallback",
            }
        ],
    )

    item = result["items"][0]
    assert item["evidence_strength"] == "weak_open_index"
    assert item["market_claim_allowed"] is False
    assert item["salary_claim_allowed"] is False


def test_v2ex_api_items_are_community_signal_not_market_claims() -> None:
    result = format_search_response(
        "V2EX",
        [
            {
                "title": "北京 Python 开发",
                "desc": "社区酷工作帖子",
                "url": "https://v2ex.com/t/1",
                "source": "http_api",
            }
        ],
    )

    item = result["items"][0]
    assert item["evidence_strength"] == "community_job_post"
    assert item["market_claim_allowed"] is False
    assert item["salary_claim_allowed"] is False


def test_recruitment_error_blocks_market_claims() -> None:
    result = format_search_error(
        "BOSS直聘",
        {
            "error": "需要登录",
            "failure_type": "login_required",
            "manual_action_required": True,
        },
    )

    assert result["failure_class"] == "platform_boundary_or_manual_lifecycle"
    assert result["evidence_strength"] == "blocked_no_claim"
    assert result["market_claim_allowed"] is False
    assert result["salary_claim_allowed"] is False


def test_recruitment_source_capability_registry_defines_platform_boundaries() -> None:
    boss = recruitment_source_capability("BOSS直聘")
    v2ex = recruitment_source_capability("V2EX")
    maimai = recruitment_source_capability("脉脉")

    assert boss.source_type == "structured_job_platform"
    assert list(boss.native_outputs) == ["job_card", "job_detail"]
    assert boss.claim_policy.market_claim_allowed is True
    assert boss.empty_semantics == "empty_results_requires_failure_classification"
    assert v2ex.source_type == "community_job_board"
    assert list(v2ex.native_outputs) == ["community_topic"]
    assert v2ex.claim_policy.market_claim_allowed is False
    assert maimai.source_type == "open_web_signal"
    assert list(maimai.native_outputs) == ["open_web_candidate"]


def test_recruitment_fusion_records_source_aware_empty_sources() -> None:
    result = fuse_recruitment_results({"boss": [], "maimai": [], "v2ex": []})

    assert result["items"] == []
    assert {source["platform"]: source["reason"] for source in result["degraded_sources"]} == {
        "boss": "empty_results_requires_failure_classification"
    }
    boundary_reasons = {source["platform"]: source["reason"] for source in result["source_boundaries"]}
    boundary_types = {source["platform"]: source["source_type"] for source in result["source_boundaries"]}
    assert boundary_reasons == {
        "maimai": "valid_no_match_for_source_type",
        "v2ex": "valid_no_match_for_source_type",
    }
    assert boundary_types == {
        "maimai": "open_web_signal",
        "v2ex": "community_job_board",
    }


def test_recruitment_fusion_marks_non_structured_sources_as_weak_signals() -> None:
    result = fuse_recruitment_results(
        {
            "v2ex": [
                {
                    "title": "远程 AI 应用开发",
                    "author": "example",
                    "url": "https://v2ex.com/t/100",
                    "source": "http_api",
                    "market_claim_allowed": True,
                    "salary_claim_allowed": True,
                }
            ]
        }
    )

    item = result["items"][0]
    assert item["source_type"] == "community_job_board"
    assert item["native_outputs"] == ["community_topic"]
    assert item["market_claim_allowed"] is False
    assert item["salary_claim_allowed"] is False


def test_recruitment_fusion_probe_runs_fixture() -> None:
    fixture = Path(__file__).parent / "fixtures" / "recruitment_fusion_sample.json"

    proc = subprocess.run(
        [
            sys.executable,
            "scripts/kr_recruitment_fusion_probe.py",
            "--input",
            str(fixture),
            "--limit",
            "5",
            "--json",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    import json

    stdout = proc.stdout.decode("utf-8", errors="replace")
    result = json.loads(stdout)
    assert result["status"] == "PASS"
    assert result["candidate_count"] == 3
    assert result["fusion"]["total"] == 2
    assert result["fusion"]["items"][0]["fusion_score"] > result["fusion"]["items"][1]["fusion_score"]


def test_recruitment_live_probe_requires_env_before_external_search(monkeypatch) -> None:
    monkeypatch.delenv("KR_RECRUITMENT_LIVE_PROBE", raising=False)
    calls: list[dict[str, object]] = []

    def fail_if_called(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        raise AssertionError("external search must stay disabled without the live env flag")

    result = recruitment_fusion_probe.run_live_probe(
        keyword="AI 产品经理",
        city="杭州",
        platforms=["boss"],
        per_platform_limit=1,
        limit=3,
        search_func=fail_if_called,
    )

    assert calls == []
    assert result["status"] == "SKIPPED"
    assert result["probe_events"][0]["reason"].startswith("set KR_RECRUITMENT_LIVE_PROBE=1")
    assert result["fusion"]["degraded_sources"][0]["platform"] == "boss"
    assert result["fusion"]["degraded_sources"][0]["reason"] == "empty_results_requires_failure_classification"


def test_recruitment_live_probe_default_covers_all_platforms(monkeypatch) -> None:
    monkeypatch.delenv("KR_RECRUITMENT_LIVE_PROBE", raising=False)

    result = recruitment_fusion_probe.run_live_probe(
        keyword="AI 产品经理",
        city="杭州",
        platforms=[],
        per_platform_limit=1,
        limit=3,
    )

    assert result["status"] == "SKIPPED"
    assert result["probe_events"][0]["platforms"] == ["boss", "liepin", "maimai", "zhilian", "v2ex"]
    assert [item["platform"] for item in result["fusion"]["degraded_sources"]] == ["boss", "liepin", "zhilian"]
    assert [item["platform"] for item in result["fusion"]["source_boundaries"]] == ["maimai", "v2ex"]


def test_recruitment_live_probe_feeds_injected_provider_results_into_fusion(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KR_RECRUITMENT_LIVE_PROBE", "1")
    monkeypatch.setenv("KR_TASK_DB_PATH", str(tmp_path / "gate.sqlite3"))
    calls: list[dict[str, object]] = []

    def fake_search(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {
            "items": [
                {
                    "title": "AI 产品经理",
                    "company": "Example AI",
                    "city": "杭州",
                    "salary": "20-30K",
                    "url": "https://example.com/job/1",
                    "strategy": "test_adapter",
                    "published_at": "2026-06-18",
                }
            ]
        }

    result = recruitment_fusion_probe.run_live_probe(
        keyword="AI 产品经理",
        city="杭州",
        platforms=["boss", "liepin"],
        per_platform_limit=1,
        limit=5,
        live_allowed=True,
        search_func=fake_search,
    )

    assert [call["platform"] for call in calls] == ["boss", "liepin"]
    assert result["status"] == "PASS"
    assert result["candidate_count"] == 2
    assert result["fusion"]["total"] == 1
    assert result["fusion"]["items"][0]["fusion"]["platform_provenance"] == ["boss", "liepin"]


def test_recruitment_live_probe_passes_scope_to_gate_and_records(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KR_RECRUITMENT_LIVE_PROBE", "1")
    monkeypatch.setenv("KR_TASK_DB_PATH", str(tmp_path / "gate.sqlite3"))
    from runtime import recruitment_governance

    gates = []
    records = []
    original_check = recruitment_governance.check_search_gate
    original_record = recruitment_governance.record_search_outcome

    def wrapped_check(platform: str, **kwargs: object) -> dict[str, object]:
        gates.append((platform, kwargs))
        return original_check(platform, **kwargs)

    def wrapped_record(platform: str, outcome: str, reason: str = "", **kwargs: object) -> None:
        records.append((platform, outcome, reason, kwargs))
        original_record(platform, outcome, reason, **kwargs)

    monkeypatch.setattr(recruitment_governance, "check_search_gate", wrapped_check)
    monkeypatch.setattr(recruitment_governance, "record_search_outcome", wrapped_record)

    result = recruitment_fusion_probe.run_live_probe(
        keyword="AI 产品经理",
        city="杭州",
        platforms=["liepin"],
        per_platform_limit=1,
        limit=1,
        live_allowed=True,
        record_gate=True,
        search_func=lambda **kwargs: {"items": []},
    )

    assert result["status"] == "EXPECTED_DEGRADED"
    assert gates == [("liepin", {"keyword": "AI 产品经理", "city": "杭州"})]
    assert records == [
        (
            "liepin",
            "degraded",
            "empty_results_requires_failure_classification",
            {"keyword": "AI 产品经理", "city": "杭州"},
        )
    ]


def test_recruitment_live_probe_treats_v2ex_empty_as_source_boundary(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KR_RECRUITMENT_LIVE_PROBE", "1")
    monkeypatch.setenv("KR_TASK_DB_PATH", str(tmp_path / "gate.sqlite3"))
    from runtime import recruitment_governance

    records = []
    original_record = recruitment_governance.record_search_outcome

    def wrapped_record(platform: str, outcome: str, reason: str = "", **kwargs: object) -> None:
        records.append((platform, outcome, reason, kwargs))
        original_record(platform, outcome, reason, **kwargs)

    monkeypatch.setattr(recruitment_governance, "record_search_outcome", wrapped_record)

    result = recruitment_fusion_probe.run_live_probe(
        keyword="AI 产品经理",
        city="杭州",
        platforms=["v2ex"],
        per_platform_limit=1,
        limit=1,
        live_allowed=True,
        record_gate=True,
        search_func=lambda **kwargs: {"items": [], "error": {"type": "empty_results"}},
    )

    assert result["status"] == "PASS"
    event = result["probe_events"][0]
    assert event["reason"] == "valid_no_match_for_source_type"
    assert event["source_type"] == "community_job_board"
    assert result["fusion"]["degraded_sources"] == []
    assert result["fusion"]["source_boundaries"][0]["reason"] == "valid_no_match_for_source_type"
    assert records == [
        ("v2ex", "ok", "valid_no_match_for_source_type", {"keyword": "AI 产品经理", "city": "杭州"})
    ]


def test_recruitment_probe_extracts_structured_error_type() -> None:
    items, reason = recruitment_fusion_probe._extract_provider_items(
        {
            "items": [],
            "error": {
                "type": "city_mismatch",
                "error": "猎聘搜索无结果",
            },
        }
    )

    assert items == []
    assert reason == "city_mismatch"


def test_recruitment_probe_overrides_degraded_source_reason_from_events() -> None:
    result = recruitment_fusion_probe._build_fusion_payload(
        {"liepin": []},
        limit=1,
        input_label="live",
        events=[{"platform": "liepin", "status": "EXPECTED_DEGRADED", "reason": "city_mismatch"}],
    )

    assert result["fusion"]["degraded_sources"][0]["platform"] == "liepin"
    assert result["fusion"]["degraded_sources"][0]["reason"] == "city_mismatch"
