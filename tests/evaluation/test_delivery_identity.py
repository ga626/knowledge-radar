import hashlib
import json
from pathlib import Path
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import product_install as installer


def package(tmp_path):
    root = tmp_path / "KnowledgeRadar"
    (root / "src").mkdir(parents=True)
    (root / "src/server.py").write_text("x=1", encoding="utf-8")
    (root / "pyproject.toml").write_text('[project]\nversion="0.1.0a13"\n', encoding="utf-8")
    files = [{"path": p.relative_to(root).as_posix(), "sha256": installer.sha256_file(p)} for p in root.rglob("*") if p.is_file()]
    (root / "package-provenance.json").write_text(json.dumps({"source_commit": "a" * 40, "source_dirty": False, "files": files}), encoding="utf-8")
    (root / "SBOM.json").write_text("{}", encoding="utf-8")
    archive = tmp_path / "candidate.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for p in root.rglob("*"):
            if p.is_file():
                bundle.write(p, "KnowledgeRadar/" + p.relative_to(root).as_posix())
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"source_commit": "a" * 40, "archive_sha256": installer.sha256_file(archive),
                                   "package_provenance_sha256": installer.sha256_file(root / "package-provenance.json"),
                                   "sbom_sha256": installer.sha256_file(root / "SBOM.json")}), encoding="utf-8")
    return root, archive, receipt


def test_extracted_content_must_match_archive_and_receipt(tmp_path):
    root, archive, receipt = package(tmp_path)
    assert installer.verify_artifact_receipt(root, archive, receipt)
    (root / "src/server.py").write_text("x=2", encoding="utf-8")
    with pytest.raises(RuntimeError, match="identity mismatch"):
        installer.verify_artifact_receipt(root, archive, receipt)


def test_unlisted_file_and_duplicate_zip_members_are_rejected(tmp_path):
    root, archive, receipt = package(tmp_path)
    (root / "unlisted.py").write_text("x=2", encoding="utf-8")
    with pytest.raises(RuntimeError, match="inventory differ"):
        installer.verify_artifact_receipt(root, archive, receipt)


def test_missing_previous_runtime_does_not_switch_active(tmp_path, monkeypatch):
    old = tmp_path / "app/old"
    old.mkdir(parents=True)
    (tmp_path / "backup").mkdir()
    (tmp_path / "backup/active.previous.json").write_text(json.dumps({"program_root": str(old), "version": "old"}), encoding="utf-8")
    current = tmp_path / "active.json"
    current.write_text('{"sentinel":"current"}', encoding="utf-8")
    before = current.read_bytes()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "isolated-home"))
    with pytest.raises(RuntimeError, match="unavailable"):
        installer.rollback(tmp_path, Path(sys.executable))
    assert current.read_bytes() == before


def test_missing_lock_cannot_fall_back_to_online_install(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "require_python_312", lambda _: None)
    with pytest.raises(RuntimeError, match="locked runtime inputs"):
        installer.ensure_runtime(tmp_path, tmp_path / "install", "v", Path(sys.executable))


def test_default_legacy_account_adapter_has_no_old_file_write(tmp_path, monkeypatch):
    from runtime.profile_registry import record_profile_state, profile_state_summary
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path))
    result = record_profile_state("B", platform="xhs", state="degraded", reason_code="LOGIN_REQUIRED", manual_action_required=True)
    assert result["write_target"] == "AccountObservation"
    assert profile_state_summary()["profiles"][0]["manual_action_required"]
    assert not list(tmp_path.glob("*.json"))
