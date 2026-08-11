from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("kr_test_selector_for_test", ROOT / "scripts" / "kr_test_selector.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

describe_changed_path = MODULE.describe_changed_path
select_tests = MODULE.select_tests


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_selects_tests_that_import_changed_source(tmp_path: Path) -> None:
    write(tmp_path / "src" / "sample" / "router.py", "def route():\n    return None\n")
    write(
        tmp_path / "tests" / "test_router.py",
        "from sample.router import route\n\n\ndef test_route():\n    assert route is not None\n",
    )

    result = select_tests(["src/sample/router.py"], tmp_path)

    assert result["confidence"] == "high"
    assert [item["path"] for item in result["selected_tests"]] == ["tests/test_router.py"]
    assert "tests/test_router.py" in result["suggested_command"]


def test_selects_tests_that_patch_changed_source_by_string(tmp_path: Path) -> None:
    write(tmp_path / "src" / "sample" / "client.py", "def call():\n    return None\n")
    write(
        tmp_path / "tests" / "test_client.py",
        "def test_client(monkeypatch):\n"
        "    monkeypatch.setattr('sample.client.call', lambda: 'ok')\n"
        "    assert True\n",
    )

    result = select_tests(["src/sample/client.py"], tmp_path)

    assert result["confidence"] == "high"
    assert result["selected_tests"][0]["path"] == "tests/test_client.py"
    assert any("references sample.client" in reason for reason in result["selected_tests"][0]["reasons"])


def test_selects_changed_test_file_itself(tmp_path: Path) -> None:
    write(tmp_path / "tests" / "test_self.py", "def test_self():\n    assert True\n")

    result = select_tests(["tests/test_self.py"], tmp_path)

    assert result["confidence"] == "high"
    assert result["selected_tests"][0]["path"] == "tests/test_self.py"
    assert result["selected_tests"][0]["nodeids"] == ["tests/test_self.py::test_self"]


def test_doc_only_changes_do_not_infer_pytest(tmp_path: Path) -> None:
    write(tmp_path / "tests" / "test_anything.py", "def test_anything():\n    assert True\n")

    result = select_tests(["docs/reference/report.md"], tmp_path)

    assert result["doc_only"] is True
    assert result["confidence"] == "none"
    assert result["selected_tests"] == []
    assert result["suggested_command"] is None


def test_profile_json_uses_package_ancestry_not_manual_provider_lists(tmp_path: Path) -> None:
    write(tmp_path / "src" / "academic_providers" / "profiles" / "academic_provider_profiles.json", "{}\n")
    write(
        tmp_path / "tests" / "test_academic_profiles.py",
        "from academic_providers.profile import load_provider_profiles\n\n"
        "def test_profiles():\n"
        "    assert load_provider_profiles\n",
    )

    result = select_tests(["src/academic_providers/profiles/academic_provider_profiles.json"], tmp_path)

    assert result["confidence"] in {"medium", "high"}
    assert result["selected_tests"][0]["path"] == "tests/test_academic_profiles.py"
    assert any("academic_providers" in reason for reason in result["selected_tests"][0]["reasons"])


def test_describe_changed_path_includes_ancestors_for_non_python_src_files() -> None:
    changed = describe_changed_path("src/academic_providers/profiles/academic_provider_profiles.json")

    assert "academic_providers" in changed.modules
    assert "academic_providers.profiles" in changed.modules
    assert "academic_provider_profiles" in changed.tokens
