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
