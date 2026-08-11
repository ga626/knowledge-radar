from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "config" / "package-manifest.product-lite.json"
DEFAULT_OUTPUT = ROOT / "dist" / "product-lite" / "KnowledgeRadar"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def normalize(path: Path) -> str:
    return path.as_posix().lstrip("./")


def matches(pattern: str, relative: str) -> bool:
    return fnmatch.fnmatch(relative.replace("\\", "/"), pattern.replace("\\", "/"))


def is_excluded(relative: str, patterns: Iterable[str]) -> bool:
    return any(matches(pattern, relative) for pattern in patterns)


def ensure_safe_output(path: Path) -> Path:
    resolved = path.resolve()
    root = ROOT.resolve()
    allowed_roots = [(ROOT / "dist").resolve(), (ROOT / "release").resolve()]
    if not any(resolved == allowed or allowed in resolved.parents for allowed in allowed_roots):
        raise ValueError(f"Output must stay under dist/ or release/: {resolved}")
    if resolved == root or root not in resolved.parents:
        raise ValueError(f"Output must stay inside project root: {resolved}")
    return resolved


def expand_include(pattern: str) -> List[Path]:
    base = ROOT / pattern
    if any(ch in pattern for ch in "*?[]"):
        return [path for path in ROOT.glob(pattern) if path.is_file()]
    if base.is_file():
        return [base]
    if base.is_dir():
        return [path for path in base.rglob("*") if path.is_file()]
    return []


def collect_files(manifest: dict) -> Tuple[List[Path], List[str]]:
    exclude = manifest.get("exclude", [])
    files: Dict[str, Path] = {}
    missing: List[str] = []
    for pattern in manifest.get("include", []):
        matched = expand_include(pattern)
        if not matched and not any(ch in pattern for ch in "*?[]"):
            missing.append(pattern)
        for path in matched:
            relative = normalize(path.relative_to(ROOT))
            if is_excluded(relative, exclude):
                continue
            files[relative] = path
    return [files[key] for key in sorted(files)], missing


def git_value(args: List[str]) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def copy_files(files: Iterable[Path], output: Path) -> List[dict]:
    copied = []
    for source in files:
        relative = source.relative_to(ROOT)
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(
            {
                "path": normalize(relative),
                "sha256": sha256_file(target),
                "size": target.stat().st_size,
            }
        )
    return copied


def write_provenance(
    output: Path,
    manifest_path: Path,
    manifest: dict,
    file_records: List[dict],
    missing_includes: List[str],
) -> None:
    builder = Path(__file__).resolve()
    commit = git_value(["rev-parse", "HEAD"])
    dirty = bool(git_value(["status", "--short"]))
    provenance = {
        "schema": "knowledgeradar-package-provenance/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_commit": commit,
        "source_dirty": dirty,
        "manifest_path": normalize(manifest_path.relative_to(ROOT)),
        "manifest_sha256": sha256_file(manifest_path),
        "builder_path": normalize(builder.relative_to(ROOT)),
        "builder_sha256": sha256_file(builder),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "include": manifest.get("include", []),
        "exclude": manifest.get("exclude", []),
        "missing_includes": missing_includes,
        "file_count": len(file_records) + 2,
        "files": file_records,
    }
    (output / "package-provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    generated_from = "\n".join(
        [
            "# Generated Package",
            "",
            "This directory is generated from the KnowledgeRadar development source.",
            "Do not edit files here directly. Change source files or manifests, then rebuild.",
            "",
            f"- Source commit: `{commit or 'unknown'}`",
            f"- Source dirty: `{dirty}`",
            f"- Manifest: `{normalize(manifest_path.relative_to(ROOT))}`",
            f"- Manifest sha256: `{provenance['manifest_sha256']}`",
            f"- Builder: `{normalize(builder.relative_to(ROOT))}`",
            f"- Builder sha256: `{provenance['builder_sha256']}`",
            f"- Generated at: `{provenance['generated_at']}`",
            "",
        ]
    )
    (output / "GENERATED_FROM.md").write_text(generated_from, encoding="utf-8")


def build(manifest_path: Path, output: Path, dry_run: bool = False) -> int:
    manifest_path = manifest_path.resolve()
    output = ensure_safe_output(output)
    manifest = load_json(manifest_path)
    files, missing = collect_files(manifest)
    print(f"Manifest: {manifest_path}")
    print(f"Output: {output}")
    print(f"Files to copy: {len(files)}")
    if missing:
        print("Missing explicit include entries:")
        for item in missing:
            print(f"- {item}")
    if dry_run:
        return 0
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    records = copy_files(files, output)
    write_provenance(output, manifest_path, manifest, records, missing)
    print("Package generated.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the KnowledgeRadar product-lite package.")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return build(Path(args.manifest), Path(args.output), dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
