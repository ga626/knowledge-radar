"""Install the KnowledgeRadar Codex product contract from this checkout.

The project checkout is the source of truth.  The installer updates only the
declared local plugin source and records the exact Skill fingerprint.  Codex
itself owns its cache, so cache drift is surfaced as a host reload/install
requirement instead of being hidden by an unsafe cache write.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[1]
CODEX_PRODUCT = ROOT / "config" / "codex-product"
PLUGIN_SOURCE = CODEX_PRODUCT / "plugin" / "knowledgeradar-research"
ENTRYPOINT = ROOT / "src" / "server.py"
PLUGIN_SKILL_RELATIVE = Path("skills") / "research" / "SKILL.md"


def _utf8(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_utf8(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True
    )
    return result.stdout.strip()


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()


def _canonical_config(root: Path, python_exe: Path) -> str:
    source = root / "src" / "server.py"
    runtime = root / "runtime"
    return "\n".join(
        [
            "[mcp_servers.knowledgeradar]",
            f"command = {json.dumps(str(python_exe), ensure_ascii=False)}",
            f"args = [\"-X\", \"utf8\", {json.dumps(str(source), ensure_ascii=False)}]",
            f"cwd = {json.dumps(str(root), ensure_ascii=False)}",
            "startup_timeout_sec = 30",
            "enabled = true",
            "",
            "[mcp_servers.knowledgeradar.env]",
            f"PYTHONPATH = {json.dumps(str(root / 'src'), ensure_ascii=False)}",
            "KR_MCP_TRANSPORT = \"stdio\"",
            f"KR_PROJECT_ROOT = {json.dumps(str(root), ensure_ascii=False)}",
            f"KR_SOURCE_ROOT = {json.dumps(str(root / 'src'), ensure_ascii=False)}",
            f"KR_STATE_DIR = {json.dumps(str(runtime), ensure_ascii=False)}",
            f"KR_LOG_DIR = {json.dumps(str(runtime / 'logs'), ensure_ascii=False)}",
            "",
        ]
    )


def _replace_mcp_block(text: str, block: str) -> str:
    lines = text.splitlines(keepends=True)
    headings = {"[mcp_servers.knowledgeradar]", "[mcp_servers.knowledgeradar.env]"}
    rendered: list[str] = []
    inserted = False
    index = 0
    while index < len(lines):
        if lines[index].strip() not in headings:
            rendered.append(lines[index])
            index += 1
            continue
        if not inserted:
            rendered.append(block + "\n")
            inserted = True
        index += 1
        while index < len(lines) and not (lines[index].strip().startswith("[") and lines[index].strip().endswith("]")):
            index += 1
    if inserted:
        return "".join(rendered)
    separator = "" if not text or text.endswith("\n") else "\n"
    return text + separator + "\n" + block + "\n"


def _replace_rule_block(text: str, fragment: str) -> str:
    marker = "## KnowledgeRadar Perception"
    if marker in text:
        start = text.index(marker)
        tail = text.find("\n## ", start + len(marker))
        end = len(text) if tail == -1 else tail + 1
        return text[:start] + fragment.rstrip() + "\n\n" + text[end:]
    return fragment.rstrip() + "\n\n" + text.lstrip()


def _backup(paths: list[Path], backup_dir: Path) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    for path in paths:
        if path.exists():
            shutil.copy2(path, backup_dir / f"{path.name}.before")


def _plugin_version_with_skill_fingerprint(destination: Path) -> str:
    manifest_path = destination / ".codex-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    skill_hash = _sha256(destination / PLUGIN_SKILL_RELATIVE)[:12]
    version = str(manifest.get("version") or "0.0.0")
    build_prefix = version.split("+", 1)[0]
    return f"{build_prefix}+codex.{skill_hash}"


def _copy_plugin(destination: Path) -> dict[str, str]:
    if destination.exists():
        resolved = destination.resolve()
        if resolved == Path.home().resolve() or resolved == Path.home().resolve().parent:
            raise RuntimeError("refusing broad plugin destination")
        stale = [
            destination / "skills" / "research" / "agents" / "openai.yaml",
            destination / ".mcp.json",
            destination / "mcp.json",
        ]
        for path in stale:
            if path.exists() and path.is_file():
                path.unlink()
    shutil.copytree(PLUGIN_SOURCE, destination, dirs_exist_ok=True)
    manifest_path = destination / ".codex-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    manifest["version"] = _plugin_version_with_skill_fingerprint(destination)
    _write_utf8(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return {
        "version": str(manifest["version"]),
        "skill_sha256": _sha256(destination / PLUGIN_SKILL_RELATIVE),
        "manifest_sha256": _sha256(manifest_path),
    }


def _marketplace_path(plugin_destination: Path) -> Path:
    return plugin_destination.parent.parent / ".agents" / "plugins" / "marketplace.json"


def _update_marketplace(plugin_destination: Path) -> Path:
    path = _marketplace_path(plugin_destination)
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    else:
        payload = {"name": "personal", "interface": {"displayName": "Personal"}, "plugins": []}
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid marketplace payload: {path}")
    plugins = payload.setdefault("plugins", [])
    if not isinstance(plugins, list):
        raise RuntimeError(f"invalid marketplace plugins list: {path}")
    entry = {
        "name": "knowledgeradar-research",
        "source": {"source": "local", "path": "./plugins/knowledgeradar-research"},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "category": "Productivity",
    }
    payload["plugins"] = [item for item in plugins if not (isinstance(item, dict) and item.get("name") == entry["name"])] + [entry]
    _write_utf8(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return path


def _receipt(plugin_destination: Path, source_dirty: bool, plugin_identity: dict[str, str]) -> dict[str, object]:
    plugin_manifest = plugin_destination / ".codex-plugin" / "plugin.json"
    marketplace = _marketplace_path(plugin_destination)
    return {
        "schema": "knowledgeradar-codex-plugin-deployment-receipt/v2",
        "plugin": "knowledgeradar-research",
        "source_repo": str(ROOT),
        "source_commit": _git("rev-parse", "HEAD"),
        "source_dirty": source_dirty,
        "source_entrypoint": str(ENTRYPOINT),
        "transport": "stdio",
        "config_contract": "src/server.py + KR_MCP_TRANSPORT=stdio",
        "plugin_manifest_sha256": _sha256(plugin_manifest),
        "plugin_skill_sha256": plugin_identity["skill_sha256"],
        "plugin_version": plugin_identity["version"],
        "plugin_source": str(plugin_destination),
        "marketplace_path": str(marketplace),
        "marketplace_sha256": _sha256(marketplace),
        "generated_at": datetime.now().astimezone().isoformat(),
        "status": "candidate",
    }


def install(*, apply: bool, config_path: Path, agents_path: Path, plugin_destination: Path, backup_dir: Path) -> dict[str, object]:
    if not ENTRYPOINT.exists():
        raise RuntimeError(f"missing entrypoint: {ENTRYPOINT}")
    if not PLUGIN_SOURCE.exists():
        raise RuntimeError(f"missing plugin source: {PLUGIN_SOURCE}")
    python_exe = ROOT / ".python312" / "python.exe"
    if not python_exe.exists():
        python_exe = Path(sys.executable).resolve()
    config_text = _canonical_config(ROOT, python_exe)
    rule_fragment = _utf8(CODEX_PRODUCT / "AGENTS.knowledgeradar.md")
    current_config = _utf8(config_path) if config_path.exists() else ""
    current_agents = _utf8(agents_path) if agents_path.exists() else ""
    rendered_config = _replace_mcp_block(current_config, config_text)
    rendered_agents = _replace_rule_block(current_agents, rule_fragment)
    parsed = tomllib.loads(rendered_config)
    server = parsed["mcp_servers"]["knowledgeradar"]
    if str(server["args"][-1]) != str(ENTRYPOINT) or parsed["mcp_servers"]["knowledgeradar"]["env"]["KR_MCP_TRANSPORT"] != "stdio":
        raise RuntimeError("rendered config does not satisfy direct-stdio contract")
    if not apply:
        return {"status": "DRY_RUN", "config": str(config_path), "plugin": str(plugin_destination)}

    marketplace_path = _marketplace_path(plugin_destination)
    _backup(
        [
            config_path,
            agents_path,
            plugin_destination.parent / "knowledgeradar-research.deployment-receipt.json",
            marketplace_path,
        ],
        backup_dir,
    )
    _write_utf8(config_path, rendered_config)
    _write_utf8(agents_path, rendered_agents)
    plugin_identity = _copy_plugin(plugin_destination)
    marketplace_path = _update_marketplace(plugin_destination)
    dirty = bool(_git("status", "--porcelain"))
    receipt_path = plugin_destination.parent / "knowledgeradar-research.deployment-receipt.json"
    _write_utf8(receipt_path, json.dumps(_receipt(plugin_destination, dirty, plugin_identity), ensure_ascii=False, indent=2) + "\n")
    return {
        "status": "APPLIED",
        "config": str(config_path),
        "agents": str(agents_path),
        "plugin": str(plugin_destination),
        "marketplace": str(marketplace_path),
        "receipt": str(receipt_path),
        "plugin_version": plugin_identity["version"],
        "plugin_skill_sha256": plugin_identity["skill_sha256"],
        "source_dirty": dirty,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the current KnowledgeRadar Codex product contract.")
    parser.add_argument("--apply", action="store_true", help="write the user config, rule block and plugin source")
    parser.add_argument("--config", default="", help="Codex config.toml path")
    parser.add_argument("--agents", default="", help="global AGENTS.md path")
    parser.add_argument("--plugin", default="", help="plugin source destination")
    parser.add_argument("--backup-dir", default="", help="backup destination")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    home = _codex_home()
    config_path = Path(args.config).expanduser().resolve() if args.config else home / "config.toml"
    agents_path = Path(args.agents).expanduser().resolve() if args.agents else home / "AGENTS.md"
    plugin_destination = Path(args.plugin).expanduser().resolve() if args.plugin else Path.home() / "plugins" / "knowledgeradar-research"
    backup_dir = Path(args.backup_dir).expanduser().resolve() if args.backup_dir else ROOT / "local" / "codex-mcp-repair-snapshot-2026-08-06"
    try:
        result = install(apply=args.apply, config_path=config_path, agents_path=agents_path, plugin_destination=plugin_destination, backup_dir=backup_dir)
    except (OSError, RuntimeError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        result = {"status": "FAIL", "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"DRY_RUN", "APPLIED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
