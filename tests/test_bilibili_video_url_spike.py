import sys
from pathlib import Path


TOOLS_ROOT = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from bilibili_video_url_spike import redact_url, select_video_candidate  # noqa: E402


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
