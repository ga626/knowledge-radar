from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_agentic_search_governance import build_agentic_search_governance, apply_to_sidecar


def _sidecar() -> dict[str, object]:
    return {
        "evidence_items": [{"id": "E001", "source_ecology": "generic_web_ecology"}],
        "coverage": {"covered_source_ecologies": ["generic_web_ecology"]},
        "claims": [
            {"id": "C001", "importance": "high", "supporting_evidence_ids": ["E001"]},
            {"id": "C002", "importance": "high", "supporting_evidence_ids": ["E999"]},
        ],
    }


def test_build_agentic_search_governance_marks_uncovered_high_claim() -> None:
    block = build_agentic_search_governance(_sidecar())

    assert block["schema"] == "knowledgeradar-agentic-search-governance/v1"
    assert block["agent_policy"] == "model_decides_rounds_tools_and_stop"
    assert block["budget_semantics"]["position"] == "runtime_sla_not_tool_route"
    assert block["stopping_criteria"]["decision"] == "continue"
    assert block["marginal_yield"]["uncovered_high_importance_claim_ids"] == ["C002"]
    c2 = next(item for item in block["evidence_coverage_ledger"] if item["claim_id"] == "C002")
    assert c2["gap"] == "supporting_evidence_not_found"


def test_apply_to_sidecar_can_write_updated_sidecar(tmp_path: Path) -> None:
    source = tmp_path / "report.evidence.json"
    output = tmp_path / "updated.evidence.json"
    source.write_text(json.dumps(_sidecar(), ensure_ascii=False), encoding="utf-8")

    result = apply_to_sidecar(source, output=output)

    assert result["status"] == "PASS"
    updated = json.loads(output.read_text(encoding="utf-8-sig"))
    assert updated["agentic_search"]["schema"] == "knowledgeradar-agentic-search-governance/v1"
