from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import subprocess
import sys
import tomllib
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "config" / "package-manifest.product-lite.json"
DEFAULT_OUTPUT = ROOT / "dist" / "product-lite" / "KnowledgeRadar"
PACKAGE_ROOT = "KnowledgeRadar"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def normalize(path: Path) -> str:
    value = path.as_posix()
    return value[2:] if value.startswith("./") else value


def matches(pattern: str, relative: str) -> bool:
    return fnmatch.fnmatch(relative.replace("\\", "/"), pattern.replace("\\", "/"))


def is_excluded(relative: str, patterns: Iterable[str]) -> bool:
    return any(matches(pattern, relative) for pattern in patterns)


def ensure_safe_output(path: Path) -> Path:
    resolved = path.resolve()
    allowed_roots = [(ROOT / "dist").resolve(), (ROOT / "release").resolve()]
    if not any(resolved == allowed or allowed in resolved.parents for allowed in allowed_roots):
        raise ValueError(f"output must stay under dist/ or release/: {resolved}")
    return resolved


def git_value(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def tracked_paths() -> set[str]:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
    return {item.decode("utf-8") for item in result.stdout.split(b"\0") if item}


def expand_include(pattern: str) -> list[Path]:
    base = ROOT / pattern
    if pattern.endswith("/**"):
        directory = ROOT / pattern[:-3]
        return [path for path in directory.rglob("*") if path.is_file()] if directory.is_dir() else []
    if any(char in pattern for char in "*?[]"):
        return [path for path in ROOT.glob(pattern) if path.is_file()]
    if base.is_file():
        return [base]
    if base.is_dir():
        return [path for path in base.rglob("*") if path.is_file()]
    return []


def collect_files(manifest: dict) -> tuple[list[Path], list[str], list[str]]:
    files: dict[str, Path] = {}
    missing: list[str] = []
    untracked: list[str] = []
    tracked = tracked_paths()
    for pattern in manifest.get("include", []):
        matched = expand_include(pattern)
        if not matched and not any(char in pattern for char in "*?[]"):
            missing.append(pattern)
        for path in matched:
            relative = normalize(path.relative_to(ROOT))
            if is_excluded(relative, manifest.get("exclude", [])):
                continue
            if relative not in tracked:
                untracked.append(relative)
                continue
            files[relative] = path
    return [files[key] for key in sorted(files)], missing, sorted(set(untracked))


def source_identity(require_clean: bool) -> tuple[str, int]:
    commit = git_value("rev-parse", "HEAD")
    dirty = bool(git_value("status", "--porcelain", "--untracked-files=all"))
    if require_clean and dirty:
        raise RuntimeError("release candidate requires a clean public checkout")
    timestamp = int(git_value("show", "-s", "--format=%ct", "HEAD"))
    return commit, timestamp


def copy_files(files: Iterable[Path], output: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for source in files:
        relative = source.relative_to(ROOT)
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        records.append({"path": normalize(relative), "sha256": sha256_file(target), "size": target.stat().st_size})
    return records


def package_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def write_metadata(output: Path, manifest_path: Path, files: list[dict[str, object]], commit: str) -> None:
    manifest = load_json(manifest_path)
    provenance = {
        "schema": "knowledgeradar-package-provenance/v2",
        "source_commit": commit,
        "source_dirty": False,
        "manifest_path": normalize(manifest_path.relative_to(ROOT)),
        "manifest_sha256": sha256_file(manifest_path),
        "builder_path": "scripts/build_product_lite_package.py",
        "builder_sha256": sha256_file(Path(__file__)),
        "file_count": len(files),
        "files": files,
    }
    (output / "package-provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sbom = {
        "schema": "knowledgeradar-content-inventory/v1",
        "scope": "package file inventory; dependency licenses are declared by their upstream distributions",
        "package": "knowledgeradar",
        "version": package_version(),
        "files": files,
    }
    (output / "SBOM.json").write_text(json.dumps(sbom, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "GENERATED_FROM.md").write_text(
        "# Generated Package\n\n"
        "Generated only from tracked files in a clean public checkout.\n"
        f"\n- Source commit: `{commit}`\n- Manifest: `{provenance['manifest_path']}`\n",
        encoding="utf-8",
    )


def deterministic_zip(package_dir: Path, archive: Path, timestamp: int) -> None:
    instant = datetime.fromtimestamp(max(timestamp, 315532800), tz=UTC)
    date_time = (instant.year, instant.month, instant.day, instant.hour, instant.minute, instant.second)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for file_path in sorted(path for path in package_dir.rglob("*") if path.is_file()):
            item = zipfile.ZipInfo(f"{PACKAGE_ROOT}/{normalize(file_path.relative_to(package_dir))}", date_time=date_time)
            item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = 0o100644 << 16
            bundle.writestr(item, file_path.read_bytes())


def build(manifest_path: Path, output: Path, *, dry_run: bool, candidate: bool) -> int:
    manifest_path = manifest_path.resolve()
    output = ensure_safe_output(output)
    manifest = load_json(manifest_path)
    files, missing, untracked = collect_files(manifest)
    print(f"manifest: {normalize(manifest_path.relative_to(ROOT))}")
    print(f"files to copy: {len(files)}")
    if missing or untracked:
        for item in missing:
            print(f"missing explicit include: {item}")
        for item in untracked:
            print(f"untracked package input: {item}")
        return 1
    commit, timestamp = source_identity(require_clean=candidate)
    if dry_run:
        print("package plan valid")
        return 0
    if output.exists():
        import shutil
        shutil.rmtree(output)
    output.mkdir(parents=True)
    records = copy_files(files, output)
    write_metadata(output, manifest_path, records, commit)
    if not candidate:
        print(f"package generated: {output}")
        return 0
    candidate_dir = (ROOT / "release" / "candidates" / f"v{package_version()}-{commit}").resolve()
    candidate_dir.mkdir(parents=True, exist_ok=True)
    archive = candidate_dir / "KnowledgeRadar.zip"
    deterministic_zip(output, archive, timestamp)
    receipt = {
        "schema": "knowledgeradar-release-candidate-receipt/v1",
        "source_commit": commit,
        "source_dirty": False,
        "version": package_version(),
        "archive": archive.name,
        "archive_sha256": sha256_file(archive),
        "package_provenance_sha256": sha256_file(output / "package-provenance.json"),
        "sbom_sha256": sha256_file(output / "SBOM.json"),
        "package_file_count": len(records),
    }
    receipt_path = candidate_dir / "candidate-receipt.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"candidate archive: {archive}")
    print(f"candidate receipt: {receipt_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the redistributable KnowledgeRadar package.")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--candidate", action="store_true", help="require clean source and create immutable candidate ZIP plus receipt")
    args = parser.parse_args(argv)
    try:
        return build(Path(args.manifest), Path(args.output), dry_run=args.dry_run, candidate=args.candidate)
    except (OSError, KeyError, RuntimeError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        print(f"package build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
