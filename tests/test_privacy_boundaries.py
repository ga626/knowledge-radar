import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.profile_registry import (
    profile_registry_internal,
    profile_registry_summary,
    select_main_chain_profile,
)
from runtime.redaction import RedactingLogFilter, redact_text, redact_url
from scripts.verify_package_integrity import check_package_content


def test_public_profile_summary_excludes_private_registry_fields(tmp_path, monkeypatch):
    profile_dir = tmp_path / "private-profile"
    profile_dir.mkdir()
    registry_path = tmp_path / "private-registry.json"
    state_path = tmp_path / "private-state.json"
    registry_path.write_text(
        json.dumps(
            {
                "version": "1",
                "accounts": [{"platform": "xiaohongshu", "account_id": "personal-account-42"}],
                "profiles": [
                    {
                        "platform": "xiaohongshu",
                        "profile_id": "private-profile-id",
                        "profile_dir": str(profile_dir),
                        "account_slot": "owner-slot",
                        "main_chain_allowed": True,
                        "notes": ["private operator note"],
                    }
                ],
                "bindings": [{"platform": "xiaohongshu", "identity": "personal-binding"}],
                "policy": {"default_mode": "safe_auto"},
            }
        ),
        encoding="utf-8",
    )
    state_path.write_text(
        json.dumps({"profiles": [{"profile_id": "private-profile-id", "notes": ["private runtime note"]}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("KR_PROFILE_STATE_PATH", str(state_path))

    internal = profile_registry_internal(str(registry_path))
    public = profile_registry_summary(str(registry_path))
    public_text = json.dumps(public, ensure_ascii=False)

    assert internal["raw"]["accounts"][0]["account_id"] == "personal-account-42"
    assert select_main_chain_profile("xiaohongshu", str(registry_path))["profile_dir"] == str(profile_dir)
    for private_value in (str(registry_path), str(state_path), str(profile_dir), "personal-account-42", "personal-binding", "private operator note", "private runtime note"):
        assert private_value not in public_text
    assert "raw" not in public
    assert "registry_path" not in public
    assert public["profiles"][0]["profile_dir_hash"]


def test_redaction_removes_url_queries_credentials_and_local_paths():
    secret_url = "https://example.test/path?token=private-token&x=1#fragment"
    assert redact_url(secret_url) == "https://example.test/path"
    local_path = "C:" + "\\Users" + "\\owner\\secret.txt"
    redacted = redact_text("Authorization: Bearer private-token url=" + secret_url + " " + local_path)
    assert "private-token" not in redacted
    assert local_path not in redacted
    assert "https://example.test/path" in redacted

    record = logging.LogRecord("test", logging.INFO, __file__, 1, "cookie=%s", ("private-cookie",), None)
    RedactingLogFilter().filter(record)
    assert "private-cookie" not in record.getMessage()


def test_package_content_scan_reports_kind_but_not_secret_value(tmp_path):
    package = tmp_path / "KnowledgeRadar"
    package.mkdir()
    (package / "package-provenance.json").write_text(
        json.dumps({"source" + "_root": "C:" + "/" + "Users/private"}), encoding="utf-8"
    )
    (package / "config.env").write_text("API_KEY=super-secret-value-123", encoding="utf-8")
    issues = check_package_content(package)
    assert any("provenance_source_root" in issue for issue in issues)
    assert any("credential_assignment" in issue for issue in issues)
    assert all("super-secret-value-123" not in issue for issue in issues)
