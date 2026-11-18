from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = _load("build_product_lite_package")
verifier = _load("verify_release_candidate")


def test_verifier_rejects_receipt_from_a_different_source(tmp_path):
    import zipfile

    root = tmp_path / "KnowledgeRadar"
    for name in verifier.REQUIRED:
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("fixture", encoding="utf-8")
    (root / "package-provenance.json").write_text(json.dumps({
        "source_commit": "a" * 40, "source_dirty": False,
    }), encoding="utf-8")
    archive = tmp_path / "KnowledgeRadar.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for file in root.rglob("*"):
            if file.is_file():
                bundle.write(file, file.relative_to(tmp_path))
    (tmp_path / "candidate-receipt.json").write_text(json.dumps({
        "source_commit": "b" * 40, "source_dirty": False, "archive_sha256": verifier.sha256_file(archive),
    }), encoding="utf-8")
    with pytest.raises(RuntimeError, match="different source"):
        verifier.verify(tmp_path, content_only=True)


@pytest.mark.parametrize('fault', ['from pathlib import Path; Path("not-shipped-required-file").read_text()',
                                  'import dependency_not_shipped_in_product'])
def test_ready_owner_does_not_hide_broken_real_mcp_request(tmp_path, fault):
    import shutil
    manifest=json.loads((ROOT/'config/package-manifest.product-lite.json').read_text(encoding='utf-8'))
    files,_,_=builder.collect_files(manifest)
    for source in files:
        target=tmp_path/source.relative_to(ROOT)
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(source,target)
    server=tmp_path/'src/server.py'
    # The owner can still become ready; only the actual stdio path is broken.
    server.write_text(server.read_text(encoding='utf-8')+'\n',encoding='utf-8')
    source=server.read_text(encoding='utf-8')
    import ast
    tree=ast.parse(source)
    insert_after=next((n.end_lineno for n in tree.body if isinstance(n,ast.ImportFrom) and n.module=='__future__'),0)
    lines=source.splitlines(True); lines.insert(insert_after, fault+'\n')
    server.write_text(''.join(lines),encoding='utf-8')
    with pytest.raises(RuntimeError,match='candidate business probe failed'):
        verifier.business_probe(tmp_path)


def test_package_business_probe_boots_real_owner_without_governance_manifest(tmp_path):
    import shutil
    manifest = json.loads((ROOT/'config/package-manifest.product-lite.json').read_text(encoding='utf-8'))
    files, _, _ = builder.collect_files(manifest)
    for source in files:
        target = tmp_path/source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    assert not (tmp_path/'config/project-structure.manifest.json').exists()
    result = verifier.business_probe(tmp_path)
    assert result['execution_owner_ready'] is True
    assert result['mcp_fixed_evidence_readback'] is True
    assert result['execution_receipt']['request_id']
    assert result['execution_receipt']['generation'] == result['owner']['generation']
    assert result['normal_shutdown'] is True


def test_consumer_environment_cannot_inherit_development_or_user_settings(tmp_path, monkeypatch):
    monkeypatch.setenv('PYTHONPATH', 'development-only')
    monkeypatch.setenv('KR_ACTIVE_INSTALL_ROOT', 'real-install')
    monkeypatch.setenv('CODEX_HOME', 'real-user')
    env = verifier.consumer_environment(tmp_path, tmp_path/'package')
    assert env['CODEX_HOME'] == str(tmp_path/'codex')
    assert 'KR_ACTIVE_INSTALL_ROOT' not in env
    assert env['PYTHONPATH'] == str(tmp_path/'package/src')
    assert all(str(tmp_path) in env[key] for key in ('HOME','USERPROFILE','APPDATA','LOCALAPPDATA','KR_OWNER_ROOT'))


def test_consumer_uses_packaged_runtime_preparation_not_verifier_interpreter(tmp_path, monkeypatch):
    import sys
    import pytest
    package = tmp_path/'package'
    (package/'scripts').mkdir(parents=True)
    (package/'scripts/product_install.py').write_text('raise RuntimeError("packaged-runtime-preparation-reached")')
    with pytest.raises(RuntimeError, match='packaged-runtime-preparation-reached'):
        verifier.prepare_consumer_runtime(package, tmp_path/'isolated', sys.executable)


