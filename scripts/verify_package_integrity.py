from __future__ import annotations

import fnmatch
import json
import re
import argparse
from pathlib import Path
from typing import List


ROOT = Path(__file__).resolve().parents[1]
STRUCTURE_MANIFEST = ROOT / "config" / "project-structure.manifest.json"
MAX_TEXT_SCAN_BYTES = 2 * 1024 * 1024
CONTENT_PATTERNS = {
    "private_key": re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    "openai_style_key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "credential_assignment": re.compile(
        r"(?im)^[ \t]*(?:(?:[A-Z][A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD))|(?:[A-Z][A-Z0-9_]*API[_-]?KEY)|API[_-]?KEY|authorization)[ \t]*=[ \t]*(?![<\"']?(?:your|example|replace|change|paste|todo|xxx)[\w-]*)(?:[^\s#]{8,})"
    ),
    "absolute_user_path": re.compile(r"(?i)(?:[a-z]:\\users\\[^\\\s]+|/(?:users|home)/[^/\s]+)"),
    "provenance_source_root": re.compile(r'"source_root"\s*:'),
}


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def matches(pattern: str, relative: str) -> bool:
    return fnmatch.fnmatch(relative.replace("\\", "/"), pattern.replace("\\", "/"))


def find_package_dirs() -> List[Path]:
    candidates: List[Path] = []
    for base_name in ("dist", "release"):
        base = ROOT / base_name
        if not base.exists():
            continue
        for child in base.iterdir():
            if child.is_dir():
                if (child / "package-provenance.json").exists() or (child / "GENERATED_FROM.md").exists():
                    candidates.append(child)
                    continue
                for grandchild in child.iterdir():
                    if grandchild.is_dir() and (
                        (grandchild / "package-provenance.json").exists()
                        or (grandchild / "GENERATED_FROM.md").exists()
                    ):
                        candidates.append(grandchild)
    return candidates


def check_package(path: Path, forbidden_patterns: List[str]) -> List[str]:
    issues: List[str] = []
    for file_path in path.rglob("*"):
        if not file_path.is_file():
            continue
        relative = file_path.relative_to(path).as_posix()
        for pattern in forbidden_patterns:
            if matches(pattern, relative) or matches(pattern, file_path.relative_to(ROOT).as_posix()):
                issues.append(f"{path.name}: forbidden file in package: {relative} <= {pattern}")
    provenance = path / "package-provenance.json"
    generated_from = path / "GENERATED_FROM.md"
    public_source = path / "PUBLIC_SOURCE_MANIFEST.json"
    if not provenance.exists() and not generated_from.exists() and not public_source.exists():
        issues.append(f"{path.name}: missing package provenance marker")
    return issues


def _text_for_scan(path: Path) -> str | None:
    if path.stat().st_size > MAX_TEXT_SCAN_BYTES:
        return None
    try:
        payload = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in payload:
        return None
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return None


def check_package_content(path: Path) -> List[str]:
    """Detect common private-content shapes without ever echoing their values."""
    issues: List[str] = []
    for file_path in path.rglob("*"):
        if not file_path.is_file():
            continue
        text = _text_for_scan(file_path)
        if text is None:
            continue
        relative = file_path.relative_to(path).as_posix()
        for kind, pattern in CONTENT_PATTERNS.items():
            if kind == "credential_assignment" and not _is_config_text(file_path):
                continue
            if pattern.search(text):
                issues.append(f"{path.name}: private content pattern ({kind}) in {relative}")
    return issues


def _is_config_text(path: Path) -> bool:
    name = path.name.lower()
    return name.startswith(".env") or name.endswith((".env", ".ini", ".toml", ".yaml", ".yml"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify generated KnowledgeRadar package/public-source boundaries.")
    parser.add_argument("--path", action="append", default=[], help="Generated package or public-source directory to verify.")
    args = parser.parse_args()
    structure = load_json(STRUCTURE_MANIFEST)
    package_dirs = [Path(item).resolve() for item in args.path] or find_package_dirs()
    if not package_dirs:
        print("Package integrity check skipped: no generated package directory under dist/ or release/.")
        return 0
    issues: List[str] = []
    for package_dir in package_dirs:
        if not package_dir.is_dir():
            issues.append(f"missing verification directory: {package_dir}")
            continue
        issues.extend(check_package(package_dir, structure["forbidden_release_patterns"]))
        issues.extend(check_package_content(package_dir))
    if issues:
        print("Package integrity check failed.")
        for issue in issues:
            print(f"- {issue}")
        return 1
    print("Package integrity check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
