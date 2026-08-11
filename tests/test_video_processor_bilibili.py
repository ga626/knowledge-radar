from pathlib import Path

import pytest

import video_processor


def test_bilibili_video_download_uses_browser_headers_and_proxy(monkeypatch, tmp_path: Path) -> None:
    captured = {}

    class FakeYoutubeDL:
        def __init__(self, opts):
            captured.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, url, download=True):
            assert url == "https://www.bilibili.com/video/BV1ELGY6sExb"
            assert download is True
            (tmp_path / "BV1ELGY6sExb_video.mp4").write_bytes(b"0" * 2048)
            return {}

    monkeypatch.setattr(video_processor, "YoutubeDL", FakeYoutubeDL, raising=False)
    monkeypatch.setattr(video_processor, "get_yt_dlp_proxy", lambda: "http://127.0.0.1:7897")
    monkeypatch.setattr(video_processor, "record_media_cache_entry", lambda *args, **kwargs: None)
    monkeypatch.setitem(__import__("sys").modules, "yt_dlp", type("M", (), {"YoutubeDL": FakeYoutubeDL}))

    out = video_processor._download_bilibili_video("BV1ELGY6sExb", str(tmp_path))

    assert out.endswith("BV1ELGY6sExb_video.mp4")
    assert captured["noplaylist"] is True
    assert captured["playlist_items"] == "1"
    assert captured["proxy"] == "http://127.0.0.1:7897"
    assert captured["http_headers"]["Referer"] == "https://www.bilibili.com/"
    assert "Chrome" in captured["http_headers"]["User-Agent"]
