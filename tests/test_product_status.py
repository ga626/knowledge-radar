import json
import os
from pathlib import Path
import time

from onboarding import product_status
from runtime.media_cache import media_cache_subdir, record_media_cache_entry


def test_capability_packs_report_configuration_without_values() -> None:
    snapshot = {
        "fields": [
            {"key": "TAVILY_API_KEY", "configured": True},
            {"key": "DASHSCOPE_API_KEY", "configured": False},
        ]
    }

    rows = {row["id"]: row for row in product_status.capability_packs(snapshot)}

    assert rows["core_web"]["status"] == "ready"
    assert rows["video_media"]["status"] == "needs_setup"
    assert "TAVILY_API_KEY" not in str(rows)


def test_console_guides_and_dashboard_never_expose_configuration_or_task_content(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("TAVILY_API_KEY=private-value\n", encoding="utf-8")
    monkeypatch.setattr(product_status, "public_snapshot", lambda: {"fields": [{"key": "TAVILY_API_KEY", "configured": True}]})
    monkeypatch.setattr(product_status, "public_provider_guides", lambda: [{"id": "tavily", "configured": True, "official_url": "https://example.test", "steps": ["step"]}])
    monkeypatch.setattr(product_status, "_dashboard_task_activity", lambda since: {"available": True, "completed": 2, "active": 1})
    monkeypatch.setattr(product_status, "_dashboard_trace_activity", lambda since: {"available": True, "successful": 3, "top_tools": [{"label": "kr_research", "count": 3}]})
    monkeypatch.setattr(product_status, "_dashboard_usage_activity", lambda since: {"available": True, "top_capabilities": [{"label": "vision", "count": 1}]})

    guide_snapshot = product_status.console_configuration_snapshot()
    dashboard = product_status.dashboard_snapshot()
    rendered = json.dumps({"guide_snapshot": guide_snapshot, "dashboard": dashboard}, ensure_ascii=False)

    assert guide_snapshot["providers"][0]["id"] == "tavily"
    assert dashboard["activity"]["tasks"] == {"available": True, "completed": 2, "active": 1}
    assert dashboard["next_action"]["view"] == "services"
    states = {row["id"]: row for row in dashboard["control_plane"]["capabilities"]}
    assert states["core_web"]["detail"] == "已接入"
    assert states["login_platforms"]["detail"] == "需要登录"
    assert "private-value" not in rendered
    assert "target" not in rendered


def test_control_plane_never_claims_remote_provider_health() -> None:
    rows = product_status._control_plane_capabilities(
        [
            {"id": "core_web", "label": "核心网页研究", "status": "ready"},
            {"id": "login_platforms", "label": "登录平台与招聘", "status": "needs_setup"},
        ],
        [{"id": "browser", "label": "Playwright Chromium", "status": "not_installed"}],
    )

    by_id = {row["id"]: row for row in rows}
    assert by_id["core_web"] == {"id": "core_web", "label": "核心网页研究", "state": "connected", "detail": "已接入"}
    assert by_id["login_platforms"]["state"] == "manual"
    assert by_id["browser"]["detail"] == "尚未安装"


def test_storage_summary_does_not_scan_source_checkout_without_product_data_root(monkeypatch) -> None:
    monkeypatch.delenv("KR_DATA_ROOT", raising=False)

    summary = product_status.storage_summary()

    assert summary["available"] is False
    assert summary["categories"] == []
    assert product_status.expired_media_cleanup(apply=True)["status"] == "SKIPPED"


def test_installation_summary_is_sanitized_and_only_uses_the_install_root(tmp_path, monkeypatch) -> None:
    install_root = tmp_path / "install"
    data_root = tmp_path / "private-data"
    data_root.mkdir(parents=True)
    (install_root / "backup").mkdir(parents=True)
    (install_root / "backup" / "active.previous.json").write_text("{}", encoding="utf-8")
    (install_root / "active.json").write_text(
        '{"schema":"knowledgeradar-active-install/v1","version":"0.1.0a8","channel":"stable","data_root":"'
        + str(data_root).replace("\\", "\\\\")
        + '","data_root_hash":"private-hash"}',
        encoding="utf-8",
    )
    monkeypatch.setenv("KR_INSTALL_ROOT", str(install_root))

    summary = product_status.installation_summary()

    assert summary["available"] is True
    assert summary["version"] == "0.1.0a8"
    assert summary["data_root_present"] is True
    assert str(data_root) not in str(summary)


def test_storage_summary_uses_manifest_and_does_not_double_count_media_cache(tmp_path, monkeypatch) -> None:
    data_root = tmp_path / "data"
    cache_root = data_root / "state" / "media_cache"
    (data_root / "state").mkdir(parents=True)
    (data_root / "state" / "task.json").write_bytes(b"task")
    cache_root.mkdir()
    (cache_root / "cached.mp4").write_bytes(b"media")
    monkeypatch.setenv("KR_DATA_ROOT", str(data_root))
    monkeypatch.setenv("KR_MEDIA_CACHE_DIR", str(cache_root))
    monkeypatch.setenv("KR_PROJECT_ROOT", str(Path(__file__).resolve().parents[1]))

    summary = product_status.storage_summary()
    rows = {row["id"]: row for row in summary["categories"]}

    assert rows["state"]["bytes"] == len(b"task")
    assert rows["media_cache"]["bytes"] == len(b"media")
    assert summary["total_bytes"] == len(b"task") + len(b"media")


def test_product_media_cleanup_quarantines_only_manifest_known_expired_files(tmp_path, monkeypatch) -> None:
    data_root = tmp_path / "data"
    cache_root = data_root / "state" / "media_cache"
    monkeypatch.setenv("KR_DATA_ROOT", str(data_root))
    monkeypatch.setenv("KR_MEDIA_CACHE_DIR", str(cache_root))
    known = media_cache_subdir("video", content_id="known") / "old.mp4"
    unknown = media_cache_subdir("video", content_id="unknown") / "old.mp4"
    known.write_bytes(b"known")
    unknown.write_bytes(b"unknown")
    old = time.time() - 3600
    os.utime(known, (old, old))
    os.utime(unknown, (old, old))
    record_media_cache_entry(known, kind="video", ttl_seconds=10)

    plan = product_status.expired_media_cleanup(apply=False)
    applied = product_status.expired_media_cleanup(apply=True)

    assert plan["status"] == "PLAN"
    assert plan["expired_file_count"] == 1
    assert applied["status"] == "QUARANTINED"
    assert not known.exists()
    assert unknown.exists()
    assert any((data_root / "quarantine").rglob("old.mp4"))


def test_optional_capability_and_diagnostic_status_never_expose_paths_or_values(tmp_path, monkeypatch) -> None:
    data_root = tmp_path / "private-data"
    bridge = data_root / "capabilities" / "xhs-bridge"
    bridge.mkdir(parents=True)
    (bridge / "xhs_mcp_bridge.cjs").write_text("private bridge", encoding="utf-8")
    (data_root / "playwright").mkdir()
    (data_root / "state").mkdir(exist_ok=True)
    (data_root / "state" / "capabilities.json").write_text(
        '{"schema":"knowledgeradar-capability-state/v1","capabilities":{"browser":{"status":"APPLIED"},"xhs_bridge":{"status":"APPLIED"}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("KR_DATA_ROOT", str(data_root))
    monkeypatch.setattr(product_status, "installation_summary", lambda: {"available": True, "message": "ok"})
    monkeypatch.setattr(product_status, "public_snapshot", lambda: {"fields": [{"key": "TAVILY_API_KEY", "configured": True}]})

    optional = product_status.optional_capabilities()
    diagnostic = product_status.diagnostic_snapshot()

    assert {row["id"]: row["status"] for row in optional} == {"browser": "ready", "xhs_bridge": "ready"}
    assert str(data_root) not in json.dumps(diagnostic)
    assert "private bridge" not in json.dumps(diagnostic)


def test_data_move_console_plan_is_sanitized(monkeypatch) -> None:
    monkeypatch.setattr(
        product_status,
        "_run_product_installer",
        lambda arguments, *, timeout: {
            "status": "PLAN",
            "source": {"files": 4, "bytes": 9, "path_hash": "private"},
            "target": {"exists": False, "free_bytes": 20},
            "required_free_bytes": 18,
            "browser_lock_relative_paths": ["browser_data/SingletonLock"],
            "confirmation_token": "plan-token",
        },
    )

    plan = product_status.data_root_move_console_plan("D:\\new-data")

    assert plan["source"] == {"files": 4, "bytes": 9}
    assert plan["browser_lock_count"] == 1
    assert "path_hash" not in json.dumps(plan)
