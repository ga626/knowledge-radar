from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.verify_research_mechanisms import run_verification


def test_research_mechanism_smoke_verification_runs() -> None:
    payload = run_verification("smoke")

    assert payload["schema"] == "knowledgeradar-research-mechanism-verification/v1"
    assert payload["profile"] == "smoke"
    assert payload["status"] == "PASS", payload["steps"]
    names = {step["name"] for step in payload["steps"]}
    assert {
        "research_preflight_can_run",
        "agentic_search_governance_builder",
        "natural_calling_scores_agentic_governance",
        "source_ecology_boundary_probe_plan",
        "capability_atlas_feedback_suggestions",
    } <= names
