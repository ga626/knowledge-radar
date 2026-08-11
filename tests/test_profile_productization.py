import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_profile_registry_example_uses_relative_profile_dirs():
    data = json.loads((ROOT / "config" / "profile_registry.example.json").read_text(encoding="utf-8-sig"))

    profile_dirs = [str(row.get("profile_dir") or "") for row in data.get("profiles", [])]
    cnki_profiles = [row for row in data.get("profiles", []) if row.get("platform") == "cnki"]
    academic_manual_platforms = {
        "cnki",
        "vip_oa",
        "coaj",
        "ucdrs",
        "calis_thesis",
        "nstrs",
        "pubscholar",
        "socolar",
    }
    profiles_by_platform = {row.get("platform"): row for row in data.get("profiles", [])}

    assert profile_dirs
    assert all(not Path(value).is_absolute() for value in profile_dirs)
    assert all(value.startswith("local/profiles/") for value in profile_dirs)
    assert len(cnki_profiles) == 1
    assert cnki_profiles[0]["main_chain_allowed"] is False
    assert cnki_profiles[0]["launch_policy"] == "explicit_probe_only"
    assert "authorized browser only" in cnki_profiles[0]["notes"][0]
    assert academic_manual_platforms <= set(profiles_by_platform)
    for platform in academic_manual_platforms:
        profile = profiles_by_platform[platform]
        assert profile["main_chain_allowed"] is False
        assert profile["auto_switch"] == "disabled"
        assert profile["launch_policy"] == "explicit_probe_only"
        assert profile["profile_dir"].startswith(f"local/profiles/{platform}/")


def test_setup_accounts_script_is_windows_first_template():
    script = (ROOT / "scripts" / "setup_accounts.bat").read_text(encoding="utf-8")

    assert "profile_registry.example.json" in script
    assert "local\\profiles" in script
    assert "不会复制" in script
    assert "cookies" in script
    assert "browser locks" in script


def test_academic_login_profile_launcher_dry_run():
    script = ROOT / "scripts" / "open_academic_login_profiles.py"
    text = script.read_text(encoding="utf-8")

    assert "LOGIN_TARGETS" in text
    assert "browser_data" in text or "browser_data_dir" in text
    assert "--dry-run" in text
    assert "request_browser_interaction" in text
    assert "subprocess.Popen" not in text
    assert "vip_oa" in text
    assert "pubscholar" in text
    assert "socolar" in text
