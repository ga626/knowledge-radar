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
import subprocess
import sys
import tempfile
import time
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


def runtime_python(runtime_root: Path) -> Path:
    return runtime_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def require_python_312(python_exe: Path) -> None:
    result = subprocess.run(
        [str(python_exe), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if result.returncode or result.stdout.strip() != "3.12":
        raise RuntimeError("KnowledgeRadar requires an available Python 3.12 interpreter")


def run_runtime_check(command: list[str], *, cwd: Path, action: str, env: dict[str, str] | None = None) -> None:
    result = subprocess.run(command, cwd=cwd, env=env, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        suffix = f": {detail[-1][:240]}" if detail else ""
        raise RuntimeError(f"runtime bootstrap failed while {action}{suffix}")


def ensure_runtime(program_root: Path, install_root: Path, version: str, base_python: Path) -> Path:
    require_python_312(base_python)
    runtime_root = install_root / "runtime" / version
    executable = runtime_python(runtime_root)
    runtime_env = dict(os.environ)
    runtime_env["PYTHONPATH"] = str(program_root / "src")
    if executable.is_file():
        run_runtime_check([str(executable), "-c", "import mcp; import server"], cwd=program_root, action="checking the existing runtime", env=runtime_env)
        return executable
    runtime_parent = runtime_root.parent
    runtime_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".runtime-staging-", dir=runtime_parent))
    try:
        run_runtime_check([str(base_python), "-m", "venv", str(staging)], cwd=program_root, action="creating the product runtime")
        staged_python = runtime_python(staging)
        run_runtime_check(
            [str(staged_python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--prefer-binary", str(program_root)],
            cwd=program_root,
            action="installing product dependencies",
        )
        run_runtime_check([str(staged_python), "-c", "import mcp; import server"], cwd=program_root, action="checking the product runtime", env=runtime_env)
        if runtime_root.exists():
            raise RuntimeError("existing product runtime is incomplete; remove it explicitly before retrying")
        staging.replace(runtime_root)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return executable


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
        "KR_MEDIA_CACHE_DIR": str(data / "state" / "media_cache"),
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


def write_product_wizard_launcher(active: dict[str, Any], python_exe: Path) -> Path:
    """Write a version-neutral Release setup entry point beside ``active.json``.

    The helper belongs to the installation root, rather than an app version, so
    a rollback can still configure a pre-wizard product version.  It resolves
    the active program and data roots at launch time; no user value is embedded
    in either generated file.
    """
    program = Path(str(active["program_root"])).resolve()
    install_root = program.parents[1]
    helper = install_root / "configure_product.py"
    helper.write_text(
        "from __future__ import annotations\n"
        "import argparse\n"
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "import sys\n\n"
        "ROOT = Path(__file__).resolve().parent\n\n"
        "def main() -> int:\n"
        "    parser = argparse.ArgumentParser(description='Start the active local KnowledgeRadar setup wizard.')\n"
        "    parser.add_argument('--port', type=int, default=0)\n"
        "    parser.add_argument('--no-open', action='store_true')\n"
        "    args = parser.parse_args()\n"
        "    if not 0 <= args.port <= 65535:\n"
        "        parser.error('--port must be between 0 and 65535')\n"
        "    active = json.loads((ROOT / 'active.json').read_text(encoding='utf-8'))\n"
        "    program = Path(str(active.get('program_root') or '')).resolve()\n"
        "    data = Path(str(active.get('data_root') or '')).resolve()\n"
        "    if active.get('schema') != 'knowledgeradar-active-install/v1' or not (program / 'src' / 'onboarding' / 'setup_wizard.py').is_file() or not data.is_dir():\n"
        "        raise RuntimeError('active KnowledgeRadar installation is unavailable')\n"
        "    os.environ.update({'KR_PROJECT_ROOT': str(program), 'KR_SOURCE_ROOT': str(program / 'src'), 'KR_DATA_ROOT': str(data), 'KR_RUNTIME_ENV_PATH': str(data / 'config' / 'runtime.env'), 'KR_PROFILE_REGISTRY_PATH': str(data / 'config' / 'profile_registry.json'), 'KR_BROWSER_DATA_DIR': str(data / 'browser_data'), 'KR_STATE_DIR': str(data / 'state'), 'KR_LOG_DIR': str(data / 'logs'), 'KR_MEDIA_CACHE_DIR': str(data / 'state' / 'media_cache')})\n"
        "    sys.path.insert(0, str(program / 'src'))\n"
        "    from onboarding.setup_wizard import run_wizard\n"
        "    run_wizard(port=args.port, open_browser=not args.no_open)\n"
        "    return 0\n\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n",
        encoding="utf-8",
    )
    launcher = install_root / "configure.cmd"
    launcher.write_text(
        "@echo off\r\n"
        "setlocal\r\n"
        "set \"PYTHONUTF8=1\"\r\n"
        "set \"PYTHONIOENCODING=utf-8\"\r\n"
        f"\"{python_exe}\" \"{helper}\" %*\r\n",
        encoding="utf-8",
    )
    return launcher


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


def _tree_manifest(path: Path) -> dict[str, str]:
    """Hash a migration tree without reading any file content into receipts."""
    if not path.is_dir():
        raise RuntimeError("data root is unavailable")
    rows: dict[str, str] = {}
    for item in sorted((entry for entry in path.rglob("*") if entry.is_file()), key=lambda entry: entry.as_posix()):
        relative = item.relative_to(path).as_posix()
        rows[relative] = f"{item.stat().st_size}:{sha256_file(item)}"
    return rows


def _migration_token(source: Path, target: Path, source_summary: dict[str, Any]) -> str:
    payload = json.dumps(
        {"source": str(source.resolve()), "target": str(target.resolve()), "files": source_summary["files"], "bytes": source_summary["bytes"]},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _active_runtime(install_root: Path, active: dict[str, Any]) -> Path:
    runtime = runtime_python(install_root / "runtime" / str(active.get("version") or ""))
    if not runtime.is_file():
        raise RuntimeError("active product runtime is unavailable")
    return runtime


def data_root_move_plan(install_root: Path, target_root: Path) -> dict[str, Any]:
    """Create a no-write plan to copy the active data root to another volume.

    The source is intentionally retained after activation.  That makes the
    operation recoverable and prevents an interrupted migration from deleting
    browser profiles, credentials, or user backups.
    """
    install_root = install_root.resolve()
    active = load_active(install_root)
    source = Path(str(active["data_root"])).resolve()
    target = target_root.resolve()
    if source == target or source in target.parents or target in source.parents:
        raise RuntimeError("new data root must be a distinct sibling location")
    if (install_root / "app") in (target, *target.parents):
        raise RuntimeError("data root must not be inside the immutable app directory")
    if target.exists() and any(target.iterdir()):
        raise RuntimeError("new data root must be empty; existing data is never merged automatically")
    source_summary = tree_summary(source)
    if not source_summary["exists"]:
        raise RuntimeError("active data root is unavailable")
    volume = shutil.disk_usage(target.parent if target.parent.exists() else target.anchor)
    locked = sorted(str(item.relative_to(source)) for item in source.rglob("Singleton*") if item.exists())
    return {
        "schema": "knowledgeradar-data-root-move-plan/v1",
        "status": "PLAN",
        "source_data_root_hash": path_hash(source),
        "target_data_root_hash": path_hash(target),
        "source": {"files": source_summary["files"], "bytes": source_summary["bytes"]},
        "target": {"exists": target.exists(), "free_bytes": volume.free},
        "required_free_bytes": source_summary["bytes"] + max(5 * 1024**3, source_summary["bytes"] // 5),
        "browser_lock_relative_paths": locked,
        "confirmation_token": _migration_token(source, target, source_summary),
        "copy_verify_switch": True,
        "keeps_source_for_rollback": True,
        "will_not_modify_development_source": True,
    }


def data_root_move_apply(install_root: Path, target_root: Path, confirmation: str) -> dict[str, Any]:
    plan = data_root_move_plan(install_root, target_root)
    if plan["browser_lock_relative_paths"]:
        raise RuntimeError("close managed browsers before migration; active profile locks were detected")
    if confirmation != plan["confirmation_token"]:
        raise RuntimeError("confirmation token does not match the current migration plan")
    if plan["target"]["free_bytes"] < plan["required_free_bytes"]:
        raise RuntimeError("target volume does not have the required free space")
    install_root = install_root.resolve()
    active = load_active(install_root)
    source = Path(str(active["data_root"])).resolve()
    target = target_root.resolve()
    staging_parent = target.parent
    staging_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".data-root-staging-", dir=staging_parent))
    started = int(time.time())
    try:
        copied = staging / "payload"
        shutil.copytree(source, copied, copy_function=shutil.copy2)
        if _tree_manifest(source) != _tree_manifest(copied):
            raise RuntimeError("copied data does not match source manifest")
        copied.replace(target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    previous = dict(active)
    active["data_root"] = str(target)
    active["data_root_hash"] = path_hash(target)
    active["previous_data_root"] = str(source)
    active["data_root_migration_token"] = plan["confirmation_token"]
    runtime = _active_runtime(install_root, active)
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve()
    config = codex_home / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    if config.is_file():
        shutil.copyfile(config, target / "receipts" / f"codex-config.before-data-move-{started}.toml")
    config.write_text(replace_mcp_block(config.read_text(encoding="utf-8") if config.is_file() else "", active_mcp_block(active, runtime)), encoding="utf-8")
    write_json_atomic(install_root / "backup" / "active.before-data-move.json", previous)
    write_json_atomic(install_root / "active.json", active)
    launcher = write_product_wizard_launcher(active, runtime)
    receipt = {
        "schema": "knowledgeradar-data-root-move-receipt/v1",
        "status": "APPLIED",
        "plan": plan,
        "active": active,
        "source_retained_for_rollback": True,
        "wizard_launcher": str(launcher),
    }
    write_json_atomic(target / "receipts" / f"data-root-move-{started}.json", receipt)
    return receipt


def data_root_move_rollback(install_root: Path) -> dict[str, Any]:
    install_root = install_root.resolve()
    backup = install_root / "backup" / "active.before-data-move.json"
    if not backup.is_file():
        raise RuntimeError("no data-root migration rollback record is available")
    previous = json.loads(backup.read_text(encoding="utf-8"))
    previous_root = Path(str(previous.get("data_root") or "")).resolve()
    if not previous_root.is_dir():
        raise RuntimeError("previous data root is unavailable")
    runtime = _active_runtime(install_root, previous)
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve()
    config = codex_home / "config.toml"
    config.write_text(replace_mcp_block(config.read_text(encoding="utf-8") if config.is_file() else "", active_mcp_block(previous, runtime)), encoding="utf-8")
    write_json_atomic(install_root / "active.json", previous)
    launcher = write_product_wizard_launcher(previous, runtime)
    return {"schema": "knowledgeradar-data-root-move-rollback/v1", "status": "ROLLED_BACK", "active": previous, "wizard_launcher": str(launcher)}


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


def env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip():
            values[key.strip()] = value.strip()
    return values


def merge_runtime_env(source: Path, destination: Path) -> tuple[int, int, list[str]]:
    source_values = env_values(source)
    if not source_values:
        return 0, 0, []
    existing = destination.read_text(encoding="utf-8-sig") if destination.is_file() else ""
    destination_values = env_values(destination)
    additions = {key: value for key, value in source_values.items() if value and not destination_values.get(key)}
    conflicts = sorted(key for key, value in source_values.items() if value and destination_values.get(key) and destination_values[key] != value)
    if additions:
        lines = existing.splitlines()
        for index, line in enumerate(lines):
            key = line.split("=", 1)[0].strip() if "=" in line else ""
            if key in additions:
                lines[index] = f"{key}={additions.pop(key)}"
        if additions:
            lines.extend(["", "# Migrated local configuration"])
            lines.extend(f"{key}={value}" for key, value in sorted(additions.items()))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return len([value for value in source_values.values() if value]), 0, conflicts


def migrate_profile_registry(source: Path, destination: Path) -> tuple[int, int, list[str]]:
    if not source.is_file():
        return 0, 0, []
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return 1, 0, []
    try:
        current = json.loads(destination.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return 0, 1, ["profile_registry.json"]
    if isinstance(current, dict) and current.get("version") == "example":
        shutil.copy2(source, destination)
        return 1, 0, []
    return 0, 1, [] if sha256_file(source) == sha256_file(destination) else ["profile_registry.json"]


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
        if category == "runtime_config":
            copied, skipped, conflicts = merge_runtime_env(source, destination)
        elif category == "profile_registry":
            copied, skipped, conflicts = migrate_profile_registry(source, destination)
        else:
            copied, skipped, conflicts = copy_without_overwrite(source, destination)
        receipt["copied"].append({"category": category, "copied_files": copied, "skipped_files": skipped, "conflict_relative_paths": conflicts[:50]})
    receipt["status"] = "NEEDS_REVIEW" if any(item["conflict_relative_paths"] for item in receipt["copied"]) else "APPLIED"
    write_json_atomic(data_root / "receipts" / f"migration-{migration_id}.json", receipt)
    return receipt


def build_plan(package_root: Path, install_root: Path, data_root: Path, python_exe: Path) -> dict[str, Any]:
    version = version_for(package_root)
    program_root = install_root / "app" / version
    runtime_root = install_root / "runtime" / version
    total_bytes = sum(path.stat().st_size for path in package_root.rglob("*") if path.is_file())
    return {
        "schema": "knowledgeradar-product-install-plan/v1",
        "package_root": str(package_root.resolve()),
        "version": version,
        "program_root": str(program_root.resolve()),
        "runtime_root": str(runtime_root.resolve()),
        "runtime_python": str(runtime_python(runtime_root).resolve()),
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
    if not program_root.exists():
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=app_root))
        try:
            shutil.copytree(package_root, staging / "payload", dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git", ".codex", "dist", "release", "__pycache__", "*.pyc", "*.egg-info"))
            (staging / "payload").replace(program_root)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    product_python = ensure_runtime(program_root, install_root, plan["version"], python_exe)
    initialize_data(data_root, package_root)
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
    config.write_text(replace_mcp_block(config.read_text(encoding="utf-8") if config.is_file() else "", active_mcp_block(active, product_python)), encoding="utf-8")
    write_json_atomic(active_path, active)
    wizard_launcher = write_product_wizard_launcher(active, product_python)
    receipt = {"schema": "knowledgeradar-activation-receipt/v1", "status": "APPLIED", "active": active, "plugin": plugin, "wizard_launcher": str(wizard_launcher)}
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
    previous_runtime = runtime_python(install_root / "runtime" / str(previous.get("version") or ""))
    if not previous_runtime.is_file():
        raise RuntimeError("previous product runtime is unavailable")
    config.write_text(replace_mcp_block(config.read_text(encoding="utf-8") if config.is_file() else "", active_mcp_block(previous, previous_runtime)), encoding="utf-8")
    wizard_launcher = write_product_wizard_launcher(previous, previous_runtime)
    return {"status": "ROLLED_BACK", "active": previous, "wizard_launcher": str(wizard_launcher)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage a local KnowledgeRadar product installation.")
    parser.add_argument(
        "command",
        choices=("plan", "apply", "status", "rollback", "migrate-plan", "migrate-apply", "data-move-plan", "data-move-apply", "data-move-rollback"),
    )
    parser.add_argument("--package-root", default=str(PACKAGE_ROOT))
    parser.add_argument("--install-root", default=str(default_install_root()))
    parser.add_argument("--data-root", default="")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--channel", choices=("stable", "maintainer-main"), default="stable")
    parser.add_argument("--archive", default="", help="downloaded candidate/release ZIP")
    parser.add_argument("--receipt", default="", help="candidate/release receipt JSON matching --archive")
    parser.add_argument("--legacy-root", default="", help="existing private development/runtime root; read only for migration")
    parser.add_argument("--confirmation", default="", help="confirmation token returned by data-move-plan")
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
        elif args.command == "data-move-plan":
            if not args.data_root:
                raise RuntimeError("data-move-plan requires --data-root as the new empty data root")
            result = data_root_move_plan(install_root, data_root)
        elif args.command == "data-move-apply":
            if not args.data_root or not args.confirmation:
                raise RuntimeError("data-move-apply requires --data-root and --confirmation")
            result = data_root_move_apply(install_root, data_root, args.confirmation)
        elif args.command == "data-move-rollback":
            result = data_root_move_rollback(install_root)
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
