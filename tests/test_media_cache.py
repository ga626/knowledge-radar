import os
import time

from runtime.media_cache import cleanup_expired_media_cache, media_cache_subdir, record_media_cache_entry
from runtime.paths import runtime_media_cache_dir


def test_media_cache_root_uses_env_and_not_legacy_data(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KR_MEDIA_CACHE_DIR", str(tmp_path / "media_cache"))
    monkeypatch.delenv("KR_RUNTIME_MEDIA_DIR", raising=False)

    root = runtime_media_cache_dir()
    subdir = media_cache_subdir("audio", content_id="BV1example")

    assert root == tmp_path / "media_cache"
    assert "data" not in root.parts
    assert subdir.exists()
    assert subdir.parent.name == "audio"


def test_media_cache_records_manifest_and_cleans_expired_files(monkeypatch, tmp_path) -> None:
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("KR_MEDIA_CACHE_DIR", str(cache_root))

    expired = media_cache_subdir("video", content_id="BV1old") / "old.mp4"
    fresh = media_cache_subdir("video", content_id="BV1new") / "new.mp4"
    expired.write_bytes(b"old media")
    fresh.write_bytes(b"new media")

    now = time.time()
    old_mtime = now - 100
    os.utime(expired, (old_mtime, old_mtime))
    os.utime(fresh, (now, now))
    record_media_cache_entry(expired, kind="video", content_id="BV1old", task_id="task-old", ttl_seconds=10)
    record_media_cache_entry(fresh, kind="video", content_id="BV1new", task_id="task-new", ttl_seconds=3600)

    result = cleanup_expired_media_cache(root=cache_root, now=now)

    assert str(expired) in result["deleted"]
    assert not expired.exists()
    assert fresh.exists()
    assert not result["errors"]
    assert (cache_root / "manifest.jsonl").exists()

