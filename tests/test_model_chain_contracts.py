"""模型职责迁移、文件分流和跨类资源互斥的产品边界。"""

import json
import threading
from pathlib import Path

import pytest

from runtime import optional_media, resource_concurrency
from runtime.leases import RuntimeLeaseCoordinator
from runtime.resource_concurrency import acquire_resource


def request(path):
    return {"input_path": str(path), "approved_input_root": str(path.parent),
            "input_sha256": optional_media.file_sha(path), "component": "auto"}


def text_pdf(path, *, blank_last=False):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    for index in range(2):
        page = writer.add_blank_page(300, 300)
        if blank_last and index == 1:
            continue
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject(
            {NameObject("/F1"): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 10 100 Td (Page {index+1} END-27) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)


def test_mixed_pdf_cannot_silently_drop_an_image_page(tmp_path):
    path = tmp_path / "source.pdf"
    text_pdf(path)
    assert optional_media.select_component(request(path)) == "pypdf"
    text_pdf(path, blank_last=True)
    assert optional_media.select_component(request(path)) == "paddleocr-vl-1.6"
    forged = request(path)
    forged["input_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="input_identity_mismatch"):
        optional_media.select_component(forged)


def test_office_and_audio_have_one_route_without_loading_models(tmp_path):
    for suffix, selected in [("docx", "docling"), ("wav", "qwen3-asr-0.6b"),
                             ("png", "paddleocr-vl-1.6")]:
        source = tmp_path / ("source." + suffix)
        source.write_bytes(b"routing-only")
        assert optional_media.select_component(request(source)) == selected


def test_ffmpeg_absolute_path_reaches_worker_with_empty_path(tmp_path, monkeypatch):
    source = tmp_path / "source.wav"
    source.write_bytes(b"input")
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("KR_OPTIONAL_COMPONENT_RECEIPTS_DIR", str(tmp_path / "components"))
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path / "state"))
    from runtime import dependency_preflight
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"managed executable identity")
    monkeypatch.setattr(dependency_preflight, "_ffmpeg_path", lambda: str(ffmpeg))
    monkeypatch.setattr(optional_media, "validate_receipt", lambda component: {
        "python": "isolated-python", "model_root": str(tmp_path), "package_versions": {"transformers": "5.20.2"}})
    captured = {}

    def worker(command, **kwargs):
        captured.update(command=command, request=json.loads(kwargs["input"]), env=kwargs["env"])
        raise RuntimeError("stop_before_inference")

    monkeypatch.setattr(optional_media, "isolated_worker_run", worker)
    with pytest.raises(RuntimeError, match="stop_before_inference"):
        optional_media.run_optional(request(source))
    assert captured["request"]["ffmpeg_exe"] == str(ffmpeg)
    assert captured["env"]["HF_HUB_OFFLINE"] == "1"
    assert captured["env"]["OMP_NUM_THREADS"] == "4"
    assert "-I" in captured["command"] and "utf8" in captured["command"]


def test_asr_and_document_share_a_slot_and_release_after_error(tmp_path, monkeypatch):
    coordinator = RuntimeLeaseCoordinator(tmp_path / "leases.sqlite3")
    monkeypatch.setattr(resource_concurrency, "get_runtime_lease_coordinator", lambda: coordinator)
    entered = threading.Event()

    def document():
        with acquire_resource("local_heavy"):
            entered.set()

    with pytest.raises(ValueError):
        with acquire_resource("asr_cpu"):
            thread = threading.Thread(target=document)
            thread.start()
            assert not entered.wait(0.3)
            raise ValueError("model_failed")
    thread.join(timeout=5)
    assert entered.is_set() and not thread.is_alive()
    assert coordinator.summary()["counts"].get("held", 0) == 0


