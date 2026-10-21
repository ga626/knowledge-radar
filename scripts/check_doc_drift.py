from __future__ import annotations

import fnmatch
import json
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "config" / "project-structure.manifest.json"


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def run_check_structure_docs() -> int:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate_structure_docs.py"), "--check"],
        cwd=ROOT,
    )
    return result.returncode


def run_generated_check(script_name: str) -> int:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script_name), "--check"],
        cwd=ROOT,
    )
    return result.returncode


def pattern_matches(pattern: str, candidate: str) -> bool:
    normalized = candidate.replace("\\", "/")
    pattern = pattern.replace("\\", "/")
    return fnmatch.fnmatch(normalized, pattern)


def normalize_relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def is_glob_pattern(pattern: str) -> bool:
    return any(char in pattern for char in "*?[]")


def existing_paths_for_pattern(pattern: str) -> List[str]:
    normalized = pattern.replace("\\", "/")
    if is_glob_pattern(normalized):
        paths = [
            normalize_relative(path)
            for path in ROOT.glob(normalized)
            if path.is_file()
        ]
        return sorted(paths)
    path = ROOT / normalized
    return [normalized] if path.exists() else []


def package_manifest_issues(structure: dict, package_manifest: dict) -> List[str]:
    include = set(package_manifest.get("include", []))
    exclude = set(package_manifest.get("exclude", []))
    required_includes = set(structure.get('required_product_includes', [
        "AGENTS.md",
        "STRUCTURE.md",
        "config/project-structure.manifest.json",
        "docs/AI_WORKING_GUIDE.md",
    ]))
    issues: List[str] = []
    for item in sorted(required_includes - include):
        issues.append(f"package manifest missing required include: {item}")
    for pattern in structure.get("forbidden_release_patterns", []):
        if pattern in include:
            issues.append(f"forbidden pattern appears in include list: {pattern}")
        if pattern not in exclude:
            issues.append(f"package manifest missing forbidden exclude: {pattern}")
    for item in include:
        for pattern in structure.get("forbidden_release_patterns", []):
            if pattern_matches(pattern, item):
                issues.append(f"included path matches forbidden release pattern: {item} <= {pattern}")
    return issues


def watch_path_issues(structure: dict) -> List[str]:
    issues: List[str] = []
    watch_paths = set(structure.get("watch_paths", []))
    generated_docs = set(structure.get("generated_docs", [])) | set(structure.get("local_generated_docs", []))

    for path in sorted(watch_paths):
        if is_glob_pattern(path):
            issues.append(f"watch_paths must contain concrete paths, not globs: {path}")
        elif not (ROOT / path).exists():
            issues.append(f"watch path missing on disk: {path}")

    for pattern in structure.get("watch_path_globs", []):
        for path in existing_paths_for_pattern(pattern):
            if path in generated_docs:
                continue
            if path not in watch_paths:
                issues.append(f"watch path glob matched unlisted file: {path} <= {pattern}")
    return issues


def forbidden_local_state_issues(structure: dict) -> List[str]:
    issues: List[str] = []
    for relative in structure.get("forbidden_local_state_paths", []):
        path = ROOT / relative
        if path.exists():
            issues.append(f"forbidden local/private state path exists: {relative}")
    return issues


def required_files_exist(paths: Iterable[str]) -> List[str]:
    missing = []
    for relative in paths:
        if not (ROOT / relative).exists():
            missing.append(relative)
    return missing


def main() -> int:
    structure = load_json(MANIFEST_PATH)
    package_manifest = load_json(ROOT / structure["package_manifest"])

    exit_code = run_check_structure_docs()
    docs_index_exit = run_generated_check("generate_docs_index.py")
    script_inventory_exit = run_generated_check("generate_script_lifecycle_inventory.py")
    process_docs_exit = run_generated_check("generate_process_docs_index.py")
    issues = package_manifest_issues(structure, package_manifest)
    issues.extend(watch_path_issues(structure))
    issues.extend(forbidden_local_state_issues(structure))
    missing = required_files_exist(structure.get('required_structure_docs', ["AGENTS.md", "STRUCTURE.md", "docs/AI_WORKING_GUIDE.md"]))
    issues.extend(f"required structure doc missing: {path}" for path in missing)

    if docs_index_exit:
        issues.append("docs index is out of date: run scripts/generate_docs_index.py")
    if script_inventory_exit:
        issues.append("script lifecycle inventory is out of date: run scripts/generate_script_lifecycle_inventory.py")
    if process_docs_exit:
        issues.append("process docs index is out of date: run scripts/generate_process_docs_index.py")

    if issues or exit_code or docs_index_exit or script_inventory_exit or process_docs_exit:
        print("Documentation drift check failed.")
        for issue in issues:
            print(f"- {issue}")
        return 1
    print("Documentation drift check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
