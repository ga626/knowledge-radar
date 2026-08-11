from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.kr_report_blueprint import create_blueprint
from scripts.kr_report_schema_lint import lint_report_schema


def test_report_blueprint_creates_schema_first_artifacts(tmp_path: Path) -> None:
    report = tmp_path / "docs" / "reference" / "测试治理报告-2026-07-06.md"

    result = create_blueprint(report, title="测试治理报告", task_type="governance_research", profile="deep")

    evidence = report.with_suffix(".evidence.json")
    preflight = report.with_suffix(".preflight.json")
    assert result["status"] == "PASS"
    assert report.exists()
    assert evidence.exists()
    assert preflight.exists()
    lint = lint_report_schema(report, evidence, preflight)
    assert lint["status"] == "PASS"
