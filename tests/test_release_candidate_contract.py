from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = _load("build_product_lite_package")
verifier = _load("verify_release_candidate")


def test_product_manifest_only_references_public_files() -> None:
    manifest = json.loads((ROOT / "config" / "package-manifest.product-lite.json").read_text(encoding="utf-8"))
    _, missing, _ = builder.collect_files(manifest)
    assert missing == []


def test_product_manifest_includes_runtime_and_plugin() -> None:
    manifest = json.loads((ROOT / "config" / "package-manifest.product-lite.json").read_text(encoding="utf-8"))
    files, _, _ = builder.collect_files(manifest)
    paths = {item.relative_to(ROOT).as_posix() for item in files}
    assert "src/server.py" in paths
    assert "scripts/product_install.py" in paths
    assert "bridge/xhs_mcp_bridge.cjs" in paths
    assert "config/codex-product/plugin/knowledgeradar-research/.codex-plugin/plugin.json" in paths
    assert "scripts/install.bat" not in paths
    assert "scripts/setup_codex_product.py" not in paths
    assert "scripts/verify_release_candidate.py" not in paths


def test_product_manifest_excludes_generated_egg_metadata() -> None:
    manifest = json.loads((ROOT / "config" / "package-manifest.product-lite.json").read_text(encoding="utf-8"))
    assert builder.is_excluded("src/knowledgeradar.egg-info/PKG-INFO", manifest["exclude"])


def test_mcp_dependency_stays_on_the_fastmcp_compatibility_line() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"mcp>=1.9,<2.0"' in pyproject


def test_candidate_provenance_has_no_absolute_source_path(tmp_path: Path) -> None:
    output = tmp_path / "package"
    output.mkdir()
    manifest = ROOT / "config" / "package-manifest.product-lite.json"
    builder.write_metadata(output, manifest, [], "a" * 40)
    provenance = json.loads((output / "package-provenance.json").read_text(encoding="utf-8"))
    assert provenance["source_dirty"] is False
    assert "source_root" not in provenance
    assert all("Users" not in str(value) for value in provenance.values())
    assert (output / "SBOM.json").is_file()


def test_safe_extract_rejects_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "bad.zip"
    import zipfile
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("KnowledgeRadar/../outside.txt", "no")
    try:
        verifier.safe_extract(archive, tmp_path / "extract")
    except RuntimeError as exc:
        assert "unsafe" in str(exc)
    else:
        raise AssertionError("unsafe archive was accepted")
