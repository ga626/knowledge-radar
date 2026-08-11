from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "kr_test_redundancy_report_for_test",
    ROOT / "scripts" / "kr_test_redundancy_report.py",
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_redundancy_report_groups_same_shape_tests(tmp_path: Path) -> None:
    write(
        tmp_path / "tests" / "test_sample.py",
        "import math\n\n"
        "def test_one(tmp_path):\n"
        "    assert math.sqrt(4) == 2\n\n"
        "def test_two(tmp_path):\n"
        "    assert math.sqrt(9) == 3\n\n"
        "def test_other(monkeypatch):\n"
        "    assert True\n",
    )

    report = MODULE.build_redundancy_report(tmp_path)

    assert report["schema"] == "knowledgeradar-test-redundancy-report/v1"
    assert report["test_count"] == 3
    assert report["cluster_count"] >= 1
    assert any(cluster["size"] == 2 for cluster in report["clusters"])


def test_redundancy_report_human_output_is_short(tmp_path: Path) -> None:
    write(tmp_path / "tests" / "test_sample.py", "def test_one():\n    assert True\n\ndef test_two():\n    assert True\n")

    report = MODULE.build_redundancy_report(tmp_path)
    text = MODULE.format_human(report, limit=1)

    assert "candidate clusters:" in text
    assert len(text.splitlines()) <= 6
