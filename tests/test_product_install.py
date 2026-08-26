from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import types
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


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_runtime(program_root: Path, install_root: Path, version: str, base_python: Path) -> Path:
        executable = installer.runtime_python(install_root / "runtime" / version)
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("fixture", encoding="utf-8")
        return executable

    monkeypatch.setattr(installer, "ensure_runtime", fake_runtime)


def package_fixture(tmp_path: Path, version: str) -> Path:
    package = tmp_path / f"package-{version}"
    (package / "src").mkdir(parents=True)
    (package / "src" / "server.py").write_text("print('fixture')\n", encoding="utf-8")
    (package / ".env.example").write_text("TAVILY_API_KEY=\n", encoding="utf-8")
    (package / "pyproject.toml").write_text(f"[project]\nname='knowledgeradar'\nversion='{version}'\n", encoding="utf-8")
    registry = package / "config" / "profile_registry.example.json"
    registry.parent.mkdir(parents=True)
    registry.write_text('{"profiles": []}\n', encoding="utf-8")
    shutil.copyfile(ROOT / "config" / "storage-ownership.manifest.json", package / "config" / "storage-ownership.manifest.json")
    (package / "scripts").mkdir()
    shutil.copyfile(ROOT / "scripts" / "setup_product_wizard.py", package / "scripts" / "setup_product_wizard.py")
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
    assert config["command"].endswith("runtime\\0.2.0\\Scripts\\python.exe") or config["command"].endswith("runtime/0.2.0/Scripts/python.exe")
    assert config["env"]["KR_DATA_ROOT"] == str(data_root)
    assert str(first) not in config_text and str(second) not in config_text
    assert (codex_home / "plugins" / "knowledgeradar-research" / ".codex-plugin" / "plugin.json").is_file()
    launcher = install_root / "configure.cmd"
    assert launcher.is_file()
    launcher_text = launcher.read_text(encoding="utf-8")
    assert "console_product.py" in launcher_text
    assert str(data_root) not in launcher_text
    assert (install_root / "console_product.py").is_file()
    assert (install_root / "console.cmd").is_file()


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
    assert "console_product.py" in (install_root / "configure.cmd").read_text(encoding="utf-8")


def test_product_wizard_launcher_is_rebound_when_rolling_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    install_root = tmp_path / "install"
    data_root = tmp_path / "data"
    first = package_fixture(tmp_path, "0.1.0")
    second = package_fixture(tmp_path, "0.2.0")
    first_archive, first_receipt = artifact_fixture(tmp_path, first)
    second_archive, second_receipt = artifact_fixture(tmp_path, second)
    installer.apply_install(first, install_root, data_root, Path(sys.executable), channel="stable", archive=first_archive, receipt_path=first_receipt)
    installer.apply_install(second, install_root, data_root, Path(sys.executable), channel="stable", archive=second_archive, receipt_path=second_receipt)
    active_launcher = (install_root / "configure.cmd").read_text(encoding="utf-8")
    assert "console_product.py" in active_launcher
    installer.rollback(install_root, Path(sys.executable))
    rollback_launcher = (install_root / "configure.cmd").read_text(encoding="utf-8")
    assert "console_product.py" in rollback_launcher


