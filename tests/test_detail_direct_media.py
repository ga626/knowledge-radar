from detail_strategies import BilibiliDetailDeps, BilibiliDetailStrategy, YouTubeDetailDeps, YouTubeDetailStrategy
from kr_core import DetailRequest, EvidenceItem


def _evidence(url: str, platform: str, data: dict) -> EvidenceItem:
    return EvidenceItem(source_url=url, source_platform=platform, summary=data.get("title", ""))


def test_bilibili_detail_skips_direct_media_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("KR_DIRECT_MEDIA_PROBE_ON_DETAIL", raising=False)
    calls = {"direct": 0}

    strategy = BilibiliDetailStrategy(
        BilibiliDetailDeps(
            extract_bvid=lambda url: "BVtest",
            get_info=lambda bvid: {"title": "test", "desc": "", "duration": 60},
            transcribe=lambda bvid, session_id="": "transcript",
            get_comments=lambda bvid: [],
            filter_comments=lambda comments, data_dir: [],
            attach_routing=lambda url, result: result,
            routing_recommends_l2=lambda result: False,
            deep_analyze=lambda bvid, result, session_id="": {},
            direct_media_probe=lambda bvid, enabled: calls.__setitem__("direct", calls["direct"] + 1) or {},
            evidence_builder=_evidence,
            data_dir=str(tmp_path),
        )
    )

    response = strategy.extract(DetailRequest(url="https://www.bilibili.com/video/BVtest"))

    assert calls["direct"] == 0
    assert response.data["direct_media"]["status"] == "skipped"


def test_bilibili_detail_auto_multimodal_attaches_direct_media(tmp_path) -> None:
    strategy = BilibiliDetailStrategy(
        BilibiliDetailDeps(
            extract_bvid=lambda url: "BVtest",
            get_info=lambda bvid: {"title": "test", "desc": "", "duration": 60},
            transcribe=lambda bvid, session_id="": "transcript",
            get_comments=lambda bvid: [],
            filter_comments=lambda comments, data_dir: [],
            attach_routing=lambda url, result: result,
            routing_recommends_l2=lambda result: False,
            deep_analyze=lambda bvid, result, session_id="": {},
            direct_media_probe=lambda bvid, enabled: {
                "schema": "knowledgeradar-direct-media/v1",
                "status": "ok",
                "candidates": [{"redacted_url": {"host": "cdn.example.test"}}],
                "reachability": {"status": "reachable"},
            },
            evidence_builder=_evidence,
            data_dir=str(tmp_path),
        )
    )

    response = strategy.extract(DetailRequest(url="https://www.bilibili.com/video/BVtest", auto_multimodal=True))

    assert response.data["direct_media"]["status"] == "ok"
    assert response.data["direct_media"]["reachability"]["status"] == "reachable"


def test_youtube_detail_exposes_watch_url_only_direct_media_status() -> None:
    strategy = YouTubeDetailStrategy(
        YouTubeDetailDeps(
            extract_video_id=lambda url: "abc12345678",
            get_detail=lambda video_id: {"title": "yt", "desc": "", "url": f"https://www.youtube.com/watch?v={video_id}"},
            attach_routing=lambda url, result: result,
            routing_recommends_l2=lambda result: False,
            deep_analyze=lambda video_id, result, session_id="": {},
            direct_media_probe=lambda video_id, enabled: {
                "schema": "knowledgeradar-direct-media/v1",
                "status": "watch_url_only" if enabled else "skipped",
                "reason": "watch URL is not direct",
            },
            evidence_builder=_evidence,
        )
    )

    response = strategy.extract(DetailRequest(url="https://www.youtube.com/watch?v=abc12345678", auto_multimodal=True))

    assert response.data["direct_media"]["status"] == "watch_url_only"
    assert "not direct" in response.data["direct_media"]["reason"]
