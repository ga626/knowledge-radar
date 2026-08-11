from pathlib import Path

from detail_strategies import BilibiliDetailDeps, BilibiliDetailStrategy
from kr_core import DetailRequest


def test_bilibili_comment_filter_uses_media_cache(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KR_MEDIA_CACHE_DIR", str(tmp_path / "media_cache"))
    captured = {}

    def filter_comments(comments, output_dir, *args):
        captured["output_dir"] = output_dir
        return comments

    strategy = BilibiliDetailStrategy(
        BilibiliDetailDeps(
            extract_bvid=lambda url: "BVcomment",
            get_info=lambda bvid: {"title": "title", "desc": "", "duration": 1},
            transcribe=lambda *args, **kwargs: "",
            get_comments=lambda bvid: [{"user": "u", "content": "c"}],
            filter_comments=filter_comments,
            attach_routing=lambda url, result: result,
            routing_recommends_l2=lambda result: False,
            deep_analyze=lambda *args, **kwargs: {},
            direct_media_probe=lambda bvid, enabled: {},
            evidence_builder=lambda url, platform, result: None,
            data_dir=str(tmp_path / "legacy"),
        )
    )

    response = strategy.extract(
        DetailRequest(url="https://www.bilibili.com/video/BVcomment", enable_comment_filtering=True)
    )

    assert response.data["filtered_comments"] == [{"user": "u", "content": "c"}]
    assert str(tmp_path / "media_cache" / "comments" / "BVcomment") == captured["output_dir"]
