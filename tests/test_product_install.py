from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import sys
import types
import tomllib
import zipfile

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


@pytest.mark.parametrize('component', ['opencli', 'browser_link'])
def test_component_update_drains_before_mutation_and_resumes_host_on_failure(tmp_path, monkeypatch, component):
    from runtime import browser_components
    events=[]
    monkeypatch.setattr(browser_components,'component_status',lambda *a,**k:{'status':'update_available'})
    def prepare(*a,**k):
        events.append('prepare')
        raise ValueError('copy interrupted')
    monkeypatch.setattr(browser_components,'prepare_component',prepare)
    monkeypatch.setattr(installer,'_quiesce_data_bridges',lambda root:events.append('daemon_drained'))
    def drain(*a):
        events.append('host_drained')
        return {'connected':True}
    lifecycle=types.SimpleNamespace(read_registration=lambda:str(tmp_path/'components/browser_link/owned.json'),_registered_state=lambda *a:tmp_path/'host',
        _drain_idle=drain,_control=lambda state,action:events.append(action))
    monkeypatch.setattr(installer,'browser_link_lifecycle',lambda:lifecycle)
    with pytest.raises(ValueError,match='copy interrupted'):
        installer._prepare_bundled_component(tmp_path,tmp_path,tmp_path,component)
    expected=['daemon_drained','prepare'] if component=='opencli' else ['host_drained','prepare','resume']
    assert events==expected


def test_installation_prepares_extensions_through_shared_update_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME',str(tmp_path/'codex'))
    package=package_fixture(tmp_path,'0.3.0')
    (package/'config/browser-components.json').write_text('{}',encoding='utf-8')
    archive,receipt=artifact_fixture(tmp_path,package)
    calls=[]
    def prepare(program,install,data,component):
        calls.append((program,install,data,component))
        return {'status':'prepared'}
    monkeypatch.setattr(installer,'_prepare_bundled_component',prepare)
    result=installer.apply_install(package,tmp_path/'install',tmp_path/'chosen',Path(sys.executable),
        channel='maintainer-main',archive=archive,receipt_path=receipt)
    assert [row[3] for row in calls]==['browser_link','opencli']
    assert all(row[:3]==(Path(result['active']['program_root']),tmp_path/'install',tmp_path/'chosen') for row in calls)


def test_component_preparation_preserves_external_host_without_opening_or_draining_it(tmp_path, monkeypatch):
    from runtime import browser_components
    external = tmp_path/'other-install'/'host.json'
    events = []
    monkeypatch.setattr(browser_components, 'component_status', lambda *a, **k: {'status':'not_prepared'})
    monkeypatch.setattr(browser_components, 'prepare_component', lambda *a, **k: events.append('prepared') or {'status':'prepared'})
    def external_state(*args):
        raise RuntimeError('browser_link_registration_owned_by_another_installation')
    lifecycle = types.SimpleNamespace(read_registration=lambda: str(external), _registered_state=external_state,
        _drain_idle=lambda *a: pytest.fail('external host must not be drained'),
        _control=lambda *a: pytest.fail('external host must not be controlled'))
    monkeypatch.setattr(installer, 'browser_link_lifecycle', lambda: lifecycle)
    result = installer._prepare_bundled_component(tmp_path/'program', tmp_path/'install', tmp_path/'data', 'browser_link')
    assert result['status']=='prepared'
    assert events==['prepared']
    assert not external.exists()


def test_upgrade_reuses_selected_data_root_when_argument_is_omitted(tmp_path):
    installation=tmp_path/'installation';program=installation/'app'/'v1'
    (program/'src').mkdir(parents=True);(program/'src/server.py').write_text('',encoding='utf-8')
    selected=tmp_path/'user-selected';selected.mkdir()
    installer.write_json_atomic(installation/'active.json',{'schema':installer.ACTIVE_SCHEMA,'program_root':str(program),'data_root':str(selected)})
    assert installer.select_data_root(installation)==selected.resolve()
    assert not (installation/'data').exists()
    selected.rmdir()
    with pytest.raises(RuntimeError,match='unavailable'):installer.select_data_root(installation)


