"""Pre-change policy checks for AI coding agents."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any


BLOCKED_PREFIXES = (
    "dist/",
    "release/",
    "local/",
    "browser_data/",
    "data/",
    "src/data/",
    ".python312/",
)

RUNTIME_ALLOWED = {
    "runtime/README.md",
    "runtime/.gitkeep",
}

SENSITIVE_PATHS = {
    ".env",
    "config/runtime.env",
    "config/mcp-approvals.json",
    "config/profile_registry.json",
}

STRUCTURAL_PATHS = {
    "AGENTS.md",
    "config/project-structure.manifest.json",
    "config/package-manifest.product-lite.json",
    "scripts/build_product_lite_package.py",
    "scripts/verify_package_integrity.py",
}

CAPABILITY_PATHS = {
    "src/capabilities.py",
    "src/runtime/health_checks.py",
    "scripts/verify_all_capabilities.py",
}


def normalize_repo_path(path: str) -> str:
    text = str(path or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return str(PurePosixPath(text))


def evaluate_paths(paths: list[str]) -> dict[str, Any]:
    blocked: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    checks: set[str] = set()
    for raw in paths:
        path = normalize_repo_path(raw)
        if not path or path == ".":
            continue
        if path in SENSITIVE_PATHS:
            blocked.append({"path": path, "reason": "secret_or_local_state_file"})
        if path.startswith("runtime/") and path not in RUNTIME_ALLOWED:
            blocked.append({"path": path, "reason": "runtime_local_state"})
        if any(path.startswith(prefix) for prefix in BLOCKED_PREFIXES):
            blocked.append({"path": path, "reason": "generated_or_local_state_boundary"})
        if path in STRUCTURAL_PATHS:
            warnings.append({"path": path, "reason": "structural_or_package_boundary"})
            checks.update(
                {
                    "python scripts/generate_structure_docs.py",
                    "python scripts/check_structure_docs.py",
                    "python scripts/check_doc_drift.py",
                    "python scripts/build_product_lite_package.py --dry-run",
                    "python scripts/verify_package_integrity.py",
                }
            )
        if path in CAPABILITY_PATHS:
            warnings.append({"path": path, "reason": "capabilities_health_validation_surface"})
            checks.update({"python -m pytest -q tests/test_capabilities_runtime_environment.py tests/test_validation_semantics.py"})
        if path.startswith("project_status/"):
            checks.add("python scripts/kr_redact_report_paths.py --check project_status")
    status = "BLOCK" if blocked else ("WARN" if warnings else "PASS")
    return {
        "schema": "knowledgeradar-pre-change-policy/v1",
        "status": status,
        "blocked": blocked,
        "warnings": warnings,
        "recommended_checks": sorted(checks),
    }


def evaluate_pre_change(intent: str = "", paths: list[str] | None = None, diff_text: str = "") -> dict[str, Any]:
    result = evaluate_paths(paths or [])
    notes: list[str] = []
    if "PROJECT_RULES.md" in diff_text or "PROJECT_RULES" in intent:
        notes.append("AGENTS.md is the rule entrypoint; PROJECT_RULES.md should not be introduced as a separate source of truth.")
        if result["status"] == "PASS":
            result["status"] = "WARN"
    if diff_text and any(token in diff_text for token in ("D:\\Projects\\KnowledgeRadar", "D:/Projects/KnowledgeRadar", "D:\\AI Studio\\KnowledgeRadar", "C:\\kr-profiles")):
        notes.append("Diff contains local absolute paths; keep development process documents out of Git and redact project_status before committing.")
        if result["status"] == "PASS":
            result["status"] = "WARN"
    result["intent"] = intent
    result["notes"] = notes
    return result