def test_package_business_probe_rejects_broken_bundled_component_catalog(tmp_path):
    import shutil
    import pytest
    manifest = json.loads((ROOT/'config/package-manifest.product-lite.json').read_text(encoding='utf-8'))
    files, _, _ = builder.collect_files(manifest)
    for source in files:
        target = tmp_path/source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    catalog = tmp_path/'config/browser-components.json'
    value = json.loads(catalog.read_text(encoding='utf-8'))
    value['components']['browser_link']['tree_sha256'] = 'broken'
    catalog.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(RuntimeError, match='component_catalog_tree_mismatch'):
        verifier.business_probe(tmp_path)


def test_package_business_probe_rejects_a_package_with_broken_archive_module(tmp_path):
    import pytest
    (tmp_path/'src/runtime').mkdir(parents=True)
    (tmp_path/'src/runtime/research_archive.py').write_text('raise RuntimeError("broken packaged archive")')
    with pytest.raises(RuntimeError, match='candidate business probe failed'):
        verifier.business_probe(tmp_path)


def test_package_business_probe_cannot_borrow_modules_from_source_checkout(tmp_path):
    import pytest
    (tmp_path/'src').mkdir()
    with pytest.raises(RuntimeError, match='candidate business probe failed'):
        verifier.business_probe(tmp_path)


def test_ci_receipt_identity_is_explicit_and_local_build_is_not_a_ci_run(monkeypatch):
    import pytest
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    assert builder.ci_build_identity() is None
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('GITHUB_RUN_ID', '')
    with pytest.raises(RuntimeError, match='incomplete'):
        builder.ci_build_identity()
    for key, value in {'GITHUB_RUN_ID':'123', 'GITHUB_RUN_ATTEMPT':'2', 'GITHUB_REPOSITORY':'owner/source',
                       'GITHUB_EVENT_NAME':'push', 'GITHUB_REF':'refs/heads/main', 'GITHUB_WORKFLOW':'CI'}.items():
        monkeypatch.setenv(key, value)
    assert builder.ci_build_identity()['run_attempt'] == '2'


def test_runtime_freeze_uses_the_packaged_lf_text_identity(tmp_path):
    freezer = _load("kr_freeze_runtime")
    path = tmp_path / "pyproject.toml"
    path.write_bytes(b"version = 'test'\r\n")
    windows = freezer.canonical_text_sha(path)
    path.write_bytes(b"version = 'test'\n")
    assert freezer.canonical_text_sha(path) == windows


def test_product_manifest_only_references_public_files() -> None:
    manifest = json.loads((ROOT / "config" / "package-manifest.product-lite.json").read_text(encoding="utf-8"))
    _, missing, _ = builder.collect_files(manifest)
    assert missing == []


def test_product_manifest_includes_runtime_and_plugin() -> None:
    manifest = json.loads((ROOT / "config" / "package-manifest.product-lite.json").read_text(encoding="utf-8"))
    files, _, untracked = builder.collect_files(manifest)
    paths = {item.relative_to(ROOT).as_posix() for item in files}
    assert "src/server.py" in paths
    assert "config/recruitment_city_registry.json" in paths
    assert "scripts/product_install.py" in paths
    assert "bridge/xhs_mcp_bridge.cjs" not in paths
    # Local inputs remain untracked until a separately authorized commit.
    assert "src/runtime/xhs_opencli_runner.mjs" in paths | set(untracked)
    assert "config/codex-product/plugin/knowledgeradar-research/.codex-plugin/plugin.json" in paths
    assert "scripts/install.bat" not in paths
    assert "scripts/setup_codex_product.py" not in paths
    assert "scripts/verify_release_candidate.py" not in paths


def test_product_manifest_excludes_generated_egg_metadata() -> None:
    manifest = json.loads((ROOT / "config" / "package-manifest.product-lite.json").read_text(encoding="utf-8"))
    assert builder.is_excluded("src/knowledgeradar.egg-info/PKG-INFO", manifest["exclude"])


def test_mcp_dependency_stays_on_the_fastmcp_compatibility_line() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"mcp>=1.9,<2.0"' in pyproject


def test_candidate_provenance_has_no_absolute_source_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(builder, "git_value", lambda *args: "")
    output = tmp_path / "package"
    output.mkdir()
    manifest = ROOT / "config" / "package-manifest.product-lite.json"
    builder.write_metadata(output, manifest, [], "a" * 40)
    provenance = json.loads((output / "package-provenance.json").read_text(encoding="utf-8"))
    assert provenance["source_dirty"] is False
    assert "source_root" not in provenance
    assert all("Users" not in str(value) for value in provenance.values())
    assert (output / "SBOM.json").is_file()