def test_first_install_requires_user_selected_directory(tmp_path):
    with pytest.raises(RuntimeError,match='explicitly selected'):installer.select_data_root(tmp_path,first_install_required=True)
    assert installer.select_data_root(tmp_path,str(tmp_path/'chosen'),first_install_required=True)==tmp_path/'chosen'


def test_legacy_migration_rebases_copied_profiles_and_excludes_runtime_ownership(tmp_path):
    legacy, data = tmp_path / "legacy", tmp_path / "data"
    profile = legacy / "browser_data" / "profiles" / "boss" / "account_a"
    profile.mkdir(parents=True)
    (profile / "Cookies").write_text("fixture", encoding="utf-8")
    (legacy / "config").mkdir()
    (legacy / "config/profile_registry.json").write_text(json.dumps({
        "profiles": [{"profile_dir": str(profile), "user_data_dir": str(profile)}],
    }), encoding="utf-8")
    (legacy / ".env").write_text(
        f"KR_PROJECT_ROOT={legacy}\nKR_DATA_ROOT={legacy}\n"
        f"KR_BOSS_CHROME_USER_DATA_DIR={profile}\nTAVILY_API_KEY=fixture\n", encoding="utf-8",
    )
    assert installer.migrate_apply(legacy, data)["status"] == "APPLIED"
    copied = json.loads((data / "config/profile_registry.json").read_text(encoding="utf-8"))
    assert copied["profiles"][0] == {
        "profile_dir": "browser_data/profiles/boss/account_a",
        "user_data_dir": "browser_data/profiles/boss/account_a",
    }
    env = (data / "config/runtime.env").read_text(encoding="utf-8")
    assert "TAVILY_API_KEY=fixture" in env
    assert "KR_PROJECT_ROOT" not in env and "KR_DATA_ROOT" not in env
    assert "KR_BOSS_CHROME_USER_DATA_DIR" not in env
    assert (profile / "Cookies").read_text(encoding="utf-8") == "fixture"


def test_legacy_registry_migration_preserves_existing_destination_and_external_paths(tmp_path):
    legacy, data = tmp_path / "legacy", tmp_path / "data"
    (legacy / "config").mkdir(parents=True)
    data.mkdir()
    source, destination = legacy / "config/profile_registry.json", data / "registry.json"
    external = str(tmp_path / "external-profile")
    source.write_text(json.dumps({"profiles": [
        {"profile_dir": external}, {"profile_dir": "browser_data/relative"},
    ]}), encoding="utf-8")
    installer.migrate_profile_registry(source, destination, legacy_root=legacy)
    assert json.loads(destination.read_text(encoding="utf-8"))["profiles"] == [
        {"profile_dir": external}, {"profile_dir": "browser_data/relative"},
    ]
    destination.write_text('{"profiles": [], "owner": "user"}\n', encoding="utf-8")
    original = destination.read_bytes()
    assert installer.migrate_profile_registry(source, destination, legacy_root=legacy)[2] == ["profile_registry.json"]
    assert destination.read_bytes() == original


def test_legacy_registry_example_uses_same_rebase_path(tmp_path):
    legacy = tmp_path / "legacy"
    (legacy / "config").mkdir(parents=True)
    source, destination = legacy / "config/profile_registry.json", tmp_path / "registry.json"
    source.write_text(json.dumps({"profiles": [{"profile_dir": str(legacy / "browser_data/.hidden-profile")}]}), encoding="utf-8")
    destination.write_text('{"version":"example"}', encoding="utf-8")
    installer.migrate_profile_registry(source, destination, legacy_root=legacy)
    assert json.loads(destination.read_text(encoding="utf-8"))["profiles"][0]["profile_dir"] == "browser_data/.hidden-profile"


