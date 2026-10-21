"""Infer focused pytest targets from changed files.

The selector is intentionally read-only. It does not run pytest and does not
write quality state. It builds a small AST index from tests and lets imports,
patch strings, and path/module ancestry decide which tests are likely relevant.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_IMPACT_INDEX = Path("runtime/cache/test-impact-index.json")
TOKEN_STOPWORDS = {
    "code",
    "codex",
    "data",
    "file",
    "gate",
    "json",
    "open",
    "project",
    "quality",
    "report",
    "state",
    "status",
    "test",
    "tests",
}


@dataclass(frozen=True)
class ChangedPath:
    path: str
    modules: tuple[str, ...] = ()
    tokens: tuple[str, ...] = ()
    is_test: bool = False
    is_code_like: bool = False
    is_doc_like: bool = False


@dataclass
class TestFile:
    path: str
    nodeids: list[str] = field(default_factory=list)
    imports: set[str] = field(default_factory=set)
    strings: set[str] = field(default_factory=set)
    patch_strings: set[str] = field(default_factory=set)


@dataclass
class TestSelection:
    path: str
    score: int
    reasons: list[str]
    nodeids: list[str]


def normalize_path(path: str | Path) -> str:
    return str(path).replace("\\", "/").lstrip("./")


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _ancestors(module: str) -> list[str]:
    parts = [part for part in module.split(".") if part]
    return [".".join(parts[:idx]) for idx in range(len(parts), 0, -1)]


def _tokens_from_path(rel_path: str) -> set[str]:
    parts = Path(rel_path).parts
    tokens: set[str] = set()
    for part in parts:
        stem = Path(part).stem
        for token in stem.replace("-", "_").split("_"):
            if len(token) >= 4 and token.lower() not in TOKEN_STOPWORDS:
                tokens.add(token.lower())
        if len(stem) >= 4 and stem.lower() not in TOKEN_STOPWORDS:
            tokens.add(stem.lower())
    return tokens


def describe_changed_path(path: str | Path) -> ChangedPath:
    rel = normalize_path(path)
    parts = Path(rel).parts
    suffix = Path(rel).suffix.lower()
    modules: set[str] = set()
    tokens = _tokens_from_path(rel)
    is_test = rel.startswith("tests/") and suffix == ".py"
    is_doc_like = rel.startswith(("docs/", "project_status/")) or suffix in {".md", ".rst", ".txt"}
    is_code_like = suffix == ".py" or rel.startswith(("src/", "scripts/", "tests/", "config/"))

    if parts and parts[0] == "src" and len(parts) > 1:
        package_parts = list(parts[1:])
        if suffix == ".py":
            package_parts[-1] = Path(package_parts[-1]).stem
            if package_parts[-1] == "__init__":
                package_parts = package_parts[:-1]
        else:
            package_parts[-1] = Path(package_parts[-1]).stem
        module = ".".join(part for part in package_parts if part)
        if module:
            modules.update(_ancestors(module))
    elif parts and parts[0] == "scripts" and suffix == ".py" and len(parts) > 1:
        module = f"scripts.{Path(parts[-1]).stem}"
        modules.add(module)
        modules.add(Path(parts[-1]).stem)
    elif is_test:
        module = ".".join(Path(part).stem if idx == len(parts) - 1 else part for idx, part in enumerate(parts))
        modules.add(module)

    return ChangedPath(
        path=rel,
        modules=tuple(sorted(modules, key=lambda item: (-item.count("."), item))),
        tokens=tuple(sorted(tokens)),
        is_test=is_test,
        is_code_like=is_code_like,
        is_doc_like=is_doc_like,
    )


class _TestAstVisitor(ast.NodeVisitor):
    def __init__(self, rel_path: str) -> None:
        self.rel_path = rel_path
        self.nodeids: list[str] = []
        self.imports: set[str] = set()
        self.strings: set[str] = set()
        self.patch_strings: set[str] = set()

    def visit_Import(self, node: ast.Import) -> Any:
        for alias in node.names:
            self.imports.add(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:
        if node.module:
            self.imports.add(node.module)
            for alias in node.names:
                if alias.name != "*":
                    self.imports.add(f"{node.module}.{alias.name}")
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        if node.name.startswith("test_"):
            self.nodeids.append(f"{self.rel_path}::{node.name}")
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        if node.name.startswith("test_"):
            self.nodeids.append(f"{self.rel_path}::{node.name}")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> Any:
        if isinstance(node.value, str):
            value = node.value.strip()
            if value:
                self.strings.add(value)

    def visit_Call(self, node: ast.Call) -> Any:
        if _is_patch_call(node.func):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    value = arg.value.strip()
                    if value:
                        self.patch_strings.add(value)
        self.generic_visit(node)


def _is_patch_call(func: ast.expr) -> bool:
    if isinstance(func, ast.Name):
        return func.id in {"patch"}
    if isinstance(func, ast.Attribute):
        if func.attr in {"patch", "setattr"}:
            return True
        if func.attr == "object" and isinstance(func.value, ast.Name) and func.value.id == "patch":
            return True
    return False


def parse_test_file(path: Path, root: Path = ROOT) -> TestFile:
    rel_path = normalize_path(path.relative_to(root))
    visitor = _TestAstVisitor(rel_path)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel_path)
    except (OSError, SyntaxError, UnicodeDecodeError):
        return TestFile(path=rel_path)
    visitor.visit(tree)
    return TestFile(
        path=rel_path,
        nodeids=visitor.nodeids,
        imports=visitor.imports,
        strings=visitor.strings,
        patch_strings=visitor.patch_strings,
    )


def build_test_index(root: Path = ROOT) -> list[TestFile]:
    tests_dir = root / "tests"
    if not tests_dir.exists():
        return []
    return [parse_test_file(path, root) for path in sorted(tests_dir.glob("test_*.py"))]


def load_impact_index(root: Path = ROOT, path: str | Path = DEFAULT_IMPACT_INDEX) -> dict[str, Any]:
    index_path = Path(path)
    if not index_path.is_absolute():
        index_path = root / index_path
    try:
        payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if payload.get("schema") != "knowledgeradar-test-impact-index/v1":
        return {}
    return payload


def discover_changed_paths(root: Path = ROOT) -> list[str]:
    commands = (
        ("git", "diff", "--name-only", "HEAD"),
        ("git", "ls-files", "--others", "--exclude-standard"),
    )
    paths: list[str] = []
    for command in commands:
        proc = subprocess.run(
            command,
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        for line in proc.stdout.splitlines():
            rel = normalize_path(line.strip())
            if rel and rel not in paths:
                paths.append(rel)
    return paths


def _module_related(left: str, right: str) -> bool:
    return left == right or left.startswith(f"{right}.") or right.startswith(f"{left}.")


def _valid_impact_entry(test: TestFile, entry: Any, root: Path = ROOT) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    test_file = str(entry.get("test_file") or test.path).replace("\\", "/")
    if test_file != test.path:
        return None
    expected_hash = entry.get("test_file_hash")
    if expected_hash and expected_hash != _sha256(root / test.path):
        return None
    return entry


def impact_index_stats(index: list[TestFile], impact_index: dict[str, Any], root: Path = ROOT) -> dict[str, int]:
    tests = impact_index.get("tests", {})
    if not isinstance(tests, dict):
        return {"entries": 0, "usable_entries": 0, "stale_entries": 0}
    test_by_nodeid = {nodeid: test for test in index for nodeid in test.nodeids}
    usable = 0
    stale = 0
    for nodeid, entry in tests.items():
        test = test_by_nodeid.get(str(nodeid))
        if not test or _valid_impact_entry(test, entry, root) is None:
            stale += 1
        else:
            usable += 1
    return {"entries": len(tests), "usable_entries": usable, "stale_entries": stale}


def _impact_index_matches(test: TestFile, changed: ChangedPath, impact_index: dict[str, Any], root: Path = ROOT) -> tuple[int, list[str]]:
    if not changed.is_code_like or not changed.path.endswith(".py"):
        return 0, []
    tests = impact_index.get("tests", {})
    if not isinstance(tests, dict):
        return 0, []
    touched: set[str] = set()
    for nodeid in test.nodeids:
        entry = _valid_impact_entry(test, tests.get(nodeid), root)
        if entry:
            touched.update(str(path).replace("\\", "/") for path in entry.get("touched_files", []))
    if changed.path in touched:
        return 90, ["coverage impact index"]
    return 0, []


def score_test_for_change(
    test: TestFile,
    changed: ChangedPath,
    impact_index: dict[str, Any] | None = None,
    root: Path = ROOT,
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    test_path_lower = test.path.lower()

    if not changed.is_code_like and not changed.is_test:
        return 0, []

    if changed.is_test and changed.path == test.path:
        score += 100
        reasons.append("changed test file")

    impact_score, impact_reasons = _impact_index_matches(test, changed, impact_index or {}, root)
    if impact_score:
        score += impact_score
        reasons.extend(impact_reasons)

    for module in changed.modules:
        for imported in test.imports:
            if _module_related(module, imported):
                delta = 80 if module == imported else 60
                score += delta
                reasons.append(f"imports {imported}")
                break

    string_blob = "\n".join(test.strings)
    patch_blob = "\n".join(test.patch_strings)
    for module in changed.modules:
        if module and module in patch_blob:
            score += 80
            reasons.append(f"references {module}")
            break

    allow_token_match = not changed.modules
    if allow_token_match:
        for token in changed.tokens:
            if token in test_path_lower:
                score += 35
                reasons.append(f"path token {token}")
                break
            if any(token in imported.lower() for imported in test.imports):
                score += 25
                reasons.append(f"import token {token}")
                break
            if token in string_blob.lower():
                score += 25
                reasons.append(f"string token {token}")
                break

    if changed.path in patch_blob:
        score += 50
        reasons.append("references changed path")

    unique_reasons = list(dict.fromkeys(reasons))
    return score, unique_reasons


def select_tests(
    changed_paths: list[str],
    root: Path = ROOT,
    max_tests: int = 12,
    impact_index_path: str | Path = DEFAULT_IMPACT_INDEX,
) -> dict[str, Any]:
    changes = [describe_changed_path(path) for path in changed_paths]
    index = build_test_index(root)
    impact_index = load_impact_index(root, impact_index_path)
    selections_by_path: dict[str, TestSelection] = {}

    for test in index:
        total_score = 0
        reasons: list[str] = []
        for changed in changes:
            score, score_reasons = score_test_for_change(test, changed, impact_index, root)
            if score:
                total_score += score
                reasons.extend(f"{changed.path}: {reason}" for reason in score_reasons)
        if total_score:
            selections_by_path[test.path] = TestSelection(
                path=test.path,
                score=total_score,
                reasons=list(dict.fromkeys(reasons)),
                nodeids=test.nodeids,
            )

    selections = sorted(selections_by_path.values(), key=lambda item: (-item.score, item.path))
    limited = selections[:max_tests]
    best_score = limited[0].score if limited else 0
    code_like = any(change.is_code_like for change in changes)
    doc_only = bool(changes) and all(change.is_doc_like and not change.is_code_like for change in changes)
    if best_score >= 80:
        confidence = "high"
    elif best_score >= 45:
        confidence = "medium"
    elif limited:
        confidence = "low"
    elif doc_only:
        confidence = "none"
    else:
        confidence = "low" if code_like else "none"

    selected_paths = [selection.path for selection in limited]
    suggested_command = None
    if selected_paths:
        suggested_command = ".\\.python312\\python.exe -m pytest -q " + " ".join(selected_paths)

    impact_stats = impact_index_stats(index, impact_index, root) if impact_index else {"entries": 0, "usable_entries": 0, "stale_entries": 0}

    return {
        "schema": "knowledgeradar-test-selector/v1",
        "changed_paths": [change.path for change in changes],
        "selected_tests": [
            {
                "path": selection.path,
                "score": selection.score,
                "reasons": selection.reasons[:8],
                "nodeids": selection.nodeids,
            }
            for selection in limited
        ],
        "selected_count": len(limited),
        "total_candidate_count": len(selections),
        "confidence": confidence,
        "fallback_required": bool(code_like and confidence in {"low", "none"}),
        "doc_only": doc_only,
        "suggested_command": suggested_command,
        "index": {
            "test_files": len(index),
            "test_nodeids": sum(len(test.nodeids) for test in index),
            "impact_index_loaded": bool(impact_index),
            "impact_index_path": normalize_path(impact_index_path),
            "impact_index_entries": impact_stats["entries"],
            "impact_index_usable_entries": impact_stats["usable_entries"],
            "impact_index_stale_entries": impact_stats["stale_entries"],
        },
    }


def format_human(result: dict[str, Any]) -> str:
    lines = [
        f"schema: {result['schema']}",
        f"confidence: {result['confidence']}",
        f"changed paths: {len(result['changed_paths'])}",
        f"selected tests: {result['selected_count']} / {result['total_candidate_count']} candidates",
    ]
    if result.get("suggested_command"):
        lines.append(f"command: {result['suggested_command']}")
    elif result.get("doc_only"):
        lines.append("command: no targeted pytest inferred for documentation-only changes")
    else:
        lines.append("command: no targeted pytest inferred; inspect manually or use the normal changed gate")
    for selection in result["selected_tests"]:
        reason = "; ".join(selection["reasons"][:3])
        lines.append(f"- {selection['path']} score={selection['score']} reason={reason}")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changed", action="store_true", help="Use git changed and untracked paths.")
    parser.add_argument("--paths", nargs="*", default=None, help="Explicit changed paths to analyze.")
    parser.add_argument("--max-tests", type=int, default=12, help="Maximum test files to return.")
    parser.add_argument("--impact-index", default=str(DEFAULT_IMPACT_INDEX), help="Optional test impact index JSON path.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.paths is not None:
        changed_paths = args.paths
    elif args.changed:
        changed_paths = discover_changed_paths(ROOT)
    else:
        print("Provide --changed or --paths.", file=sys.stderr)
        return 2
    result = select_tests(changed_paths, ROOT, max_tests=args.max_tests, impact_index_path=args.impact_index)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_human(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
