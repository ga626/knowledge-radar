"""Recommend closeout checks and redact local path leaks."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from runtime.project_state import redact_path_text
from runtime.project_policy import normalize_repo_path


PATH_LEAK_PATTERNS = (
    re.compile(r"D:\\Projects\\KnowledgeRadar", re.IGNORECASE),
    re.compile(r"D:/Projects/KnowledgeRadar", re.IGNORECASE),
    re.compile(r"D:\\AI Studio\\KnowledgeRadar", re.IGNORECASE),
    re.compile(r"D:/AI Studio/KnowledgeRadar", re.IGNORECASE),
    re.compile(r"C:\\kr-profiles", re.IGNORECASE),
    re.compile(r"C:/kr-profiles", re.IGNORECASE),
)


def scan_text_for_path_leaks(text: str) -> list[str]:
    hits: list[str] = []
    for pattern in PATH_LEAK_PATTERNS:
        if pattern.search(text or ""):
            hits.append(pattern.pattern)
    return hits


def scan_files_for_path_leaks(paths: list[str | Path], *, root: Path | None = None) -> list[dict[str, Any]]:
    repo = root or Path.cwd()
    findings: list[dict[str, Any]] = []
    for item in paths:
        path = Path(item)
        if not path.is_absolute():
            path = repo / path
        if path.is_dir():
            candidates = [p for p in path.rglob("*.md") if ".git" not in p.parts]
        else:
            candidates = [path]
        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                findings.append({"path": str(candidate), "error": str(exc)})
                continue
            hits = scan_text_for_path_leaks(text)
            if hits:
                findings.append({"path": str(candidate), "hits": hits})
    return findings


def recommend_closeout(changed_paths: list[str]) -> dict[str, Any]:
    normalized = [normalize_repo_path(path) for path in changed_paths if path]
    checks: set[str] = {"git diff --check", "python scripts/kr_quality_gate.py --changed"}
    reasons: list[str] = []
    if normalized:
        reasons.append("quality_gate_required")
    if any(path == "config/project-structure.manifest.json" or path.startswith("config/package-manifest") for path in normalized):
        reasons.append("structure_or_package_manifest_changed")
        checks.update(
            {
                "python scripts/generate_structure_docs.py",
                "python scripts/check_structure_docs.py",
                "python scripts/check_doc_drift.py",
                "python scripts/build_product_lite_package.py --dry-run",
                "python scripts/verify_package_integrity.py",
            }
        )
    if any(path.startswith("src/") or path.startswith("tests/") for path in normalized):
        reasons.append("python_code_or_tests_changed")
        checks.add("python -m pytest -q")
    if any(path.startswith("project_status/") for path in normalized):
        reasons.append("project_status_changed")
        checks.add("python scripts/kr_project_state.py --check-fresh")
        checks.add("python scripts/kr_redact_report_paths.py --check project_status --strict")
    return {
        "schema": "knowledgeradar-closeout-advisor/v1",
        "changed_paths": normalized,
        "reasons": reasons,
        "recommended_checks": sorted(checks),
    }


def redact_file(path: Path, *, root: Path | None = None) -> bool:
    original = path.read_text(encoding="utf-8", errors="replace")
    redacted = redact_path_text(original, root)
    if redacted == original:
        return False
    path.write_text(redacted, encoding="utf-8")
    return True