def test_retired_ocr_cannot_be_planned_restored_or_loaded(tmp_path, monkeypatch):
    from runtime import model_roles, optional_media
    assert set(model_roles.COMPONENTS) == {"qwen3-asr-0.6b", "docling"}
    assert not (ROOT / "config/optional-ocr-win-py312.lock").exists()
    for role in model_roles.COMPONENTS.values():
        assert optional_media.file_sha(ROOT / role["requirements_lock"]) == role["requirements_sha256"]
    retired = "paddleocr-vl-1.6"
    with pytest.raises(RuntimeError, match="unsupported optional capability"):
        installer.capability_plan(tmp_path, retired)
    monkeypatch.setenv("KR_OPTIONAL_COMPONENT_RECEIPTS_DIR", str(tmp_path / "receipts"))
    (tmp_path / "receipts").mkdir()
    (tmp_path / "receipts" / (retired + ".json")).write_text('{"status":"ready"}', encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported_optional_component"):
        optional_media.validate_receipt(retired)
    data_root = tmp_path / "data"
    installer.write_json_atomic(installer.capability_state_path(data_root), {
        "schema": installer.CAPABILITY_STATE_SCHEMA, "capabilities": {retired: {"status": "APPLIED"}}})
    monkeypatch.setattr(installer, "_run_optional_download", lambda *_a, **_k: pytest.fail("restored retired component"))
    assert installer.restore_selected_runtime_components(tmp_path, Path("python.exe"), data_root) == []


def test_maintainer_marker_is_per_install_and_keeps_active_identity(tmp_path,monkeypatch):
    root=tmp_path/'install'
    data=tmp_path/'data'
    active={'data_root':str(data),'version':'fixture-version','program_root':str(tmp_path/'program')}
    monkeypatch.setattr(installer,'load_active',lambda path:active.copy())
    from onboarding.console_page import _developer_view_enabled
    monkeypatch.setenv('KR_DATA_ROOT',str(data))
    monkeypatch.setenv('KR_CONSOLE_ROLE','stable')
    monkeypatch.delenv('KR_DEVELOPER_VIEW',raising=False)
    assert not _developer_view_enabled()
    installer.set_developer_view(root,True)
    assert _developer_view_enabled()
    installer.set_developer_view(root,False)
    assert not _developer_view_enabled()
    assert active['version']=='fixture-version'


def test_upgrade_restores_only_previously_selected_components(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    installer.write_json_atomic(installer.capability_state_path(data_root), {
        "schema": installer.CAPABILITY_STATE_SCHEMA,
        "capabilities": {"media_downloader": {"status": "APPLIED"}},
    })
    calls = []
    monkeypatch.setattr(installer.subprocess, "run", lambda *_args, **_kwargs: types.SimpleNamespace(returncode=1))
    monkeypatch.setattr(installer, "_run_optional_download", lambda command, **kwargs: calls.append(command))
    result = installer.restore_selected_runtime_components(tmp_path, Path("python.exe"), data_root)
    assert result == ["media_downloader"]
    assert len(calls) == 1 and calls[0][-1].startswith("yt-dlp")
    assert not any("whisper" in arg or "playwright" == arg for command in calls for arg in command)


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
    (package / "src/runtime").mkdir()
    shutil.copyfile(ROOT / "src/runtime/model_roles.py", package / "src/runtime/model_roles.py")
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
    provenance = package / "package-provenance.json"
    payload = json.loads(provenance.read_text(encoding="utf-8"))
    payload["files"] = [{"path": p.relative_to(package).as_posix(), "sha256": installer.sha256_file(p)} for p in package.rglob("*") if p.is_file() and p.name not in {"package-provenance.json", "SBOM.json"}]
    provenance.write_text(json.dumps(payload), encoding="utf-8")
    (package / "SBOM.json").write_text("{}", encoding="utf-8")
    with zipfile.ZipFile(archive, "w") as bundle:
        for p in package.rglob("*"):
            if p.is_file():
                bundle.write(p, "KnowledgeRadar/" + p.relative_to(package).as_posix())
    receipt = tmp_path / f"{package.name}.receipt.json"
    receipt.write_text(json.dumps({"source_commit": payload["source_commit"], "archive_sha256": installer.sha256_file(archive), "package_provenance_sha256": installer.sha256_file(provenance), "sbom_sha256": installer.sha256_file(package / "SBOM.json")}), encoding="utf-8")
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
    parsed_config = tomllib.loads(config_text)
    config = parsed_config["mcp_servers"]["knowledgeradar"]

    assert result["status"] == "APPLIED"
    assert active["version"] == "0.2.0"
    assert Path(active["program_root"]).name == active["installation_id"]
    assert active["installation_id"].startswith("0.2.0-")
    assert env_path.read_text(encoding="utf-8").startswith("TAVILY_API_KEY=private-value\n")
    assert "KR_FRAME_VISION_MODELS=bailian:qwen3.8-omni-flash" in env_path.read_text(encoding="utf-8")
    assert config_text.count("[mcp_servers.knowledgeradar]") == 1
    assert parsed_config["mcp_optional_startup_grace_ms"] == 0
    assert config["cwd"].startswith(str(install_root))
    assert active["installation_id"] in config["command"]
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


def test_consecutive_main_packages_same_version_keep_distinct_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    install_root, data_root = tmp_path / "install", tmp_path / "data"
    first = package_fixture(tmp_path / "a", "0.1.0")
    archive, receipt = artifact_fixture(tmp_path / "a", first)
    installer.apply_install(first, install_root, data_root, Path(sys.executable), channel="maintainer-main", archive=archive, receipt_path=receipt)
    previous = installer.load_active(install_root)
    second = package_fixture(tmp_path / "b", "0.1.0")
    provenance = second / "package-provenance.json"
    provenance.write_text(json.dumps({"source_commit": "b" * 40, "source_dirty": False}), encoding="utf-8")
    archive, receipt = artifact_fixture(tmp_path / "b", second)
    installer.apply_install(second, install_root, data_root, Path(sys.executable), channel="maintainer-main", archive=archive, receipt_path=receipt)
    active = installer.load_active(install_root)
    assert active["version"] == previous["version"]
    assert active["installation_id"] != previous["installation_id"]
    assert Path(previous["program_root"]).is_dir()
    installer.rollback(install_root, Path(sys.executable))
    restored = installer.load_active(install_root)
    assert restored["installation_id"] == previous["installation_id"]
    assert restored["source_commit"] == "a" * 40


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
    registered: list[Path] = []

    def register_task(root: Path, runtime: Path) -> Path:
        del runtime
        marker = root / "console-autostart.task"
        marker.write_text("task", encoding="utf-8")
        registered.append(marker)
        return marker

    monkeypatch.setattr(installer, "write_console_autostart", register_task)

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
    assert registered == [install_root / "console-autostart.task"]
    assert not startup.exists()


def test_update_rebinds_an_opted_in_console_startup_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    install_root = tmp_path / "install"
    data_root = tmp_path / "data"
    first = package_fixture(tmp_path, "0.1.0")
    second = package_fixture(tmp_path, "0.2.0")
    first_archive, first_receipt = artifact_fixture(tmp_path, first)
    second_archive, second_receipt = artifact_fixture(tmp_path, second)
    registrations: list[Path] = []

    def register_task(root: Path, runtime: Path) -> Path:
        del runtime
        marker = root / "console-autostart.task"
        marker.write_text("task", encoding="utf-8")
        registrations.append(marker)
        return marker

    monkeypatch.setattr(installer, "write_console_autostart", register_task)

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

    assert registrations == [install_root / "console-autostart.task", install_root / "console-autostart.task"]
    assert (install_root / "console-autostart.task").is_file()
    assert (install_root / "console_product.py").is_file()


def test_product_wizard_resolves_active_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    install_root = tmp_path / "install"
    data_root = tmp_path / "data"
    package = package_fixture(tmp_path, "0.1.0")
    archive, receipt = artifact_fixture(tmp_path, package)
    installer.apply_install(package, install_root, data_root, Path(sys.executable), channel="stable", archive=archive, receipt_path=receipt)
    script = Path(installer.load_active(install_root)["program_root"]) / "scripts" / "setup_product_wizard.py"
    spec = importlib.util.spec_from_file_location("setup_product_wizard_fixture", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    previous_path = list(sys.path)
    try:
        spec.loader.exec_module(module)
        captured: dict[str, object] = {}
        def delegated(command, **kwargs):
            captured.update(command=command, **kwargs)
            return types.SimpleNamespace(returncode=0)
        monkeypatch.setattr(module.subprocess, 'run', delegated)
        assert module.main(["--install-root", str(install_root), "--no-open"]) == 0
        assert captured['command'] == [sys.executable, str(install_root / 'console_product.py'), '--role', 'stable', '--port', '18882', '--install-root', str(install_root), '--no-open']
        with pytest.raises(SystemExit):
            module.main(['--port', '0'])
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


def test_data_move_token_rejects_same_length_edit_and_rollback_preserves_new_work(tmp_path,monkeypatch):
    monkeypatch.setenv('CODEX_HOME',str(tmp_path/'codex'))
    package=package_fixture(tmp_path,'0.1.0');archive,receipt=artifact_fixture(tmp_path,package)
    install=tmp_path/'install';source=tmp_path/'source';target=tmp_path/'target'
    installer.apply_install(package,install,source,Path(sys.executable),channel='stable',archive=archive,receipt_path=receipt)
    note=source/'note.md';note.write_text('one')
    plan=installer.data_root_move_plan(install,target)
    note.write_text('two')
    with pytest.raises(RuntimeError,match='confirmation'):
        installer.data_root_move_apply(install,target,plan['confirmation_token'])
    current=installer.data_root_move_plan(install,target)
    installer.data_root_move_apply(install,target,current['confirmation_token'])
    (target/'new.md').write_text('new work')
    with pytest.raises(RuntimeError,match='new data has changed'):
        installer.data_root_move_rollback(install)
    assert (target/'new.md').read_text()=='new work'


def test_data_move_resumes_same_journal_after_active_switch(tmp_path,monkeypatch):
    monkeypatch.setenv('CODEX_HOME',str(tmp_path/'codex'))
    package=package_fixture(tmp_path,'0.1.0');archive,receipt=artifact_fixture(tmp_path,package)
    install=tmp_path/'install';source=tmp_path/'source';target=tmp_path/'target'
    installer.apply_install(package,install,source,Path(sys.executable),channel='stable',archive=archive,receipt_path=receipt)
    plan=installer.data_root_move_plan(install,target)
    original=installer.write_product_wizard_launcher
    monkeypatch.setattr(installer,'write_product_wizard_launcher',lambda *a:(_ for _ in ()).throw(OSError('simulated power loss')))
    with pytest.raises(OSError):installer.data_root_move_apply(install,target,plan['confirmation_token'])
    assert (install/'backup/data-move-pending.json').is_file()
    monkeypatch.setattr(installer,'write_product_wizard_launcher',original)
    assert installer.data_root_move_plan(install,target)['resume']
    assert installer.data_root_move_apply(install,target,plan['confirmation_token'])['status']=='APPLIED'
    assert not (install/'backup/data-move-pending.json').exists()


def test_data_move_resumes_partial_copy_without_replacing_user_files(tmp_path,monkeypatch):
    monkeypatch.setenv('CODEX_HOME',str(tmp_path/'codex'))
    package=package_fixture(tmp_path,'0.1.0');archive,receipt=artifact_fixture(tmp_path,package)
    install=tmp_path/'install';source=tmp_path/'source';target=tmp_path/'target'
    installer.apply_install(package,install,source,Path(sys.executable),channel='stable',archive=archive,receipt_path=receipt)
    (source/'first.md').write_text('first');(source/'second.md').write_text('second')
    plan=installer.data_root_move_plan(install,target)
    original=installer.shutil.copy2;count=[0]
    def copy_then_interrupt(src,dest,*a,**kw):
        result=original(src,dest,*a,**kw);count[0]+=1
        if count[0]==2:raise OSError('simulated copy interruption')
        return result
    monkeypatch.setattr(installer.shutil,'copy2',copy_then_interrupt)
    with pytest.raises(installer.shutil.Error):installer.data_root_move_apply(install,target,plan['confirmation_token'])
    journal=json.loads((install/'backup/data-move-pending.json').read_text())
    assert journal['phase']=='copying' and installer.load_active(install)['data_root']==str(source)
    monkeypatch.setattr(installer.shutil,'copy2',original)
    assert installer.data_root_move_plan(install,target)['resume']
    assert installer.data_root_move_apply(install,target,plan['confirmation_token'])['status']=='APPLIED'
    assert (target/'first.md').read_text()=='first' and (source/'second.md').read_text()=='second'


def test_data_move_binding_failure_restores_source_and_keeps_partial_target(tmp_path,monkeypatch):
    from runtime import data_relocation
    monkeypatch.setenv('CODEX_HOME',str(tmp_path/'codex'))
    package=package_fixture(tmp_path,'0.1.0');archive,receipt=artifact_fixture(tmp_path,package)
    install=tmp_path/'install';source=tmp_path/'source';target=tmp_path/'target'
    installer.apply_install(package,install,source,Path(sys.executable),channel='stable',archive=archive,receipt_path=receipt)
    (source/'note.md').write_text('original')
    plan=installer.data_root_move_plan(install,target)
    def interrupted(*a):
        (target/'partial-binding.json').write_text('{}')
        raise OSError('simulated binding interruption')
    monkeypatch.setattr(data_relocation,'relocate_owned_state',interrupted)
    with pytest.raises(OSError):installer.data_root_move_apply(install,target,plan['confirmation_token'])
    with pytest.raises(RuntimeError,match='data binding interrupted'):installer.data_root_move_plan(install,target)
    assert installer.data_root_move_rollback(install)['source_restored']
    assert (target/'partial-binding.json').exists() and (source/'note.md').read_text()=='original'
    assert not (install/'backup/data-move-pending.json').exists()


def test_data_move_rollback_before_active_switch_restores_connection_configuration(tmp_path,monkeypatch):
    monkeypatch.setenv('CODEX_HOME',str(tmp_path/'codex'))
    package=package_fixture(tmp_path,'0.1.0');archive,receipt=artifact_fixture(tmp_path,package)
    install=tmp_path/'install';source=tmp_path/'source';target=tmp_path/'target'
    installer.apply_install(package,install,source,Path(sys.executable),channel='stable',archive=archive,receipt_path=receipt)
    plan=installer.data_root_move_plan(install,target)
    original=installer.write_json_atomic
    def interrupt(path,payload):
        if path==install/'active.json':raise OSError('simulated active switch interruption')
        return original(path,payload)
    monkeypatch.setattr(installer,'write_json_atomic',interrupt)
    with pytest.raises(OSError):installer.data_root_move_apply(install,target,plan['confirmation_token'])
    config=tmp_path/'codex/config.toml'
    assert tomllib.loads(config.read_text())['mcp_servers']['knowledgeradar']['env']['KR_DATA_ROOT']==str(target)
    installer.data_root_move_rollback(install)
    assert tomllib.loads(config.read_text())['mcp_servers']['knowledgeradar']['env']['KR_DATA_ROOT']==str(source)


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


def test_media_component_plans_are_console_owned_and_separate_runtime_from_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    install_root = tmp_path / "install"
    data_root = tmp_path / "data"
    package = package_fixture(tmp_path, "0.1.0")
    archive, receipt = artifact_fixture(tmp_path, package)
    installer.apply_install(package, install_root, data_root, Path(sys.executable), channel="stable", archive=archive, receipt_path=receipt)

    downloader = installer.capability_plan(install_root, "media_downloader")
    model = installer.capability_plan(install_root, "transcription_model")
    assert downloader['details']['command'][-1] == 'yt-dlp>=2024.8.6'
    assert model['details']['label'] == '本地语音转写（Qwen3-ASR 0.6B · sherpa INT8）'
    assert model['details']['command'][-2:] == ['qwen3-asr-0.6b', '--acknowledge-download']
    assert '隔离' in model['details']['boundary']
    assert 'transcription_runtime' not in installer.CAPABILITY_IDS


def test_retired_xhs_bridge_cannot_be_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    install_root = tmp_path / "install"
    data_root = tmp_path / "data"
    package = package_fixture(tmp_path, "0.1.0")
    archive, receipt = artifact_fixture(tmp_path, package)
    installer.apply_install(package, install_root, data_root, Path(sys.executable), channel="stable", archive=archive, receipt_path=receipt)
    with pytest.raises(RuntimeError, match="OpenCLI"):
        installer.capability_plan(install_root, "xhs_bridge")
    assert not (data_root / "capabilities" / "xhs-bridge").exists()
