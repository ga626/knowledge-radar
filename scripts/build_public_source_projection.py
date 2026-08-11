"""Build a GitHub-ready public-source projection from tracked allowlisted files."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "config" / "public-source-manifest.json"
DEFAULT_OUTPUT = ROOT / "dist" / "public-source" / "KnowledgeRadar"


def _matches(pattern: str, relative: str) -> bool:
    return fnmatch.fnmatch(relative.replace("\\", "/"), pattern.replace("\\", "/"))


def _safe_output(path: Path) -> Path:
    resolved = path.resolve()
    allowed = (ROOT / "dist").resolve()
    if resolved != allowed and allowed not in resolved.parents:
        raise ValueError(f"Output must stay under dist/: {resolved}")
    return resolved


def _tracked_files() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    )
    return {item.decode("utf-8") for item in result.stdout.split(b"\0") if item}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _commit() -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def _collect(manifest: dict, tracked: set[str]) -> list[str]:
    includes: Iterable[str] = manifest.get("include", [])
    excludes: Iterable[str] = manifest.get("exclude", [])
    selected = [
        relative for relative in sorted(tracked)
        if any(_matches(pattern, relative) for pattern in includes)
        and not any(_matches(pattern, relative) for pattern in excludes)
    ]
    if not selected:
        raise ValueError("Public-source manifest selected no tracked files")
    return selected


def build(manifest_path: Path, output: Path, *, dry_run: bool) -> int:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    output = _safe_output(output)
    files = _collect(manifest, _tracked_files())
    print(f"Manifest: {manifest_path.relative_to(ROOT).as_posix()}")
    print(f"Output: {output}")
    print(f"Tracked public files: {len(files)}")
    if dry_run:
        return 0
    if output.exists():
        shutil.rmtree(output)
    records = []
    for relative in files:
        source = ROOT / relative
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        records.append({"path": relative, "sha256": _sha256(target), "size": target.stat().st_size})
    receipt = {
        "schema": "knowledgeradar-public-source-projection/v1",
        "source_commit": _commit(),
        "manifest_path": manifest_path.relative_to(ROOT).as_posix(),
        "manifest_sha256": _sha256(manifest_path),
        "file_count": len(records),
        "files": records,
    }
    (output / "PUBLIC_SOURCE_MANIFEST.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the tracked, public KnowledgeRadar source projection.")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return build(Path(args.manifest).resolve(), Path(args.output), dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