def test_safe_extract_rejects_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "bad.zip"
    import zipfile
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("KnowledgeRadar/../outside.txt", "no")
    try:
        verifier.safe_extract(archive, tmp_path / "extract")
    except RuntimeError as exc:
        assert "unsafe" in str(exc)
    else:
        raise AssertionError("unsafe archive was accepted")


def test_candidate_probe_reuses_the_owned_state_and_cleanup_contract(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    calls = []

    def probe(root):
        calls.append(root)
        return {'status': 'PASS', 'tools': sorted(verifier.EXPECTED_TOOLS)}

    monkeypatch.setitem(sys.modules, 'verify_codex_product', SimpleNamespace(stdio_probe=probe))
    assert verifier.stdio_probe(tmp_path) == sorted(verifier.EXPECTED_TOOLS)
    assert calls == [tmp_path]


def test_candidate_probe_propagates_the_shared_probe_failure(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    import pytest

    monkeypatch.setitem(sys.modules, 'verify_codex_product', SimpleNamespace(
        stdio_probe=lambda root: {'status': 'FAIL', 'error': 'isolated owner did not exit'}))
    with pytest.raises(RuntimeError, match='isolated owner did not exit'):
        verifier.stdio_probe(tmp_path)


def test_candidate_rejects_worktree_line_endings_in_runtime_identity(tmp_path: Path) -> None:
    import hashlib
    import pytest
    (tmp_path / "config").mkdir()
    (tmp_path / "pyproject.toml").write_bytes(b"version = 'test'\n")
    (tmp_path / "config/runtime-win-py312.lock").write_bytes(b"locked\n")
    metadata = {"pyproject_sha256": hashlib.sha256(b"version = 'test'\r\n").hexdigest(),
                "lock_sha256": hashlib.sha256(b"locked\n").hexdigest()}
    (tmp_path / "config/runtime-artifacts.json").write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(RuntimeError, match="runtime lock identity mismatch"):
        verifier.verify_runtime_inputs(tmp_path)
    metadata["pyproject_sha256"] = hashlib.sha256((tmp_path / "pyproject.toml").read_bytes()).hexdigest()
    (tmp_path / "config/runtime-artifacts.json").write_text(json.dumps(metadata), encoding="utf-8")
    verifier.verify_runtime_inputs(tmp_path)


def test_runtime_manifest_matches_actual_packaged_source_inputs():
    freezer = _load('kr_freeze_runtime')
    metadata = json.loads((ROOT / 'config/runtime-artifacts.json').read_text(encoding='utf-8'))
    for relative, key in [('pyproject.toml', 'pyproject_sha256'), ('config/runtime-win-py312.lock', 'lock_sha256')]:
        assert metadata[key] == freezer.canonical_text_sha(ROOT / relative), relative



def test_freezer_rejects_stale_wheelhouse_without_rewriting_manifest(tmp_path):
    import hashlib
    import zipfile
    import pytest
    freezer = _load('kr_freeze_runtime')
    (tmp_path/'config').mkdir()
    (tmp_path/'pyproject.toml').write_text('[project]\nname="fixture"\n', encoding='utf-8')
    wheels = tmp_path/'wheels'; wheels.mkdir()
    old = wheels/'fixture-1.0-py3-none-any.whl'
    with zipfile.ZipFile(old, 'w') as wheel:
        wheel.writestr('fixture-1.0.dist-info/METADATA', 'Name: fixture\nVersion: 1.0\n')
    (tmp_path/'config/runtime-win-py312.lock').write_text('fixture==2.0 --hash=sha256:'+'a'*64+'\n', encoding='utf-8')
    manifest = tmp_path/'config/runtime-artifacts.json'
    manifest.write_text('original manifest', encoding='utf-8')
    with pytest.raises(ValueError, match='missing_locked_wheel'):
        freezer.freeze(tmp_path, wheels)
    assert manifest.read_text(encoding='utf-8') == 'original manifest'
    correct = wheels/'fixture-2.0-py3-none-any.whl'
    with zipfile.ZipFile(correct, 'w') as wheel:
        wheel.writestr('fixture-2.0.dist-info/METADATA', 'Name: fixture\nVersion: 2.0\n')
    digest = hashlib.sha256(correct.read_bytes()).hexdigest()
    (tmp_path/'config/runtime-win-py312.lock').write_text('fixture==2.0 --hash=sha256:'+digest+'\n', encoding='utf-8')
    freezer.freeze(tmp_path, wheels)
    frozen = json.loads(manifest.read_text(encoding='utf-8'))
    assert [row['file'] for row in frozen['wheels']] == [correct.name]