def test_version_neutral_launcher_supports_legacy_active_program(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    install_root = tmp_path / "install"
    data_root = tmp_path / "data"
    package = package_fixture(tmp_path, "0.1.0")
    archive, receipt = artifact_fixture(tmp_path, package)

    installer.apply_install(package, install_root, data_root, Path(sys.executable), channel="stable", archive=archive, receipt_path=receipt)

    helper = (install_root / "console_product.py").read_text(encoding="utf-8")
    assert "active.json" in helper
    assert "setup_wizard.py" in helper
    assert "18882" in helper
    assert (install_root / "configure.cmd").is_file()
    assert (install_root / "console.cmd").is_file()


def test_cli_apply_creates_a_visible_per_user_console_startup_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    install_root = tmp_path / "install"
    data_root = tmp_path / "data"
    package = package_fixture(tmp_path, "0.1.0")
    archive, receipt = artifact_fixture(tmp_path, package)

    assert installer.main(
        [
            "apply",
            "--package-root",
            str(package),
            "--install-root",
            str(install_root),
            "--data-root",
            str(data_root),
            "--python",
            str(Path(sys.executable)),
            "--archive",
            str(archive),
            "--receipt",
            str(receipt),
        ]
    ) == 0

    startup = tmp_path / "roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "KnowledgeRadar Local Console.cmd"
    assert startup.is_file()
    startup_text = startup.read_text(encoding="utf-8")
    assert "--serve --port 18882 --no-open" in startup_text
    assert str(data_root) not in startup_text


def test_update_rebinds_an_opted_in_console_startup_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    install_root = tmp_path / "install"
    data_root = tmp_path / "data"
    first = package_fixture(tmp_path, "0.1.0")
    second = package_fixture(tmp_path, "0.2.0")
    first_archive, first_receipt = artifact_fixture(tmp_path, first)
    second_archive, second_receipt = artifact_fixture(tmp_path, second)

    installer.apply_install(
        first,
        install_root,
        data_root,
        Path(sys.executable),
        channel="stable",
        archive=first_archive,
        receipt_path=first_receipt,
        enable_console_autostart=True,
    )
    installer.apply_install(second, install_root, data_root, Path(sys.executable), channel="stable", archive=second_archive, receipt_path=second_receipt)

    startup = tmp_path / "roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "KnowledgeRadar Local Console.cmd"
    assert "runtime" in startup.read_text(encoding="utf-8")
    assert (install_root / "console_product.py").is_file()


def test_product_wizard_resolves_active_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    install_root = tmp_path / "install"
    data_root = tmp_path / "data"
    package = package_fixture(tmp_path, "0.1.0")
    archive, receipt = artifact_fixture(tmp_path, package)
    installer.apply_install(package, install_root, data_root, Path(sys.executable), channel="stable", archive=archive, receipt_path=receipt)
    script = install_root / "app" / "0.1.0" / "scripts" / "setup_product_wizard.py"
    spec = importlib.util.spec_from_file_location("setup_product_wizard_fixture", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    previous_path = list(sys.path)
    try:
        spec.loader.exec_module(module)
        captured: dict[str, object] = {}
        wizard = types.ModuleType("onboarding.setup_wizard")
        wizard.run_wizard = lambda **kwargs: captured.update(kwargs)
        monkeypatch.setitem(sys.modules, "onboarding.setup_wizard", wizard)
        assert module.main(["--install-root", str(install_root), "--no-open"]) == 0
        assert captured == {"port": 18882, "open_browser": False}
        assert Path(os.environ["KR_RUNTIME_ENV_PATH"]) == data_root / "config" / "runtime.env"
        assert Path(os.environ["KR_DATA_ROOT"]) == data_root
    finally:
        sys.path[:] = previous_path


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


def test_data_root_move_copies_verifies_switches_and_keeps_source_for_rollback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    install_root = tmp_path / "install"
    source_data = tmp_path / "c-data"
    package = package_fixture(tmp_path, "0.1.0")
    archive, receipt = artifact_fixture(tmp_path, package)
    installer.apply_install(package, install_root, source_data, Path(sys.executable), channel="stable", archive=archive, receipt_path=receipt)
    (source_data / "browser_data").mkdir(exist_ok=True)
    (source_data / "browser_data" / "profile-state.bin").write_bytes(b"private-profile-state")
    target_data = tmp_path / "d-data"

    plan = installer.data_root_move_plan(install_root, target_data)
    applied = installer.data_root_move_apply(install_root, target_data, str(plan["confirmation_token"]))

    assert applied["status"] == "APPLIED"
    assert source_data.is_dir()
    assert (target_data / "browser_data" / "profile-state.bin").read_bytes() == b"private-profile-state"
    assert Path(installer.load_active(install_root)["data_root"]) == target_data
    config = tomllib.loads((tmp_path / "codex" / "config.toml").read_text(encoding="utf-8"))
    assert config["mcp_servers"]["knowledgeradar"]["env"]["KR_DATA_ROOT"] == str(target_data)

    restored = installer.data_root_move_rollback(install_root)
    assert restored["status"] == "ROLLED_BACK"
    assert Path(installer.load_active(install_root)["data_root"]) == source_data


def test_data_root_move_refuses_wrong_confirmation_or_live_browser_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    install_root = tmp_path / "install"
    source_data = tmp_path / "c-data"
    package = package_fixture(tmp_path, "0.1.0")
    archive, receipt = artifact_fixture(tmp_path, package)
    installer.apply_install(package, install_root, source_data, Path(sys.executable), channel="stable", archive=archive, receipt_path=receipt)
    target_data = tmp_path / "d-data"
    plan = installer.data_root_move_plan(install_root, target_data)
    with pytest.raises(RuntimeError, match="confirmation"):
        installer.data_root_move_apply(install_root, target_data, "not-the-plan")
    lock = source_data / "browser_data" / "SingletonLock"
    lock.parent.mkdir(exist_ok=True)
    lock.write_text("locked", encoding="utf-8")
    with pytest.raises(RuntimeError, match="close managed browsers"):
        installer.data_root_move_apply(install_root, target_data, str(plan["confirmation_token"]))


def test_optional_browser_capability_requires_a_fresh_plan_and_records_no_private_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    install_root = tmp_path / "install"
    data_root = tmp_path / "data"
    package = package_fixture(tmp_path, "0.1.0")
    archive, receipt = artifact_fixture(tmp_path, package)
    installer.apply_install(package, install_root, data_root, Path(sys.executable), channel="stable", archive=archive, receipt_path=receipt)
    plan = installer.capability_plan(install_root, "browser")
    calls: list[tuple[list[str], Path]] = []
    monkeypatch.setattr(
        installer,
        "_run_optional_download",
        lambda command, *, cwd, env, action: calls.append((command, cwd)) or (Path(env["PLAYWRIGHT_BROWSERS_PATH"]).mkdir(parents=True, exist_ok=True)),
    )

    with pytest.raises(RuntimeError, match="confirmation"):
        installer.capability_apply(install_root, "browser", "wrong-token")
    result = installer.capability_apply(install_root, "browser", str(plan["confirmation_token"]))

    state = json.loads((data_root / "state" / "capabilities.json").read_text(encoding="utf-8"))
    assert result == {"schema": "knowledgeradar-capability-apply/v1", "status": "APPLIED", "capability": "browser", "restart_required": False}
    assert calls[0][0][-3:] == ["-m", "playwright", "install", "chromium"][-3:]
    assert state["capabilities"]["browser"]["status"] == "APPLIED"
    assert "data_root" not in json.dumps(state)


def test_xhs_bridge_capability_stays_in_data_root_and_requires_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    install_root = tmp_path / "install"
    data_root = tmp_path / "data"
    package = package_fixture(tmp_path, "0.1.0")
    bridge = package / "bridge"
    bridge.mkdir()
    (bridge / "package.json").write_text('{"dependencies": {}}\n', encoding="utf-8")
    (bridge / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
    (bridge / "xhs_mcp_bridge.cjs").write_text("console.log('fixture')\n", encoding="utf-8")
    archive, receipt = artifact_fixture(tmp_path, package)
    installer.apply_install(package, install_root, data_root, Path(sys.executable), channel="stable", archive=archive, receipt_path=receipt)
    plan = installer.capability_plan(install_root, "xhs_bridge")
    monkeypatch.setattr(installer.shutil, "which", lambda name: "npm.exe" if name == "npm" else None)
    monkeypatch.setattr(
        installer,
        "_run_optional_download",
        lambda command, *, cwd, env, action: (cwd / "node_modules").mkdir(exist_ok=True),
    )

    result = installer.capability_apply(install_root, "xhs_bridge", str(plan["confirmation_token"]))

    config = tomllib.loads((tmp_path / "codex" / "config.toml").read_text(encoding="utf-8"))
    assert result["restart_required"] is True
    assert (data_root / "capabilities" / "xhs-bridge" / "xhs_mcp_bridge.cjs").is_file()
    assert config["mcp_servers"]["knowledgeradar"]["env"]["XHS_BRIDGE_PATH"].endswith("xhs-bridge\\xhs_mcp_bridge.cjs") or config["mcp_servers"]["knowledgeradar"]["env"]["XHS_BRIDGE_PATH"].endswith("xhs-bridge/xhs_mcp_bridge.cjs")