def test_model_upgrade_keeps_credentials_and_is_idempotent(tmp_path):
    import importlib.util
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("model_migration_installer", root / "scripts/product_install.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    path = tmp_path / "config/runtime.env"
    path.parent.mkdir()
    before = b'DASHSCOPE_API_KEY="private-key"\nTAVILY_API_KEY=search-key\nKR_ASR_MODELS=old\nKR_FRAME_VISION_MODELS=old\nKR_COMMENT_FILTER_MODELS=old\n'
    path.write_bytes(before)
    receipt = installer.migrate_model_configuration(tmp_path, root)
    after = path.read_text(encoding="utf-8")
    assert 'DASHSCOPE_API_KEY="private-key"' in after
    assert "TAVILY_API_KEY=search-key" in after
    assert "KR_COMMENT_FILTER_MODELS" not in after
    assert "KR_ASR_MODELS=local:qwen3-asr/0.6b-hf" in after
    assert "KR_FRAME_VISION_MODELS=bailian:qwen3.8-omni-flash" in after
    assert "private-key" not in json.dumps(receipt)
    assert (tmp_path / "receipts" / receipt["backup_name"]).read_bytes() == before
    assert installer.migrate_model_configuration(tmp_path, root)["status"] == "UNCHANGED"
    path.write_text(after.replace('private-key', 'new-private-key').replace(
        'KR_FRAME_VISION_MODELS=bailian:qwen3.8-omni-flash', 'KR_FRAME_VISION_MODELS=user-choice'), encoding="utf-8")
    restored = installer.restore_model_configuration(tmp_path, receipt)
    rolled_back = path.read_text(encoding="utf-8")
    assert 'DASHSCOPE_API_KEY="new-private-key"' in rolled_back
    assert 'KR_ASR_MODELS=old' in rolled_back
    assert 'KR_COMMENT_FILTER_MODELS=old' in rolled_back
    assert 'KR_FRAME_VISION_MODELS=user-choice' in rolled_back
    assert restored["preserved_user_keys"] == ["KR_FRAME_VISION_MODELS"]


def test_uninstall_owns_only_component_files_and_keeps_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("KR_OPTIONAL_COMPONENT_RECEIPTS_DIR", str(tmp_path / "components"))
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path / "state"))
    base = optional_media.receipt_root()
    env = base / "environments/docling"
    env.mkdir(parents=True)
    (env / "owned.txt").write_text("owned")
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("retained")
    optional_media.uninstall_component("docling")
    assert not env.exists() and evidence.read_text() == "retained"
    with pytest.raises(ValueError):
        optional_media.uninstall_component("../evidence.txt", purge_downloads=True)


def test_interrupted_weight_download_resumes_and_verifies_hash(tmp_path, monkeypatch):
    import hashlib
    import httpx
    from runtime import optional_install
    data = b"verified weights"
    target = tmp_path / "model.safetensors"
    pending = target.with_suffix(".safetensors.pending")
    pending.write_bytes(data[:5])
    requested = []

    def response(request):
        requested.append(request.headers.get("Range"))
        return httpx.Response(206, content=data[5:], headers={"content-range": f"bytes 5-{len(data)-1}/{len(data)}"})

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        optional_install._download_file(client, "https://example.invalid/model", target, hashlib.sha256(data).hexdigest())
    assert requested == ["bytes=5-"] and target.read_bytes() == data and not pending.exists()
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"corrupt"))) as client:
        with pytest.raises(ValueError, match="hash_mismatch"):
            optional_install._download_file(client, "https://example.invalid/model", target, hashlib.sha256(data).hexdigest())
    assert target.read_bytes() == data


def test_corrupt_receipt_degrades_component_view(tmp_path, monkeypatch):
    monkeypatch.setenv("KR_OPTIONAL_COMPONENT_RECEIPTS_DIR", str(tmp_path))
    (tmp_path / "docling.json").write_text('{broken', encoding="utf-8")
    plan = optional_media.component_plan("docling")
    assert plan["status"] == "not_installed"


def test_model_timeout_ends_real_worker_descendants():
    import os
    import subprocess
    import sys
    import time
    from runtime.process import isolated_worker_run
    from runtime.process_identity import process_identity
    command = [sys.executable, "-c", "import subprocess,sys,time; child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);print(child.pid,flush=True);time.sleep(120)"]
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        isolated_worker_run(command, input="", env=dict(os.environ), timeout=1)
    child_pid = int(raised.value.output.strip())
    deadline = time.monotonic() + 3
    while process_identity(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not process_identity(child_pid)
