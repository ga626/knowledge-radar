from pathlib import Path

import understanding.image as image_mod
from runtime.tasks import TaskStore


def test_xhs_ocr_registers_scope_metadata(monkeypatch, tmp_path: Path) -> None:
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    monkeypatch.setattr(image_mod, "get_task_store", lambda: store)

    class _Response:
        content = b"image-bytes"

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(image_mod.httpx, "get", lambda *args, **kwargs: _Response())
    monkeypatch.setattr(image_mod, "image_bytes_to_base64", lambda data: "base64")
    monkeypatch.setattr(
        image_mod,
        "call_multimodal_models",
        lambda **kwargs: ('{"text":"图中文字","items":[{"text":"图中文字","score":1.0}],"visual_summary":"摘要"}', "test-model"),
    )

    result = image_mod.ocr_first_xhs_image(
        ["https://example.com/xhs-image.jpg"],
        task_metadata={
            "server_run_id": "server-1",
            "work_scope_id": "work-1",
            "task_scope_id": "task-1",
            "source_url": "https://www.xiaohongshu.com/explore/0123456789abcdef01234567",
            "content_id": "0123456789abcdef01234567",
            "blocks_final_report": True,
        },
    )

    assert result["status"] == "ok"
    task = store.get_task(result["task_id"])
    assert task is not None
    assert task["server_run_id"] == "server-1"
    assert task["work_scope_id"] == "work-1"
    assert task["task_scope_id"] == "task-1"
    assert task["content_id"] == "0123456789abcdef01234567"
    assert task["metadata"]["blocks_final_report"] is True
    assert task["metadata"]["result_reread_tool"] == "get_content_detail"


def test_xhs_ocr_processes_multiple_images_with_limit(monkeypatch, tmp_path: Path) -> None:
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    monkeypatch.setattr(image_mod, "get_task_store", lambda: store)
    monkeypatch.setenv("KR_XHS_OCR_MAX_IMAGES", "2")
    requested_urls = []

    class _Response:
        content = b"image-bytes"

        def raise_for_status(self) -> None:
            return None

    def fake_get(url, *args, **kwargs):
        requested_urls.append(url)
        return _Response()

    def fake_call_multimodal_models(**kwargs):
        assert len(kwargs["images_base64"]) == 2
        return ('{"text":"第一张\\n第二张","items":[],"visual_summary":"两张图摘要"}', "test-model")

    monkeypatch.setattr(image_mod.httpx, "get", fake_get)
    monkeypatch.setattr(image_mod, "image_bytes_to_base64", lambda data: "base64")
    monkeypatch.setattr(image_mod, "call_multimodal_models", fake_call_multimodal_models)

    result = image_mod.ocr_first_xhs_image(
        [
            "https://example.com/one.jpg",
            "https://example.com/two.jpg",
            "https://example.com/three.jpg",
        ],
        task_metadata={"source_url": "https://www.xiaohongshu.com/explore/0123456789abcdef01234567"},
    )

    assert result["status"] == "ok"
    assert result["images_processed"] == 2
    assert requested_urls == ["https://example.com/one.jpg", "https://example.com/two.jpg"]
    task = store.get_task(result["task_id"])
    assert task is not None
    assert task["metadata"]["image_count"] == 2


