from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "research_quality"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.check_research_quality import validate_research_quality, write_quality_receipt
from scripts.build_research_evidence_sidecar import build_sidecar, normalize_sidecar_for_report
from scripts.build_codex_thread_trace import build_codex_thread_trace, parse_codex_jsonl_trace
from scripts.verify_research_repair_loop import run_verification


def _validate(name: str) -> dict:
    return validate_research_quality(FIXTURES / "minimal_report.md", FIXTURES / name)


def test_quality_receipt_binds_the_exact_artifacts(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    evidence = tmp_path / "evidence.json"
    output = tmp_path / "quality.receipt.json"
    report.write_text("# report", encoding="utf-8")
    evidence.write_text("{}", encoding="utf-8")

    receipt = write_quality_receipt(
        result={"schema": "knowledgeradar-research-quality-check/v1", "status": "pass", "checked_at": "2026-08-09T00:00:00+00:00"},
        report_path=report,
        evidence_path=evidence,
        output_path=output,
    )

    assert receipt["status"] == "pass"
    assert receipt["report_path"] == str(report.resolve())
    assert output.is_file()


def test_research_quality_minimal_pass_fixture() -> None:
    result = _validate("minimal_pass.evidence.json")

    assert result["status"] == "pass", result
    assert result["hard_findings"] == []
    assert result["repair_requests"] == []
    assert "local_code" in result["summary"]["covered_surfaces"]
    assert "prior_report" in result["summary"]["covered_surfaces"]


def test_research_quality_missing_code_requests_source_archaeology() -> None:
    result = _validate("missing_code.evidence.json")

    assert result["status"] == "needs_revision"
    assert any(item["code"] == "missing_required_evidence_surface" for item in result["hard_findings"])
    assert any(item["code"] == "source_archaeology_needed" for item in result["soft_findings"])
    repair = next(item for item in result["repair_requests"] if item["category"] == "evidence_coverage")
    assert repair["agent_autonomy"] == "agent_decides_tools"
    assert repair["acceptable_evidence_surfaces"] == ["local_code"]


def test_research_quality_missing_media_requests_modality_detail() -> None:
    result = _validate("missing_media.evidence.json")

    assert result["status"] == "needs_revision"
    assert any(item["code"] == "modality_detail_needed" for item in result["soft_findings"])
    repair = next(item for item in result["repair_requests"] if item["category"] == "modality_coverage")
    assert repair["acceptable_evidence_surfaces"] == ["media_detail"]
    assert repair["budget_impact"] == "moderate"


def test_research_quality_missing_source_ecology_requests_coverage(tmp_path: Path) -> None:
    evidence = tmp_path / "missing_ecology.evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "schema": "knowledgeradar-research-evidence/v1",
                "artifact": {"report_path": str(FIXTURES / "minimal_report.md"), "task_prompt_hash": "sha256:test"},
                "budget": {"selected_profile": "balanced", "remaining_repair_rounds": 1},
                "tool_calls": [],
                "evidence_items": [
                    {
                        "id": "E001",
                        "type": "web",
                        "title": "Generic source",
                        "url": "https://example.com",
                        "summary": "Generic web evidence.",
                        "strength": "weak",
                        "source_ecology": "generic_web_ecology",
                    }
                ],
                "claims": [
                    {
                        "id": "C001",
                        "text": "Public article ecology should be checked.",
                        "importance": "high",
                        "supporting_evidence_ids": ["E001"],
                    }
                ],
                "coverage": {
                    "required_surfaces": ["web"],
                    "covered_surfaces": ["web"],
                    "skipped_surfaces": [],
                    "required_source_ecologies": ["wechat_public_article_ecology"],
                    "covered_source_ecologies": ["generic_web_ecology"],
                    "skipped_source_ecologies": [],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = validate_research_quality(FIXTURES / "minimal_report.md", evidence)

    assert result["status"] == "needs_revision"
    assert any(item["code"] == "missing_required_source_ecology" for item in result["hard_findings"])
    repair = next(item for item in result["repair_requests"] if item["category"] == "source_ecology_coverage")
    assert "source_ecology" in repair["acceptable_evidence_surfaces"]
    assert result["summary"]["required_source_ecologies"] == ["wechat_public_article_ecology"]


def test_research_quality_cli_outputs_machine_readable_json() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "check_research_quality.py"),
            "--report",
            str(FIXTURES / "minimal_report.md"),
            "--evidence",
            str(FIXTURES / "minimal_pass.evidence.json"),
            "--json",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    payload = json.loads(proc.stdout)
    assert payload["schema"] == "knowledgeradar-research-quality-check/v1"
    assert payload["status"] == "pass"


def test_research_quality_missing_sidecar_requests_draft_generation(tmp_path: Path) -> None:
    result = validate_research_quality(FIXTURES / "minimal_report.md", tmp_path / "missing.evidence.json")

    assert result["status"] == "fail_hard"
    repair = next(item for item in result["repair_requests"] if item["category"] == "evidence_sidecar_draft")
    assert "build_research_evidence_sidecar.py" in repair["done_when"][0]


def test_build_research_evidence_sidecar_extracts_report_evidence(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    report.write_text(
        "参考 [源码](scripts/check_research_quality.py)、https://github.com/example/repo 和 https://mp.weixin.qq.com/s/example 形成判断。",
        encoding="utf-8",
    )

    payload = build_sidecar(report)

    assert payload["schema"] == "knowledgeradar-research-evidence/v1"
    assert payload["artifact"]["draft"] is True
    assert "upstream_code" in payload["coverage"]["covered_surfaces"]
    assert "local_code" in payload["coverage"]["covered_surfaces"]
    assert "github_repository_ecology" in payload["coverage"]["covered_source_ecologies"]
    assert "wechat_public_article_ecology" in payload["coverage"]["covered_source_ecologies"]


def test_build_research_evidence_sidecar_adds_report_placeholder_for_plain_text(tmp_path: Path) -> None:
    report = tmp_path / "plain.md"
    report.write_text("这是一份没有链接的报告。", encoding="utf-8")

    payload = build_sidecar(report)

    assert payload["evidence_items"][0]["type"] == "prior_report"
    assert payload["evidence_items"][0]["strength"] == "weak"


def test_normalize_sidecar_for_report_adds_quality_contract_defaults(tmp_path: Path) -> None:
    report = tmp_path / "governance.md"
    report.write_text(
        """# 项目治理报告

已执行 health_check(mode='summary') 和 get_capabilities(summary=true)，并参考 scripts/check_research_quality.py 形成判断。

```mermaid
flowchart TD
  A["P0 读取现状"] --> B["P1 改造机制"]
  B --> C["P2 验证闭环"]
  A --> C
```

## 详细执行步骤

### Step 1 / P0：读取现状

- 做什么：读取现有检查链路。
- 怎么做：检查本地脚本和 sidecar 字段。
- 输入：report-light、research-quality 和 docs index 脚本。
- 输出：现状判断。
- 验收：能定位自动准备缺口。
- 依赖：无。

### Step 2 / P1：改造机制

- 做什么：补全报告收尾机制。
- 怎么做：在检查前生成机器可读 sidecar 字段。
- 输入：报告正文和 evidence sidecar。
- 输出：可检查的 JSON。
- 验收：research quality 通过。
- 依赖：P0。

### Step 3 / P2：验证闭环

- 做什么：验证一次跑通。
- 怎么做：运行 report-light gate。
- 输入：报告和 sidecar。
- 输出：PASS。
- 验收：没有修复请求。
- 依赖：P1。
""",
        encoding="utf-8",
    )
    evidence = tmp_path / "governance.evidence.json"
    payload = normalize_sidecar_for_report({}, report, evidence=evidence, validation_profile="report_light")
    evidence.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = validate_research_quality(report, evidence)

    assert result["status"] == "pass", result
    assert payload["task_type"] == "report"
    assert payload["known_issues"]["schema"] == "knowledgeradar-known-issues/v1"
    assert payload["decision_register"]["schema"] == "knowledgeradar-decision-register/v1"
    assert payload["process_quality"]["schema"] == "knowledgeradar-process-quality/v1"


def test_repair_loop_fixture_can_be_repaired_by_standard_request(tmp_path: Path) -> None:
    result = run_verification(tmp_path)

    assert result["status"] == "PASS", result
    assert result["initial_status"] == "needs_revision"
    assert result["repaired_status"] == "pass"


def test_p8_report_requires_agentic_search_governance(tmp_path: Path) -> None:
    report = tmp_path / "p8.md"
    report.write_text(
        """# P8 模型自决搜索报告

## 规划图

```mermaid
flowchart TD
  P0["P0"] --> P1["P1"]
  P1 --> P2["P2"]
  P2 --> P3["P3"]
```

## 详细执行步骤

| 阶段 | 做什么 | 怎么做 | 输入 | 输出 | 验收 | 依赖 | 是否改代码 |
|---|---|---|---|---|---|---|---|
| P0 | inspect current evidence | read local files and sidecar metadata | x | y | pass | 无前置依赖 | 不改代码 |
| P1 | implement governance rule | patch project rules and checker fixtures | y | z | pass | P0 | 是 |
| P2 | verify report contract | run targeted quality validation | z | done | pass | P1 | 是 |
| P3 | publish findings | update report and sidecar | done | final | pass | P2 | 不改代码 |
""",
        encoding="utf-8",
    )
    payload = {
        "schema": "knowledgeradar-research-evidence/v1",
        "artifact": {"report_path": str(report), "title": "P8 模型自决搜索报告"},
        "budget": {"selected_profile": "deep", "remaining_repair_rounds": 1},
        "tool_calls": [{"id": "T001", "tool": "health_check"}],
        "evidence_items": [{"id": "E001", "type": "local_code", "summary": "code"}],
        "claims": [{"id": "C001", "importance": "high", "supporting_evidence_ids": ["E001"]}],
        "coverage": {"required_surfaces": ["local_code"], "covered_surfaces": ["local_code"]},
        "roadmap": [
            {"phase": "P0", "task": "inspect current evidence", "method": "read local files and sidecar metadata", "input": "x", "output": "y", "dependency": "none", "acceptance": "pass"},
            {"phase": "P1", "task": "implement governance rule", "method": "patch project rules and checker fixtures", "input": "y", "output": "z", "dependency": "P0", "acceptance": "pass"},
            {"phase": "P2", "task": "verify report contract", "method": "run targeted quality validation", "input": "z", "output": "done", "dependency": "P1", "acceptance": "pass"},
            {"phase": "P3", "task": "publish findings", "method": "update report and sidecar", "input": "done", "output": "final", "dependency": "P2", "acceptance": "pass"},
        ],
        "questioner_checkpoints": [
            {"stage": "after_local_archaeology", "questions": ["q"], "decision": "continue", "reason": "r"},
            {"stage": "after_first_external_round", "questions": ["q"], "decision": "continue", "reason": "r"},
            {"stage": "before_final_report", "questions": ["q"], "decision": "finalize", "reason": "r"},
        ],
        "known_issues": {
            "schema": "knowledgeradar-known-issues/v1",
            "generated_at": "2026-06-16T00:00:00+08:00",
            "report_path": str(report),
            "issues": [{"id": "KI-20260616-001", "description": "x", "discovered_at": "2026-06-16", "status": "未解决", "resolved_in_report": None}],
        },
        "decision_register": {
            "schema": "knowledgeradar-decision-register/v1",
            "generated_at": "2026-06-16T00:00:00+08:00",
            "report_path": str(report),
            "decisions": [{"id": "D001", "title": "x", "status": "approved", "recommended_action": "go", "options": [{"value": "approve"}, {"value": "reject"}], "decision_record_location": ["report"], "next_review": "next"}],
        },
        "process_quality": {
            "schema": "knowledgeradar-process-quality/v1",
            "preflight": {
                "schema": "knowledgeradar-research-preflight/v1",
                "status": "PASS",
                "requires_preflight": True,
                "known_issue_inheritance": {"required_issue_ids": []},
            },
            "capability_handshake": {"health_check": True, "get_capabilities": True},
            "local_archaeology": {"performed": True},
            "source_ecology_plan": {"considered": ["generic_web_ecology"], "selected": [], "skipped": []},
            "validation_ladder": {"selected_profile": "deep", "commands": ["check_research_quality.py"]},
            "presentation": [{"surface": "report"}],
        },
    }
    evidence = tmp_path / "p8.evidence.json"
    evidence.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = validate_research_quality(report, evidence)

    assert result["status"] == "needs_revision"
    assert any(item["code"] == "missing_or_invalid_agentic_search_governance" for item in result["hard_findings"])


def test_codex_thread_trace_adapter_exports_sidecar_artifacts(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    report.write_text("# Report", encoding="utf-8")
    evidence = tmp_path / "report.evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "schema": "knowledgeradar-research-evidence/v1",
                "tool_calls": [{"id": "T001", "tool": "health_check"}],
                "evidence_items": [{"id": "E001", "type": "local_code", "source_ecology": "generic_web_ecology"}],
                "coverage": {"covered_surfaces": ["local_code"], "covered_source_ecologies": ["generic_web_ecology"]},
                "known_issues": {"schema": "knowledgeradar-known-issues/v1", "issues": [{"id": "KI-20260616-001"}]},
                "decision_register": {"schema": "knowledgeradar-decision-register/v1", "decisions": [{"id": "D001"}]},
                "process_quality": {
                    "schema": "knowledgeradar-process-quality/v1",
                    "source_ecology_plan": {},
                    "validation_ladder": {"selected_profile": "report_light"},
                    "presentation": [],
                },
                "agentic_search": {"schema": "knowledgeradar-agentic-search-governance/v1"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    trace = build_codex_thread_trace(report=report, evidence=evidence, task_id="case")

    assert trace["adapter_schema"] == "knowledgeradar-codex-thread-trace-adapter/v4"
    assert "known_issues" in trace["output_artifacts"]
    assert "decision_register" in trace["output_artifacts"]
    assert "agentic_search_governance" in trace["output_artifacts"]


def test_codex_thread_trace_adapter_parses_jsonl_bypass_and_builtin_web(tmp_path: Path) -> None:
    jsonl = tmp_path / "rollout.jsonl"
    turn_id = "turn-1"
    rows = [
        {
            "type": "response_item",
            "payload": {
                "type": "web_search_call",
                "id": "ws_1",
                "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": "{\"cmd\":\"import server\\nserver.kr_web_search(query='x')\"}",
                "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "mcp__knowledgeradar.health_check",
                "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
            },
        },
    ]
    jsonl.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")

    parsed = parse_codex_jsonl_trace(jsonl, turn_id=turn_id)

    assert parsed["builtin_web_search_count"] == 1
    assert parsed["server_import_bypass_count"] == 1
    assert parsed["direct_kr_mcp_tools"] == ["health_check"]
    assert "server.kr_web_search" in parsed["tool_calls"]
    assert parsed["privacy"]["raw_prompt_or_arguments_retained"] is False
    assert "query='x'" not in json.dumps(parsed, ensure_ascii=False)
    assert "import server" not in json.dumps(parsed, ensure_ascii=False)


def test_research_quality_warns_when_mcp_bypass_lacks_record(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    report.write_text("# Governance Report\n\n```mermaid\nflowchart TD\n  P0 --> P1\n  P1 --> P2\n```\n\n## 详细执行步骤\nP0 做什么 怎么做 输入 输出 验收 dependency change boundary。\nP1 做什么 怎么做 输入 输出 验收 dependency change boundary。\nP2 做什么 怎么做 输入 输出 验收 dependency change boundary。\n", encoding="utf-8")
    evidence = tmp_path / "report.evidence.json"
    payload = {
        "schema": "knowledgeradar-research-evidence/v1",
        "artifact": {"title": "治理审计", "report_path": str(report)},
        "budget": {"selected_profile": "balanced"},
        "tool_calls": [{"id": "T001", "tool": "server.kr_web_search"}],
        "evidence_items": [{"id": "E001", "type": "runtime_probe", "summary": "trace"}],
        "claims": [{"id": "C001", "importance": "high", "supporting_evidence_ids": ["E001"]}],
        "coverage": {"required_surfaces": ["runtime_probe"], "covered_surfaces": ["runtime_probe"]},
        "roadmap": [
            {"phase": "P0", "task": "a", "method": "b", "input": "c", "output": "d", "acceptance": "e", "dependency": "none"},
            {"phase": "P1", "task": "a", "method": "b", "input": "c", "output": "d", "acceptance": "e", "dependency": "P0"},
            {"phase": "P2", "task": "a", "method": "b", "input": "c", "output": "d", "acceptance": "e", "dependency": "P1"},
        ],
        "known_issues": {"schema": "knowledgeradar-known-issues/v1", "issues": [{"id": "KI1", "description": "x", "discovered_at": "2026-06-29", "status": "新发现"}]},
        "decision_register": {
            "schema": "knowledgeradar-decision-register/v1",
            "decisions": [
                {"id": "D1", "title": "x", "status": "pending_user_decision", "recommended_action": "approve", "options": [{"value": "a"}, {"value": "b"}], "decision_record_location": ["report"], "next_review": "later"}
            ],
        },
        "process_quality": {
            "schema": "knowledgeradar-process-quality/v1",
            "preflight_skip_reason": "unit test",
            "capability_handshake": {"health_check": True, "get_capabilities": True},
            "local_archaeology": {"performed": True},
            "source_ecology_plan": {"considered": ["runtime_probe"], "selected": ["runtime_probe"], "skipped": []},
            "validation_ladder": {"selected_profile": "balanced", "commands": ["unit"]},
            "presentation": {"roadmap_in_sidecar": True},
        },
        "questioner_checkpoints": [
            {"stage": "after_local_archaeology"},
            {"stage": "after_first_external_round"},
            {"stage": "before_final_report"},
        ],
        "claim_evidence_chains": [
            {"claim_id": "C001", "tool_call_ids": ["T001"], "evidence_ids": ["E001"], "sources": ["trace"], "extracted_facts": ["fact"], "inference": "inf", "limitations": "lim", "strength": "medium"}
        ],
    }
    evidence.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = validate_research_quality(report, evidence)

    assert result["status"] == "needs_revision"
    assert any(item["code"] == "mcp_bypass_reason_needed" for item in result["soft_findings"])


def test_research_quality_accepts_builtin_web_as_host_internal_wave(tmp_path: Path) -> None:
    report = tmp_path / "report.md"
    report.write_text("# Report\n\n```mermaid\nflowchart TD\n  P0 --> P1\n  P1 --> P2\n```\n\n## 详细执行步骤\nP0 做什么 怎么做 输入 输出 验收 dependency change boundary。\nP1 做什么 怎么做 输入 输出 验收 dependency change boundary。\nP2 做什么 怎么做 输入 输出 验收 dependency change boundary。\n", encoding="utf-8")
    evidence = tmp_path / "report.evidence.json"
    payload = {
        "schema": "knowledgeradar-research-evidence/v1",
        "artifact": {"title": "治理审计", "report_path": str(report)},
        "budget": {"selected_profile": "balanced"},
        "tool_calls": [{"id": "T001", "tool": "builtin_web_search"}],
        "evidence_items": [{"id": "E001", "type": "runtime_probe", "summary": "trace"}],
        "claims": [{"id": "C001", "importance": "high", "supporting_evidence_ids": ["E001"]}],
        "coverage": {"required_surfaces": ["runtime_probe"], "covered_surfaces": ["runtime_probe"]},
        "roadmap": [
            {"phase": "P0", "task": "a", "method": "b", "input": "c", "output": "d", "acceptance": "e", "dependency": "none"},
            {"phase": "P1", "task": "a", "method": "b", "input": "c", "output": "d", "acceptance": "e", "dependency": "P0"},
            {"phase": "P2", "task": "a", "method": "b", "input": "c", "output": "d", "acceptance": "e", "dependency": "P1"},
        ],
        "known_issues": {"schema": "knowledgeradar-known-issues/v1", "issues": [{"id": "KI1", "description": "x", "discovered_at": "2026-06-29", "status": "新发现"}]},
        "decision_register": {
            "schema": "knowledgeradar-decision-register/v1",
            "decisions": [
                {"id": "D1", "title": "x", "status": "pending_user_decision", "recommended_action": "approve", "options": [{"value": "a"}, {"value": "b"}], "decision_record_location": ["report"], "next_review": "later"}
            ],
        },
        "process_quality": {
            "schema": "knowledgeradar-process-quality/v1",
            "preflight_skip_reason": "unit test",
            "capability_handshake": {"health_check": True, "get_capabilities": True},
            "local_archaeology": {"performed": True},
            "source_ecology_plan": {"considered": ["generic_web_ecology"], "selected": ["generic_web_ecology"], "skipped": []},
            "host_internal_web_wave": {
                "wave_id": "host_internal_web_wave",
                "strategy_tree": "web_search.provider_wave",
                "relationship_to_kr": "finger_of_kr_hand",
                "reason": "official docs retrieval",
            },
            "validation_ladder": {"selected_profile": "balanced", "commands": ["unit"]},
            "presentation": {"roadmap_in_sidecar": True},
        },
        "questioner_checkpoints": [
            {"stage": "after_local_archaeology"},
            {"stage": "after_first_external_round"},
            {"stage": "before_final_report"},
        ],
        "claim_evidence_chains": [
            {"claim_id": "C001", "tool_call_ids": ["T001"], "evidence_ids": ["E001"], "sources": ["trace"], "extracted_facts": ["fact"], "inference": "inf", "limitations": "lim", "strength": "medium"}
        ],
    }
    evidence.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = validate_research_quality(report, evidence)

    assert not any(item["code"] == "generic_web_fallback_reason_needed" for item in result["soft_findings"])
