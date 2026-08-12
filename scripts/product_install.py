"""Install, activate, roll back, or inspect a local KnowledgeRadar product copy.

This command is intentionally local-only.  It never uploads, prints, or
overwrites user configuration.  Source-checkout installation remains in
``install.bat``; this is the public package lifecycle entry point.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import tomllib
from typing import Any
from uuid import uuid4


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_NAME = "knowledgeradar-research"
ACTIVE_SCHEMA = "knowledgeradar-active-install/v1"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def path_hash(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:24]


def default_install_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "KnowledgeRadar"


def version_for(package_root: Path) -> str:
    with (package_root / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def safe_child(root: Path, path: Path) -> Path:
    root = root.resolve()
    path = path.resolve()
    if path != root and root not in path.parents:
        raise ValueError(f"path escapes install root: {path}")
    return path


def load_active(install_root: Path) -> dict[str, Any]:
    path = install_root / "active.json"
    if not path.is_file():
        raise RuntimeError("no active KnowledgeRadar installation")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != ACTIVE_SCHEMA:
        raise RuntimeError("unsupported active-install record")
    program_root = safe_child(install_root / "app", Path(str(payload.get("program_root") or "")))
    if not (program_root / "src" / "server.py").is_file():
        raise RuntimeError("active program is incomplete")
    return payload


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def replace_mcp_block(text: str, block: str) -> str:
    headings = {"[mcp_servers.knowledgeradar]", "[mcp_servers.knowledgeradar.env]"}
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    inserted = False
    index = 0
    while index < len(lines):
        if lines[index].strip() not in headings:
            output.append(lines[index])
            index += 1
            continue
        if not inserted:
            output.append(block + "\n")
            inserted = True
        index += 1
        while index < len(lines) and not (lines[index].strip().startswith("[") and lines[index].strip().endswith("]")):
            index += 1
    return "".join(output) if inserted else (text.rstrip() + "\n\n" + block + "\n")


def active_mcp_block(active: dict[str, Any], python_exe: Path) -> str:
    program = Path(str(active["program_root"])).resolve()
    data = Path(str(active["data_root"])).resolve()
    fields = {
        "PYTHONPATH": str(program / "src"),
        "KR_MCP_TRANSPORT": "stdio",
        "KR_PROJECT_ROOT": str(program),
        "KR_SOURCE_ROOT": str(program / "src"),
        "KR_DATA_ROOT": str(data),
        "KR_RUNTIME_ENV_PATH": str(data / "config" / "runtime.env"),
        "KR_PROFILE_REGISTRY_PATH": str(data / "config" / "profile_registry.json"),
        "KR_BROWSER_DATA_DIR": str(data / "browser_data"),
        "KR_STATE_DIR": str(data / "state"),
        "KR_LOG_DIR": str(data / "logs"),
        "PLAYWRIGHT_BROWSERS_PATH": str(data / "playwright"),
    }
    lines = [
        "[mcp_servers.knowledgeradar]",
        f"command = {json.dumps(str(python_exe), ensure_ascii=False)}",
        f"args = [\"-X\", \"utf8\", {json.dumps(str(program / 'src' / 'server.py'), ensure_ascii=False)}]",
        f"cwd = {json.dumps(str(program), ensure_ascii=False)}",
        "startup_timeout_sec = 30",
        "enabled = true",
        "",
        "[mcp_servers.knowledgeradar.env]",
    ]
    lines.extend(f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in fields.items())
    return "\n".join(lines)


def copy_plugin(program_root: Path, codex_home: Path) -> dict[str, str]:
    source = program_root / "config" / "codex-product" / "plugin" / PLUGIN_NAME
    destination = codex_home / "plugins" / PLUGIN_NAME
    if not (source / ".codex-plugin" / "plugin.json").is_file():
        raise RuntimeError("product plugin manifest is missing")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, dirs_exist_ok=True)
    skill = destination / "skills" / "research" / "SKILL.md"
    manifest = destination / ".codex-plugin" / "plugin.json"
    payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
    payload["version"] = f"{str(payload.get('version') or '0.0.0').split('+', 1)[0]}+codex.{sha256_file(skill)[:12]}"
    manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    marketplace = codex_home / ".agents" / "plugins" / "marketplace.json"
    existing = json.loads(marketplace.read_text(encoding="utf-8-sig")) if marketplace.is_file() else {"name": "personal", "interface": {"displayName": "Personal"}, "plugins": []}
    rows = [row for row in existing.get("plugins", []) if not isinstance(row, dict) or row.get("name") != PLUGIN_NAME]
    rows.append({"name": PLUGIN_NAME, "source": {"source": "local", "path": f"./plugins/{PLUGIN_NAME}"}, "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}, "category": "Productivity"})
    existing["plugins"] = rows
    write_json_atomic(marketplace, existing)
    return {"plugin_version": str(payload["version"]), "plugin_skill_sha256": sha256_file(skill), "plugin_manifest_sha256": sha256_file(manifest)}


def initialize_data(data_root: Path, package_root: Path) -> None:
    for relative in ("config", "browser_data", "state", "logs", "cache", "media", "models", "playwright", "receipts"):
        (data_root / relative).mkdir(parents=True, exist_ok=True)
    runtime_env = data_root / "config" / "runtime.env"
    if not runtime_env.exists():
        shutil.copyfile(package_root / ".env.example", runtime_env)
    registry = data_root / "config" / "profile_registry.json"
    template = package_root / "config" / "profile_registry.example.json"
    if not registry.exists() and template.is_file():
        shutil.copyfile(template, registry)


def tree_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "files": 0, "bytes": 0, "path_hash": path_hash(path)}
    files = [item for item in path.rglob("*") if item.is_file()] if path.is_dir() else [path]
    return {"exists": True, "files": len(files), "bytes": sum(item.stat().st_size for item in files), "path_hash": path_hash(path)}


def migration_plan(legacy_root: Path, data_root: Path) -> dict[str, Any]:
    legacy_root = legacy_root.resolve()
    data_root = data_root.resolve()
    if legacy_root == data_root or legacy_root in data_root.parents:
        raise RuntimeError("legacy source must stay outside the product data root")
    mappings = [
        (legacy_root / ".env", data_root / "config" / "runtime.env", "runtime_config"),
        (legacy_root / "config" / "profile_registry.json", data_root / "config" / "profile_registry.json", "profile_registry"),
        (legacy_root / "browser_data", data_root / "browser_data", "browser_data"),
        (legacy_root / "local", data_root / "local", "local_profiles"),
        (legacy_root / "runtime", data_root / "state" / "legacy-runtime", "runtime_state"),
    ]
    return {
        "schema": "knowledgeradar-legacy-migration-plan/v1",
        "legacy_root_hash": path_hash(legacy_root),
        "data_root_hash": path_hash(data_root),
        "mappings": [
            {"category": category, "source": tree_summary(source), "destination": tree_summary(destination)}
            for source, destination, category in mappings
        ],
        "copy_only": True,
        "does_not_modify_legacy_source": True,
        "conflicts_require_manual_review": True,
    }


def copy_without_overwrite(source: Path, destination: Path) -> tuple[int, int, list[str]]:
    if not source.exists():
        return 0, 0, []
    copied = 0
    skipped = 0
    conflicts: list[str] = []
    sources = [source] if source.is_file() else [item for item in source.rglob("*") if item.is_file()]
    for item in sources:
        relative = Path(item.name) if source.is_file() else item.relative_to(source)
        target = destination if source.is_file() else destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            skipped += 1
            if sha256_file(item) != sha256_file(target):
                conflicts.append(relative.as_posix())
            continue
        shutil.copy2(item, target)
        copied += 1
    return copied, skipped, conflicts


def migrate_apply(legacy_root: Path, data_root: Path) -> dict[str, Any]:
    plan = migration_plan(legacy_root, data_root)
    legacy_root = legacy_root.resolve()
    data_root = data_root.resolve()
    migration_id = uuid4().hex
    receipt: dict[str, Any] = {"schema": "knowledgeradar-legacy-migration-receipt/v1", "migration_id": migration_id, "status": "APPLIED", "plan": plan, "copied": []}
    mapping_paths = [
        (legacy_root / ".env", data_root / "config" / "runtime.env", "runtime_config"),
        (legacy_root / "config" / "profile_registry.json", data_root / "config" / "profile_registry.json", "profile_registry"),
        (legacy_root / "browser_data", data_root / "browser_data", "browser_data"),
        (legacy_root / "local", data_root / "local", "local_profiles"),
        (legacy_root / "runtime", data_root / "state" / "legacy-runtime", "runtime_state"),
    ]
    for source, destination, category in mapping_paths:
        copied, skipped, conflicts = copy_without_overwrite(source, destination)
        receipt["copied"].append({"category": category, "copied_files": copied, "skipped_files": skipped, "conflict_relative_paths": conflicts[:50]})
    receipt["status"] = "NEEDS_REVIEW" if any(item["conflict_relative_paths"] for item in receipt["copied"]) else "APPLIED"
    write_json_atomic(data_root / "receipts" / f"migration-{migration_id}.json", receipt)
    return receipt


def build_plan(package_root: Path, install_root: Path, data_root: Path, python_exe: Path) -> dict[str, Any]:
    version = version_for(package_root)
    program_root = install_root / "app" / version
    total_bytes = sum(path.stat().st_size for path in package_root.rglob("*") if path.is_file())
    return {
        "schema": "knowledgeradar-product-install-plan/v1",
        "package_root": str(package_root.resolve()),
        "version": version,
        "program_root": str(program_root.resolve()),
        "data_root": str(data_root.resolve()),
        "python": str(python_exe.resolve()),
        "package_bytes": total_bytes,
        "required_free_bytes": total_bytes * 2,
        "will_preserve_existing_data": True,
        "will_not_touch_development_source": True,
        "will_not_activate_candidate": False,
    }


def verify_product_package(package_root: Path) -> dict[str, Any]:
    if (package_root / ".git").exists():
        raise RuntimeError("product installer refuses a source checkout; use a generated product package")
    provenance_path = package_root / "package-provenance.json"
    if not provenance_path.is_file():
        raise RuntimeError("product package provenance is missing")
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    if provenance.get("source_dirty") is not False or not str(provenance.get("source_commit") or ""):
        raise RuntimeError("product package is not bound to a clean source commit")
    if "source_root" in provenance:
        raise RuntimeError("product package provenance exposes a source path")
    return provenance


def verify_artifact_receipt(package_root: Path, archive: Path, receipt_path: Path) -> dict[str, Any]:
    if not archive.is_file() or not receipt_path.is_file():
        raise RuntimeError("apply requires the downloaded archive and its candidate/release receipt")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("archive_sha256") != sha256_file(archive):
        raise RuntimeError("archive SHA-256 does not match the supplied receipt")
    provenance = verify_product_package(package_root)
    if receipt.get("source_commit") != provenance.get("source_commit"):
        raise RuntimeError("receipt and extracted package source identity do not match")
    return receipt


def apply_install(package_root: Path, install_root: Path, data_root: Path, python_exe: Path, *, channel: str, archive: Path | None = None, receipt_path: Path | None = None) -> dict[str, Any]:
    plan = build_plan(package_root, install_root, data_root, python_exe)
    provenance = verify_product_package(package_root)
    if archive is None or receipt_path is None:
        raise RuntimeError("apply requires --archive and --receipt; use plan to inspect without changes")
    artifact_receipt = verify_artifact_receipt(package_root, archive, receipt_path)
    if not (package_root / "src" / "server.py").is_file():
        raise RuntimeError("package root is missing src/server.py")
    if not python_exe.is_file():
        raise RuntimeError("selected Python executable does not exist")
    install_root = install_root.resolve()
    data_root = data_root.resolve()
    app_root = install_root / "app"
    if data_root.resolve() == app_root.resolve() or app_root.resolve() in data_root.resolve().parents:
        raise RuntimeError("data root must not be inside the immutable app directory")
    program_root = Path(plan["program_root"])
    app_root.mkdir(parents=True, exist_ok=True)
    initialize_data(data_root, package_root)
    if not program_root.exists():
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=app_root))
        try:
            shutil.copytree(package_root, staging / "payload", dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git", ".codex", "dist", "release", "__pycache__", "*.pyc", "*.egg-info"))
            (staging / "payload").replace(program_root)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    active = {
        "schema": ACTIVE_SCHEMA,
        "channel": channel,
        "version": plan["version"],
        "program_root": str(program_root),
        "data_root": str(data_root),
        "program_root_hash": path_hash(program_root),
        "data_root_hash": path_hash(data_root),
        "archive_sha256": str(artifact_receipt["archive_sha256"]),
        "source_commit": provenance["source_commit"],
    }
    active_path = install_root / "active.json"
    if active_path.is_file():
        backup_dir = install_root / "backup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(active_path, backup_dir / "active.previous.json")
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve()
    config = codex_home / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    backup = data_root / "receipts" / "codex-config.before.toml"
    if config.is_file():
        shutil.copyfile(config, backup)
    plugin = copy_plugin(program_root, codex_home)
    config.write_text(replace_mcp_block(config.read_text(encoding="utf-8") if config.is_file() else "", active_mcp_block(active, python_exe)), encoding="utf-8")
    write_json_atomic(active_path, active)
    receipt = {"schema": "knowledgeradar-activation-receipt/v1", "status": "APPLIED", "active": active, "plugin": plugin}
    write_json_atomic(data_root / "receipts" / "activation.json", receipt)
    return receipt


def rollback(install_root: Path, python_exe: Path) -> dict[str, Any]:
    backup = install_root / "backup" / "active.previous.json"
    if not backup.is_file():
        raise RuntimeError("no previous active installation is available")
    previous = json.loads(backup.read_text(encoding="utf-8"))
    safe_child(install_root / "app", Path(str(previous.get("program_root") or "")))
    write_json_atomic(install_root / "active.json", previous)
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve()
    config = codex_home / "config.toml"
    config.write_text(replace_mcp_block(config.read_text(encoding="utf-8") if config.is_file() else "", active_mcp_block(previous, python_exe)), encoding="utf-8")
    return {"status": "ROLLED_BACK", "active": previous}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage a local KnowledgeRadar product installation.")
    parser.add_argument("command", choices=("plan", "apply", "status", "rollback", "migrate-plan", "migrate-apply"))
    parser.add_argument("--package-root", default=str(PACKAGE_ROOT))
    parser.add_argument("--install-root", default=str(default_install_root()))
    parser.add_argument("--data-root", default="")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--channel", choices=("stable", "maintainer-main"), default="stable")
    parser.add_argument("--archive", default="", help="downloaded candidate/release ZIP")
    parser.add_argument("--receipt", default="", help="candidate/release receipt JSON matching --archive")
    parser.add_argument("--legacy-root", default="", help="existing private development/runtime root; read only for migration")
    args = parser.parse_args(argv)
    try:
        package_root = Path(args.package_root).resolve()
        install_root = Path(args.install_root).resolve()
        data_root = Path(args.data_root).resolve() if args.data_root else install_root / "data"
        python_exe = Path(args.python).resolve()
        if args.command == "migrate-plan":
            if not args.legacy_root:
                raise RuntimeError("migrate-plan requires --legacy-root")
            result = migration_plan(Path(args.legacy_root), data_root)
        elif args.command == "migrate-apply":
            if not args.legacy_root:
                raise RuntimeError("migrate-apply requires --legacy-root")
            result = migrate_apply(Path(args.legacy_root), data_root)
        elif args.command == "plan":
            result = build_plan(package_root, install_root, data_root, python_exe)
        elif args.command == "apply":
            result = apply_install(package_root, install_root, data_root, python_exe, channel=args.channel, archive=Path(args.archive).resolve() if args.archive else None, receipt_path=Path(args.receipt).resolve() if args.receipt else None)
        elif args.command == "status":
            result = {"status": "ACTIVE", "active": load_active(install_root)}
        else:
            result = rollback(install_root, python_exe)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        result = {"status": "FAIL", "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") not in {"FAIL"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
