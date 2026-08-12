from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load():
    path = ROOT / "scripts" / "product_install.py"
    spec = importlib.util.spec_from_file_location("product_install", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


installer = _load()


def package_fixture(tmp_path: Path, version: str) -> Path:
    package = tmp_path / f"package-{version}"
    (package / "src").mkdir(parents=True)
    (package / "src" / "server.py").write_text("print('fixture')\n", encoding="utf-8")
    (package / ".env.example").write_text("TAVILY_API_KEY=\n", encoding="utf-8")
    (package / "pyproject.toml").write_text(f"[project]\nname='knowledgeradar'\nversion='{version}'\n", encoding="utf-8")
    registry = package / "config" / "profile_registry.example.json"
    registry.parent.mkdir(parents=True)
    registry.write_text('{"profiles": []}\n', encoding="utf-8")
    source_plugin = ROOT / "config" / "codex-product" / "plugin" / "knowledgeradar-research"
    shutil.copytree(source_plugin, package / "config" / "codex-product" / "plugin" / "knowledgeradar-research")
    (package / "package-provenance.json").write_text(json.dumps({"source_commit": "a" * 40, "source_dirty": False}), encoding="utf-8")
    return package


def artifact_fixture(tmp_path: Path, package: Path) -> tuple[Path, Path]:
    archive = tmp_path / f"{package.name}.zip"
    archive.write_bytes(package.name.encode("utf-8"))
    receipt = tmp_path / f"{package.name}.receipt.json"
    receipt.write_text(json.dumps({"source_commit": "a" * 40, "archive_sha256": installer.sha256_file(archive)}), encoding="utf-8")
    return archive, receipt


def test_apply_uses_one_active_program_and_preserves_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    install_root = tmp_path / "install"
    data_root = tmp_path / "data"
    first = package_fixture(tmp_path, "0.1.0")
    first_archive, first_receipt = artifact_fixture(tmp_path, first)
    result = installer.apply_install(first, install_root, data_root, Path(sys.executable), channel="stable", archive=first_archive, receipt_path=first_receipt)
    env_path = data_root / "config" / "runtime.env"
    env_path.write_text("TAVILY_API_KEY=private-value\n", encoding="utf-8")

    second = package_fixture(tmp_path, "0.2.0")
    second_archive, second_receipt = artifact_fixture(tmp_path, second)
    installer.apply_install(second, install_root, data_root, Path(sys.executable), channel="stable", archive=second_archive, receipt_path=second_receipt)
    active = installer.load_active(install_root)
    config_text = (codex_home / "config.toml").read_text(encoding="utf-8")
    config = tomllib.loads(config_text)["mcp_servers"]["knowledgeradar"]

    assert result["status"] == "APPLIED"
    assert active["version"] == "0.2.0"
    assert active["program_root"].endswith("app\\0.2.0") or active["program_root"].endswith("app/0.2.0")
    assert env_path.read_text(encoding="utf-8") == "TAVILY_API_KEY=private-value\n"
    assert config_text.count("[mcp_servers.knowledgeradar]") == 1
    assert config["cwd"].startswith(str(install_root))
    assert config["env"]["KR_DATA_ROOT"] == str(data_root)
    assert str(first) not in config_text and str(second) not in config_text
    assert (codex_home / "plugins" / "knowledgeradar-research" / ".codex-plugin" / "plugin.json").is_file()


def test_rollback_restores_previous_active_without_deleting_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    install_root = tmp_path / "install"
    data_root = tmp_path / "data"
    first = package_fixture(tmp_path, "0.1.0")
    second = package_fixture(tmp_path, "0.2.0")
    first_archive, first_receipt = artifact_fixture(tmp_path, first)
    second_archive, second_receipt = artifact_fixture(tmp_path, second)
    installer.apply_install(first, install_root, data_root, Path(sys.executable), channel="stable", archive=first_archive, receipt_path=first_receipt)
    installer.apply_install(second, install_root, data_root, Path(sys.executable), channel="stable", archive=second_archive, receipt_path=second_receipt)
    (data_root / "config" / "runtime.env").write_text("EXA_API_KEY=private\n", encoding="utf-8")

    result = installer.rollback(install_root, Path(sys.executable))

    assert result["status"] == "ROLLED_BACK"
    assert installer.load_active(install_root)["version"] == "0.1.0"
    assert (data_root / "config" / "runtime.env").read_text(encoding="utf-8") == "EXA_API_KEY=private\n"


def test_source_checkout_cannot_be_activated(tmp_path: Path) -> None:
    source = package_fixture(tmp_path, "0.1.0")
    (source / ".git").mkdir()
    with pytest.raises(RuntimeError, match="source checkout"):
        installer.verify_product_package(source)


def test_apply_rejects_archive_with_a_mismatched_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    package = package_fixture(tmp_path, "0.1.0")
    archive, receipt = artifact_fixture(tmp_path, package)
    receipt.write_text(json.dumps({"source_commit": "a" * 40, "archive_sha256": "0" * 64}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="SHA-256"):
        installer.apply_install(package, tmp_path / "install", tmp_path / "data", Path(sys.executable), channel="stable", archive=archive, receipt_path=receipt)


def test_apply_never_uses_the_real_codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    isolated_home = tmp_path / "isolated-codex"
    monkeypatch.setenv("CODEX_HOME", str(isolated_home))
    package = package_fixture(tmp_path, "0.1.0")
    archive, receipt = artifact_fixture(tmp_path, package)
    installer.apply_install(package, tmp_path / "install", tmp_path / "data", Path(sys.executable), channel="stable", archive=archive, receipt_path=receipt)
    assert (isolated_home / "config.toml").is_file()
    assert not (tmp_path / ".codex" / "config.toml").exists()


def test_migration_is_copy_only_and_never_overwrites_existing_data(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    (legacy / "browser_data").mkdir(parents=True)
    (legacy / ".env").write_text("TAVILY_API_KEY=private\n", encoding="utf-8")
    (legacy / "browser_data" / "cookie.txt").write_text("private-cookie", encoding="utf-8")
    data_root = tmp_path / "data"
    plan = installer.migration_plan(legacy, data_root)
    result = installer.migrate_apply(legacy, data_root)

    assert plan["does_not_modify_legacy_source"] is True
    assert result["status"] == "APPLIED"
    assert "TAVILY_API_KEY=private" in (data_root / "config" / "runtime.env").read_text(encoding="utf-8")
    assert (data_root / "browser_data" / "cookie.txt").read_text(encoding="utf-8") == "private-cookie"
    (data_root / "config" / "runtime.env").write_text("TAVILY_API_KEY=existing\n", encoding="utf-8")
    retry = installer.migrate_apply(legacy, data_root)
    assert retry["status"] == "NEEDS_REVIEW"
    assert (data_root / "config" / "runtime.env").read_text(encoding="utf-8") == "TAVILY_API_KEY=existing\n"
