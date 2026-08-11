from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("kr_test_impact_index_for_test", ROOT / "scripts" / "kr_test_impact_index.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

SELECTOR_SPEC = importlib.util.spec_from_file_location("kr_test_selector_for_impact_test", ROOT / "scripts" / "kr_test_selector.py")
assert SELECTOR_SPEC and SELECTOR_SPEC.loader
SELECTOR = importlib.util.module_from_spec(SELECTOR_SPEC)
sys.modules[SELECTOR_SPEC.name] = SELECTOR
SELECTOR_SPEC.loader.exec_module(SELECTOR)


class FakeCoverageData:
    def __init__(self, contexts: dict[str, dict[int, list[str]]]) -> None:
        self.contexts = contexts

    def measured_files(self) -> list[str]:
        return list(self.contexts)

    def contexts_by_lineno(self, filename: str) -> dict[int, list[str]]:
        return self.contexts[filename]

    def set_query_contexts(self, _contexts) -> None:
        return None


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_build_index_from_coverage_contexts_keeps_test_nodeids(tmp_path: Path) -> None:
    source = tmp_path / "src" / "sample" / "dynamic.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    write(tmp_path / "tests" / "test_dynamic.py", "def test_dynamic():\n    assert True\n")
    data = FakeCoverageData(
        {
            str(source): {
                1: ["tests/test_dynamic.py::test_dynamic"],
                2: [""],
            }
        }
    )

    payload = MODULE.build_index_from_coverage_data(data, tmp_path)

    assert payload["schema"] == "knowledgeradar-test-impact-index/v1"
    entry = payload["tests"]["tests/test_dynamic.py::test_dynamic"]
    assert entry["touched_files"] == ["src/sample/dynamic.py"]
    assert entry["test_file"] == "tests/test_dynamic.py"
    assert entry["test_file_hash"]
    assert entry["source_hashes"]["src/sample/dynamic.py"]


def test_selector_uses_impact_index_for_dynamic_relationship(tmp_path: Path) -> None:
    write(tmp_path / "src" / "sample" / "dynamic.py", "VALUE = 1\n")
    write(tmp_path / "tests" / "test_dynamic.py", "def test_dynamic():\n    assert True\n")
    impact = {
        "schema": "knowledgeradar-test-impact-index/v1",
        "tests": {
            "tests/test_dynamic.py::test_dynamic": {
                "touched_files": ["src/sample/dynamic.py"],
            }
        },
    }
    impact_path = tmp_path / "runtime" / "cache" / "test-impact-index.json"
    write(impact_path, json.dumps(impact))

    result = SELECTOR.select_tests(["src/sample/dynamic.py"], tmp_path, impact_index_path=impact_path)

    assert result["confidence"] == "high"
    assert result["index"]["impact_index_loaded"] is True
    assert result["selected_tests"][0]["path"] == "tests/test_dynamic.py"
    assert "coverage impact index" in result["selected_tests"][0]["reasons"][0]


def test_selector_ignores_stale_impact_entry_when_test_file_changed(tmp_path: Path) -> None:
    write(tmp_path / "src" / "sample" / "dynamic.py", "VALUE = 1\n")
    test_file = tmp_path / "tests" / "test_dynamic.py"
    write(test_file, "def test_dynamic():\n    assert True\n")
    stale_hash = "0" * 64
    impact = {
        "schema": "knowledgeradar-test-impact-index/v1",
        "tests": {
            "tests/test_dynamic.py::test_dynamic": {
                "test_file": "tests/test_dynamic.py",
                "test_file_hash": stale_hash,
                "touched_files": ["src/sample/dynamic.py"],
            }
        },
    }
    impact_path = tmp_path / "runtime" / "cache" / "test-impact-index.json"
    write(impact_path, json.dumps(impact))

    result = SELECTOR.select_tests(["src/sample/dynamic.py"], tmp_path, impact_index_path=impact_path)

    assert result["index"]["impact_index_loaded"] is True
    assert result["index"]["impact_index_stale_entries"] == 1
    assert result["selected_tests"] == []
    assert result["fallback_required"] is True
