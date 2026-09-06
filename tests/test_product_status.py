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
    assert rows["content_intelligence"]["status"] == "optional"
    assert rows["accounts_browser"]["status"] == "needs_interaction"
    assert "diagnostics_privacy" not in rows
    assert rows["core_web"]["tool_count"] == 4
    assert "TAVILY_API_KEY" not in str(rows)


def test_console_guides_and_dashboard_never_expose_configuration_or_task_content(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("TAVILY_API_KEY=private-value\n", encoding="utf-8")
    monkeypatch.setattr(product_status, "public_snapshot", lambda: {"fields": [{"key": "TAVILY_API_KEY", "configured": True}]})
    monkeypatch.setattr(product_status, "public_provider_guides", lambda: [{"id": "tavily", "configured": True, "official_url": "https://example.test", "steps": ["step"]}])
    monkeypatch.setattr(
        product_status,
        "_dashboard_task_activity",
        lambda since: {
            "available": True,
            "completed": 2,
            "active": 1,
            "active_tasks": [{"label": "视频转写", "platform": "B 站", "status": "running", "status_label": "处理中", "updated_label": "刚刚更新", "duration_seconds": 3}],
            "recent_tasks": [{"label": "视频转写", "platform": "B 站", "status": "completed", "status_label": "已完成", "updated_label": "1 分钟前更新", "duration_seconds": 12}],
        },
    )
    monkeypatch.setattr(product_status, "_dashboard_trace_activity", lambda since: {"available": True, "successful": 3, "top_tools": [{"label": "kr_research", "count": 3}]})
    monkeypatch.setattr(product_status, "_dashboard_usage_activity", lambda since: {"available": True, "top_capabilities": [{"label": "vision", "count": 1}]})

    guide_snapshot = product_status.console_configuration_snapshot()
    dashboard = product_status.dashboard_snapshot()
    rendered = json.dumps({"guide_snapshot": guide_snapshot, "dashboard": dashboard}, ensure_ascii=False)

    assert guide_snapshot["providers"][0]["id"] == "tavily"
    assert dashboard["activity"]["tasks"]["active"] == 1
    assert dashboard["activity"]["tasks"]["recent_tasks"][0]["label"] == "视频转写"
    assert dashboard["next_action"]["view"] == "services"
    states = {row["id"]: row for row in dashboard["control_plane"]["capabilities"]}
    assert states["core_web"]["detail"] == "已接入"
    assert states["accounts_browser"]["detail"] == "需要登录"
    assert "private-value" not in rendered
    assert "target" not in rendered


def test_dashboard_task_activity_exposes_only_sanitized_task_progress(tmp_path, monkeypatch) -> None:
    database = tmp_path / "tasks.sqlite3"
    monkeypatch.setenv("KR_TASK_DB_PATH", str(database))
    connection = __import__("sqlite3").connect(database)
    connection.execute(
        """CREATE TABLE runtime_tasks (
        task_id TEXT PRIMARY KEY, task_type TEXT, platform TEXT, status TEXT,
        target TEXT, content_id TEXT, created_at REAL, updated_at REAL,
        started_at REAL, finished_at REAL, attempts INTEGER, max_attempts INTEGER,
        result_path TEXT, error TEXT, error_code TEXT, metadata_json TEXT)"""
    )
    now = time.time()
    connection.execute(
        "INSERT INTO runtime_tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("private-task-id", "bilibili_transcribe", "private-platform", "running", "https://private.example", "private-content", now - 12, now, now - 10, None, 1, 2, "C:/private", "private-error", "RuntimeError", "{}"),
    )
    connection.commit()
    connection.close()

    activity = product_status._dashboard_task_activity(now - 86400)
    rendered = json.dumps(activity, ensure_ascii=False)

    assert activity["active"] == 1
    assert activity["active_tasks"] == [{"label": "视频转写", "platform": "B 站", "status": "running", "status_label": "处理中", "updated_label": "刚刚更新", "duration_seconds": 10}]
    assert "private" not in rendered
    assert "target" not in rendered


def test_control_plane_never_claims_remote_provider_health() -> None:
    rows = product_status._control_plane_capabilities(
        [
            {"id": "core_web", "label": "核心网页研究", "status": "ready"},
            {"id": "accounts_browser", "label": "平台账号与浏览器", "status": "needs_interaction"},
        ],
        [{"id": "browser", "label": "Playwright Chromium", "status": "not_installed"}],
    )

    by_id = {row["id"]: row for row in rows}
    assert by_id["core_web"] == {"id": "core_web", "label": "核心网页研究", "state": "connected", "detail": "已接入"}
    assert by_id["accounts_browser"]["state"] == "manual"
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

    states = {row["id"]: row["status"] for row in optional}
    assert states["browser"] == "ready"
    assert states["xhs_bridge"] == "ready"
    assert {"media_downloader", "transcription_runtime", "transcription_model"}.issubset(states)
    assert str(data_root) not in json.dumps(diagnostic)
    assert "private bridge" not in json.dumps(diagnostic)


def test_local_component_catalog_groups_everything_by_user_goal(tmp_path, monkeypatch) -> None:
    data_root = tmp_path / "private-data"
    model_cache = data_root / "models" / "whisper"
    model_cache.mkdir(parents=True)
    (model_cache / "model.bin").write_bytes(b"cached")
    monkeypatch.setenv("KR_DATA_ROOT", str(data_root))
    monkeypatch.setenv("KR_STATE_DIR", str(data_root))
    monkeypatch.setattr(product_status, "resolve_managed_chrome", lambda: None)
    monkeypatch.setattr(product_status.shutil, "which", lambda name: "node.exe" if name == "node" else None)

    catalog = product_status.local_component_catalog()
    rendered = json.dumps(catalog, ensure_ascii=False)

    groups = {group["id"]: group for group in catalog["groups"]}
    all_items = {item["id"]: item for group in groups.values() for item in group["items"]}

    assert set(groups) == {"web_automation", "platform_sessions", "media_understanding", "experimental"}
    assert {"browser"} == {item["id"] for item in groups["web_automation"]["items"]}
    assert {"chrome", "node", "xhs_bridge"} == {item["id"] for item in groups["platform_sessions"]["items"]}
    assert {"media_downloader", "transcription_runtime", "transcription_model", "ffmpeg"} == {item["id"] for item in groups["media_understanding"]["items"]}
    assert {"camoufox", "funasr", "sherpa_onnx"} == {item["id"] for item in groups["experimental"]["items"]}
    assert all_items["transcription_model"]["status"] == "ready"
    assert all_items["node"]["install_mode"] == "guided"
    assert all_items["transcription_runtime"]["install_mode"] == "managed"
    assert str(data_root) not in rendered


def test_optional_media_components_are_not_downloaded_with_the_plugin_body() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    dependencies = pyproject.read_text(encoding="utf-8")
    installer = Path(__file__).resolve().parents[1] / "scripts" / "product_install.py"
    installer_source = installer.read_text(encoding="utf-8")

    assert '"faster-whisper>=1.1,<2.0"' not in dependencies
    assert '"yt-dlp>=2024.8.6"' not in dependencies
    assert '"transcription_runtime"' in installer_source
    assert '"transcription_model"' in installer_source


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
