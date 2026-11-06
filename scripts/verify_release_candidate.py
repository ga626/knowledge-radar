"""Verify one generated release-candidate ZIP without using local user state."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {
    "README.md",
    "LICENSE",
    "pyproject.toml",
    "src/server.py",
    "scripts/product_install.py",
    "config/codex-product/plugin/knowledgeradar-research/.codex-plugin/plugin.json",
    "package-provenance.json",
    "SBOM.json",
}
EXPECTED_TOOLS = {"health_check", "get_capabilities", "kr_research", "finalize_research_task"}


def package_content_issues(package_root: Path) -> list[str]:
    script = ROOT / "scripts" / "verify_package_integrity.py"
    spec = importlib.util.spec_from_file_location("knowledgeradar_package_integrity", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load package-integrity verifier")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.check_package_content(package_root)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_runtime_inputs(package_root: Path) -> None:
    metadata = json.loads((package_root / "config/runtime-artifacts.json").read_text(encoding="utf-8"))
    for relative, key in (("pyproject.toml", "pyproject_sha256"), ("config/runtime-win-py312.lock", "lock_sha256")):
        if sha256_file(package_root / relative) != metadata.get(key):
            raise RuntimeError("candidate runtime lock identity mismatch: " + relative)


def safe_extract(archive: Path, destination: Path) -> Path:
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if any(Path(name).is_absolute() or ".." in Path(name).parts for name in names):
            raise RuntimeError("archive contains unsafe paths")
        roots = {Path(name).parts[0] for name in names if Path(name).parts}
        if roots != {"KnowledgeRadar"}:
            raise RuntimeError("archive must contain one KnowledgeRadar root")
        bundle.extractall(destination)
    return destination / "KnowledgeRadar"


def stdio_probe(package_root: Path) -> list[str]:
    """Use the same isolated owner lifetime as the public source verifier."""
    scripts_path = str(ROOT / "scripts")
    previous_path = list(sys.path)
    try:
        sys.path.insert(0, scripts_path)
        from verify_codex_product import stdio_probe as isolated_stdio_probe

        result = isolated_stdio_probe(package_root)
    finally:
        sys.path[:] = previous_path
    if result.get("status") != "PASS":
        raise RuntimeError("candidate stdio probe failed: " + str(result.get("error", result)))
    return result["tools"]


def verify(candidate_dir: Path) -> dict[str, object]:
    archive = candidate_dir / "KnowledgeRadar.zip"
    receipt_path = candidate_dir / "candidate-receipt.json"
    if not archive.is_file() or not receipt_path.is_file():
        raise RuntimeError("candidate archive or receipt is missing")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("source_dirty") is not False or receipt.get("archive_sha256") != sha256_file(archive):
        raise RuntimeError("candidate receipt does not bind the clean archive")
    with tempfile.TemporaryDirectory(prefix="knowledgeradar-candidate-extract-") as temp:
        package_root = safe_extract(archive, Path(temp))
        missing = sorted(item for item in REQUIRED if not (package_root / item).is_file())
        if missing:
            raise RuntimeError(f"candidate is missing required files: {', '.join(missing)}")
        provenance = json.loads((package_root / "package-provenance.json").read_text(encoding="utf-8"))
        if provenance.get("source_dirty") is not False or "source_root" in provenance:
            raise RuntimeError("candidate provenance exposes source state or local path")
        issues = package_content_issues(package_root)
        if issues:
            raise RuntimeError("candidate contains private-content patterns: " + "; ".join(issues))
        verify_runtime_inputs(package_root)
        tools = stdio_probe(package_root)
    missing_tools = sorted(EXPECTED_TOOLS - set(tools))
    if missing_tools:
        raise RuntimeError(f"candidate stdio surface missing tools: {', '.join(missing_tools)}")
    return {"archive": archive.name, "archive_sha256": sha256_file(archive), "tool_count": len(tools), "tools": tools, "status": "PASS"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a KnowledgeRadar release candidate.")
    parser.add_argument("--candidate-dir", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = verify(Path(args.candidate_dir).resolve())
    except (ImportError, OSError, RuntimeError, KeyError, TypeError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        result = {"status": "FAIL", "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
