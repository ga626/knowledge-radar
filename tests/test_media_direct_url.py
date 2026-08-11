from media_direct_url import (
    DirectMediaCandidate,
    build_direct_media_probe,
    provider_downloadability,
    redact_url,
    select_video_candidate,
    youtube_watch_url_candidate,
)


def test_redact_url_removes_query_tokens() -> None:
    raw = "https://cdn.example.test/path/video.m4s?token=secret&deadline=123"

    redacted = redact_url(raw)

    assert redacted["host"] == "cdn.example.test"
    assert redacted["path_suffix"].endswith("video.m4s")
    assert "secret" not in repr(redacted)
    assert "deadline" not in repr(redacted)


def test_select_video_candidate_prefers_video_format() -> None:
    info = {
        "formats": [
            {"url": "https://cdn.example.test/audio.m4s", "vcodec": "none", "height": None},
            {"url": "https://cdn.example.test/video.m4s", "vcodec": "avc1", "height": 360},
        ]
    }

    selected = select_video_candidate(info)

    assert selected["url"].endswith("video.m4s")
    assert selected["vcodec"] == "avc1"


def test_build_direct_media_probe_can_skip_reachability_without_download() -> None:
    candidate = DirectMediaCandidate(
        url="https://cdn.example.test/video.mp4?token=secret",
        platform="bilibili",
        extractor="unit-test",
        duration=120,
    )

    probe = build_direct_media_probe(
        {"status": "ok", "extractor": "unit-test", "duration": 120, "candidate": candidate},
        probe_reachability=False,
    )

    assert probe["schema"] == "knowledgeradar-direct-media/v1"
    assert probe["status"] == "ok"
    assert probe["candidates"][0]["redacted_url"]["host"] == "cdn.example.test"
    assert "secret" not in repr(probe)
    assert probe["reachability"]["status"] == "skipped"


def test_youtube_watch_url_is_not_marked_direct() -> None:
    result = youtube_watch_url_candidate("abc12345678")
    probe = build_direct_media_probe(result, probe_reachability=False)

    assert probe["status"] == "watch_url_only"
    assert probe["candidates"][0]["extractor"] == "watch_url"
    assert probe["candidates"][0]["metadata"]["direct"] is False
    assert "does not expose a direct playable video URL" in probe["reason"]


def test_bilibili_reachable_url_is_not_provider_downloadable() -> None:
    candidate = DirectMediaCandidate(
        url="https://upos-sz-mirror.example.test/video.m4s?deadline=1234567890",
        platform="bilibili",
        extractor="yt-dlp",
    )

    status = provider_downloadability(candidate, {"status": "reachable"})

    assert status["status"] == "provider_blocked"
    assert status["allow_native_media"] is False


def test_non_blocked_reachable_url_is_provider_downloadable() -> None:
    candidate = DirectMediaCandidate(
        url="https://media.w3.org/2010/05/sintel/trailer.mp4",
        platform="web",
        extractor="direct",
    )

    status = provider_downloadability(candidate, {"status": "reachable"})

    assert status["status"] == "provider_downloadable"
    assert status["allow_native_media"] is True