def test_xhs_ocr_skips_obvious_avatar_images(monkeypatch, tmp_path: Path) -> None:
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    monkeypatch.setattr(image_mod, "get_task_store", lambda: store)
    monkeypatch.setenv("KR_XHS_OCR_MAX_IMAGES", "2")
    requested_urls = []

    class _Response:
        content = b"image-bytes"

        def raise_for_status(self) -> None:
            return None

    def fake_get(url, *args, **kwargs):
        requested_urls.append(url)
        return _Response()

    monkeypatch.setattr(image_mod.httpx, "get", fake_get)
    monkeypatch.setattr(image_mod, "image_bytes_to_base64", lambda data: "base64")
    monkeypatch.setattr(
        image_mod,
        "call_multimodal_models",
        lambda **kwargs: ('{"text":"正文图","items":[],"visual_summary":"正文截图"}', "test-model"),
    )

    result = image_mod.ocr_first_xhs_image(
        [
            "https://cdn.example.com/avatar/user.jpg",
            "https://sns-img-qc.xhscdn.com/content-note-one.jpg",
            "https://cdn.example.com/icon/logo.png",
            "https://sns-img-qc.xhscdn.com/content-note-two.jpg",
        ],
        task_metadata={"source_url": "https://www.xiaohongshu.com/explore/0123456789abcdef01234567"},
    )

    assert result["status"] == "ok"
    assert requested_urls == [
        "https://sns-img-qc.xhscdn.com/content-note-one.jpg",
        "https://sns-img-qc.xhscdn.com/content-note-two.jpg",
    ]
    assert result["image_selection"]["selected_indexes"] == [1, 3]


def test_xhs_ocr_prefers_sns_webpic_over_platform_assets(monkeypatch, tmp_path: Path) -> None:
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    monkeypatch.setattr(image_mod, "get_task_store", lambda: store)
    monkeypatch.setenv("KR_XHS_OCR_MAX_IMAGES", "2")
    requested_urls = []

    class _Response:
        content = b"image-bytes"

        def raise_for_status(self) -> None:
            return None

    def fake_get(url, *args, **kwargs):
        requested_urls.append(url)
        return _Response()

    monkeypatch.setattr(image_mod.httpx, "get", fake_get)
    monkeypatch.setattr(image_mod, "image_bytes_to_base64", lambda data: "base64")
    monkeypatch.setattr(
        image_mod,
        "call_multimodal_models",
        lambda **kwargs: ('{"text":"内容截图","items":[],"visual_summary":"正文图摘要"}', "test-model"),
    )

    content_one = "https://sns-webpic-qc.xhscdn.com/20260614/spectrum/content-one!nd_dft_wlteh_webp_3"
    content_two = "https://sns-webpic-qc.xhscdn.com/20260614/spectrum/content-two!nd_dft_wlteh_webp_3"

    result = image_mod.ocr_first_xhs_image(
        [
            "https://fe-platform.xhscdn.com/platform/asset-one.webp",
            "https://sns-avatar-qc.xhscdn.com/avatar/user.jpg",
            content_one,
            "https://fe-platform.xhscdn.com/platform/asset-two.webp",
            content_two,
        ],
        task_metadata={"source_url": "https://www.xiaohongshu.com/explore/0123456789abcdef01234567"},
    )

    assert result["status"] == "ok"
    assert requested_urls == [content_one, content_two]
    assert result["image_selection"]["selected_indexes"] == [2, 4]
    preview = result["image_selection"]["candidates_preview"]
    rejected = {item["rejected_reason"] for item in preview if "rejected_reason" in item}
    assert "platform_asset" in rejected
    assert "avatar_asset" in rejected


def test_xhs_ocr_parses_fenced_json_response(monkeypatch, tmp_path: Path) -> None:
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    monkeypatch.setattr(image_mod, "get_task_store", lambda: store)

    class _Response:
        content = b"image-bytes"

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(image_mod.httpx, "get", lambda *args, **kwargs: _Response())
    monkeypatch.setattr(image_mod, "image_bytes_to_base64", lambda data: "base64")
    monkeypatch.setattr(
        image_mod,
        "call_multimodal_models",
        lambda **kwargs: (
            '```json\n{"text":"读图文字","items":[{"text":"读图文字","score":0.9}],"visual_summary":"结构化摘要"}\n```',
            "test-model",
        ),
    )

    result = image_mod.ocr_first_xhs_image(
        ["https://sns-webpic-qc.xhscdn.com/20260614/spectrum/content!nd_dft_wlteh_webp_3"],
        task_metadata={"source_url": "https://www.xiaohongshu.com/explore/0123456789abcdef01234567"},
    )

    assert result["status"] == "ok"
    assert result["text"] == "读图文字"
    assert result["items"] == [{"text": "读图文字", "score": 0.9}]
    assert result["visual_summary"] == "结构化摘要"
