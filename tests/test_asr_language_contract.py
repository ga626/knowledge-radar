import pytest
from runtime.asr_policy import AsrPolicy, normalize_language
from collectors.platform import youtube
import youtube_transcript_api
from types import SimpleNamespace


def test_language_auto_and_environment_contract(monkeypatch):
    monkeypatch.delenv("KR_ASR_LANGUAGE", raising=False)
    assert AsrPolicy.for_request().language == ""
    for spelling in ["auto", "AUTO", "mixed", "zh-en", ""]:
        monkeypatch.setenv("KR_ASR_LANGUAGE", spelling)
        assert AsrPolicy.for_request().language == ""
    monkeypatch.setenv("KR_ASR_LANGUAGE", "English")
    assert AsrPolicy.for_request().language == "en"
    assert AsrPolicy.for_request("auto").language == ""
    assert normalize_language("zh-Hans") == "zh"
    assert normalize_language("Japanese") == "ja"
    with pytest.raises(ValueError, match="invalid_asr_language"):
        AsrPolicy.for_request("zz")
    monkeypatch.setenv("KR_ASR_LANGUAGE", "zz")
    with pytest.raises(ValueError, match="invalid_asr_language"):
        AsrPolicy.for_request()
    assert AsrPolicy.for_request("auto").language == ""
    assert AsrPolicy.for_request("en").language == "en"


def test_asset_identity_binds_effective_language(monkeypatch, tmp_path):
    from runtime.asset_identity import model_identity

    monkeypatch.setenv("KR_OPTIONAL_COMPONENT_RECEIPTS_DIR", str(tmp_path))
    assert model_identity(AsrPolicy(language="en")) != model_identity(AsrPolicy(language="zh"))


def test_youtube_auto_reads_other_original_subtitle_but_explicit_does_not(monkeypatch):
    class NoTranscriptFound(Exception):
        pass

    class Track:
        is_generated = False
        language_code = "fr"

        def fetch(self):
            class Fetched(list):
                language_code = "fr"

            return Fetched([SimpleNamespace(text="Bonjour", start=0, duration=2)])

    class Tracks:
        def find_transcript(self, languages):
            raise NoTranscriptFound()

        def __iter__(self):
            return iter([Track()])

    class Api:
        def __init__(self, **kw):
            pass

        def list(self, video_id):
            return Tracks()

    monkeypatch.setattr(youtube_transcript_api, "YouTubeTranscriptApi", Api)
    monkeypatch.setattr(youtube, "get_httpx_proxy", lambda: None)
    result = youtube.fetch_youtube_transcript("fixture")
    assert result["text"] == "Bonjour" and result["language"] == "fr"
    assert youtube.fetch_youtube_transcript("fixture", ["en"])["failure_type"] == "transcript_unavailable"
    Track.language_code = "en-US"
    assert youtube.fetch_youtube_transcript("fixture", ["en"])["status"] == "ok"


def test_audio_language_gate_requires_model_prefix_and_never_guesses_from_text():
    from runtime.optional_media_worker import _detected_transcript, UnsupportedASRLanguage

    for label in ["French", "Japanese", "Finnish", "unknown"]:
        result = SimpleNamespace(
            text="nonempty", tokens=["KRlanguage", " " + label, "<asr_text>", "English-looking content"]
        )
        with pytest.raises(UnsupportedASRLanguage, match="不支持的语言") as exc:
            _detected_transcript(result)
        assert exc.value.error_code == "unsupported_language"
    with pytest.raises(ValueError, match="asr_language_detection_failed"):
        _detected_transcript(SimpleNamespace(text="English looking text", tokens=["English looking text"]))
    assert _detected_transcript(SimpleNamespace(text="", tokens=[])) == ("", "")
    assert _detected_transcript(
        SimpleNamespace(
            text="nonempty", tokens=["KRlanguage", " English", "<asr_text>", "the ", "KRlanguage", " model"]
        )
    ) == ("the language model", "en")


def test_empty_audio_language_marker_is_not_a_foreign_language():
    from runtime.optional_media_worker import _detected_transcript, ASRLanguageDetectionFailed
    assert _detected_transcript(SimpleNamespace(text='language None<asr_text>',
        tokens=['KRlanguage', ' None', '<asr_text>', ' \n'])) == ('', '')
    with pytest.raises(ASRLanguageDetectionFailed):
        _detected_transcript(SimpleNamespace(text='language None<asr_text>speech',
            tokens=['KRlanguage', ' None', '<asr_text>', 'speech']))


@pytest.mark.parametrize('speech_status,chunks,duration,accepted',[
    ('no_speech_detected',1,30,True),('no_speech_detected',0,0,False),('',1,30,False)])
def test_optional_asr_empty_requires_explicit_processed_no_speech(monkeypatch,speech_status,chunks,duration,accepted,tmp_path):
    from runtime import optional_media,optional_media_worker,component_environment
    source=tmp_path/'audio.wav';source.write_bytes(b'input')
    monkeypatch.setattr(optional_media,'approved_source',lambda p:source)
    monkeypatch.setattr(optional_media,'select_component',lambda p:'qwen3-asr-0.6b')
    monkeypatch.setattr(optional_media,'validate_receipt',lambda c:{'model_root':str(tmp_path)})
    monkeypatch.setattr(component_environment,'component_environment',lambda *a,**kw:None)
    monkeypatch.setattr(optional_media,'file_sha',lambda p:'a'*64)
    monkeypatch.setattr(optional_media,'_save_result',lambda result,*a,**kw:result)
    monkeypatch.setattr(optional_media_worker,'execute',lambda r:{'status':'acquired','text':'','speech_status':speech_status,
        'asr_metrics':{'chunks':chunks,'audio_duration_s':duration}})
    payload={'input_sha256':'a'*64,'language':'auto'}
    if accepted:
        assert optional_media.run_optional(payload)['speech_status']=='no_speech_detected'
    else:
        with pytest.raises(ValueError,match='optional_component_empty_or_failed'):
            optional_media.run_optional(payload)


def test_media_checkpoint_invalidates_effective_language(monkeypatch, tmp_path):
    from media_bundle import MediaBundle, MediaRef, MediaKind, MediaLocationType
    from media_router import MediaActionPlan, MediaOperation, InformationSourceMode, StoragePolicy
    from runtime import media_execution, optional_media, media_acquisition

    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KR_MEDIA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("KR_OPTIONAL_COMPONENT_RECEIPTS_DIR", str(tmp_path / "receipts"))
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"identity-only")
    calls = []

    def run(payload):
        calls.append(payload["language"])
        return {"status": "acquired", "text": "transcript", "engine": "sherpa_onnx_int8", "detected_language": "en"}

    monkeypatch.setattr(optional_media, "run_optional", run)
    monkeypatch.setattr(
        media_acquisition,
        "acquire_media_ref",
        lambda r: {"path": str(audio), "sha256": optional_media.file_sha(audio), "download_s": 0},
    )
    ref = MediaRef(MediaKind.AUDIO, str(audio), location_type=MediaLocationType.LOCAL_PATH)
    bundle = MediaBundle.build(platform="test", source_url="", audio_refs=[ref])
    plan = MediaActionPlan.from_bundle(
        bundle,
        source_mode=InformationSourceMode.DERIVED_TEXT,
        target_kinds=(MediaKind.AUDIO,),
        operations=(MediaOperation.RUN_ASR,),
        storage_policy=StoragePolicy.MEDIA_CACHE_TTL,
    )
    monkeypatch.setenv("KR_ASR_LANGUAGE", "zh")
    first = media_execution.execute_media_plan(bundle, plan)
    media_execution.execute_media_plan(bundle, plan)
    monkeypatch.setenv("KR_ASR_LANGUAGE", "en")
    media_execution.execute_media_plan(bundle, plan)
    assert calls == ["zh", "en"]
    assert first["transcript_metadata"]["language"] == "en" and first["transcript_metadata"]["language_hint"] == "zh"


def test_asr_lock_and_retired_receipt_are_rejected(monkeypatch, tmp_path):
    import json
    import hashlib
    from pathlib import Path
    from runtime.model_roles import COMPONENTS, ASR_COMPONENT
    from runtime.optional_media import validate_receipt

    role = COMPONENTS[ASR_COMPONENT]
    root = Path(__file__).resolve().parents[1]
    lock = (root / role["requirements_lock"]).read_bytes()
    assert hashlib.sha256(lock).hexdigest() == role["requirements_sha256"]
    assert b"transformers" not in lock and b"torch" not in lock
    monkeypatch.setenv("KR_OPTIONAL_COMPONENT_RECEIPTS_DIR", str(tmp_path))
    (tmp_path / (ASR_COMPONENT + ".json")).write_text(json.dumps({"weight_revision": "retired-hf-revision"}))
    with pytest.raises(ValueError, match="optional_component_role_revision_mismatch"):
        validate_receipt(ASR_COMPONENT)


def test_unsupported_audio_is_visible_in_public_media_result(monkeypatch, tmp_path):
    from media_bundle import MediaBundle, MediaKind, MediaRef, MediaLocationType
    from media_router import MediaActionPlan, MediaOperation, InformationSourceMode, StoragePolicy
    from runtime import media_execution, optional_media, media_acquisition
    from runtime.optional_media_worker import UnsupportedASRLanguage

    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KR_MEDIA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("KR_OPTIONAL_COMPONENT_RECEIPTS_DIR", str(tmp_path / "receipts"))
    monkeypatch.delenv("KR_ASR_LANGUAGE", raising=False)
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"fixture")
    monkeypatch.setattr(
        media_acquisition,
        "acquire_media_ref",
        lambda r: {"path": str(audio), "sha256": optional_media.file_sha(audio), "download_s": 0},
    )

    def unsupported(p):
        raise UnsupportedASRLanguage("French")

    monkeypatch.setattr(optional_media, "run_optional", unsupported)
    bundle = MediaBundle.build(
        platform="test",
        source_url="",
        audio_refs=[MediaRef(MediaKind.AUDIO, str(audio), location_type=MediaLocationType.LOCAL_PATH)],
    )
    plan = MediaActionPlan.from_bundle(
        bundle,
        source_mode=InformationSourceMode.DERIVED_TEXT,
        target_kinds=(MediaKind.AUDIO,),
        operations=(MediaOperation.RUN_ASR,),
        storage_policy=StoragePolicy.MEDIA_CACHE_TTL,
    )
    result = media_execution.execute_media_plan(bundle, plan)
    assert result["status"] == "partial" and "transcript" not in result
    assert result["steps"][0]["error_code"] == "unsupported_language"
    assert "不支持的语言" in result["steps"][0]["error_message"]


@pytest.mark.parametrize("fail", [False, True])
def test_component_upgrade_is_explicit_and_rolls_back_on_failure(monkeypatch, tmp_path, fail):
    import json
    from runtime import optional_media, resource_concurrency, leases
    from runtime.model_roles import ASR_COMPONENT

    root = tmp_path / "components"
    root.mkdir()
    monkeypatch.setenv("KR_OPTIONAL_COMPONENT_RECEIPTS_DIR", str(root))
    monkeypatch.setenv("KR_LOG_DIR", str(tmp_path / "logs"))
    coordinator = leases.RuntimeLeaseCoordinator(tmp_path / "leases.sqlite3")
    monkeypatch.setattr(resource_concurrency, "get_runtime_lease_coordinator", lambda: coordinator)
    target = root / "environments" / ASR_COMPONENT
    target.mkdir(parents=True)
    (target / "old.txt").write_text("retired environment")
    receipt = root / (ASR_COMPONENT + ".json")
    old = {
        "schema": "knowledgeradar-optional-install/v1",
        "component": ASR_COMPONENT,
        "weight_revision": "previous-role",
    }
    receipt.write_text(json.dumps(old))
    before = receipt.read_bytes()

    def install(component, spec):
        assert not target.exists() and not receipt.exists()
        target.mkdir()
        (target / "new.txt").write_text("current environment")
        if fail:
            raise RuntimeError("offline_install_failed")
        receipt.write_text('{"candidate":"verified"}')
        return {"status": "installed_identity_verified"}

    monkeypatch.setattr(optional_media, "_install_component", install)
    with pytest.raises(ValueError, match="reviewed_installation_required"):
        optional_media.install_component(ASR_COMPONENT, {})
    assert receipt.read_bytes() == before and (target / "old.txt").is_file()
    if fail:
        with pytest.raises(RuntimeError, match="offline_install_failed"):
            optional_media.install_component(
                ASR_COMPONENT, {"installation_acknowledged": True, "license_review": "APPROVED"}
            )
        assert receipt.read_bytes() == before and (target / "old.txt").is_file() and not (target / "new.txt").exists()
    else:
        assert (
            optional_media.install_component(
                ASR_COMPONENT, {"installation_acknowledged": True, "license_review": "APPROVED"}
            )["status"]
            == "installed_identity_verified"
        )
        assert (target / "new.txt").is_file() and not (target / "old.txt").exists()
    assert not list(root.glob(".replacing-*"))


def test_native_model_download_only_accepts_pinned_archive_members(monkeypatch, tmp_path):
    import tarfile
    import io
    import hashlib
    from runtime.optional_install import _download_asr_model

    data = b"fixture pinned graph"
    archive = tmp_path / "test.tar.bz2"
    with tarfile.open(archive, "w:bz2") as t:
        info = tarfile.TarInfo("test/encoder.int8.onnx")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    role = {
        "model": "test",
        "weight_revision": "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest(),
        "model_files": {"encoder.int8.onnx": hashlib.sha256(data).hexdigest()},
    }
    assert _download_asr_model(role, tmp_path / "model") == role["model_files"]
    assert (tmp_path / "model/encoder.int8.onnx").read_bytes() == data
    role["model_files"]["encoder.int8.onnx"] = "0" * 64
    with pytest.raises(ValueError, match="downloaded_weight_hash_mismatch"):
        _download_asr_model(role, tmp_path / "model")


def test_task_language_is_frozen_before_enqueue_and_changes_task_key(monkeypatch, tmp_path):
    import json
    from runtime.task_service import TaskService
    from runtime.tasks import TaskStore
    from runtime import optional_media
    from runtime.model_roles import ASR_COMPONENT

    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KR_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("KR_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("KR_OPTIONAL_COMPONENT_RECEIPTS_DIR", str(tmp_path / "receipts"))
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    (receipts / (ASR_COMPONENT + ".json")).write_text("{}")
    monkeypatch.setattr(optional_media, "validate_receipt", lambda c: {})
    source = tmp_path / "voice.wav"
    source.write_bytes(b"not decoded at enqueue")
    service = TaskService(TaskStore(tmp_path / "tasks.sqlite3"))
    jobs = []
    monkeypatch.setattr(service, "_queue_enhancement", lambda *args: jobs.append(args))
    payload = {
        "component": ASR_COMPONENT,
        "input_path": str(source),
        "approved_input_root": str(tmp_path),
        "input_sha256": optional_media.file_sha(source),
    }
    monkeypatch.setenv("KR_ASR_LANGUAGE", "zh")
    first = service.submit("optional_media", payload)
    monkeypatch.setenv("KR_ASR_LANGUAGE", "en")
    second = service.submit("optional_media", payload)
    assert first["task_id"] != second["task_id"] and "language" not in payload
    assert jobs[0][0]["payload"]["language"] == "zh" and jobs[1][0]["payload"]["language"] == "en"
    assert json.loads(jobs[0][2])["language"] == "zh"
    assert json.loads((jobs[0][1] / "input.json").read_text("utf-8"))["payload"]["language"] == "zh"


@pytest.mark.parametrize("language,lan", [("auto", "fr"), ("en", "ai-en"), ("zh", "ai-zh"), ("en", "en-US")])
def test_bilibili_subtitle_language_normalization_without_asr(monkeypatch, tmp_path, language, lan):
    from collectors.platform import bilibili

    monkeypatch.setenv("KR_MEDIA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("KR_ASR_LANGUAGE", raising=False)
    data = [
        {"code": 0, "data": {"subtitle": {"subtitles": [{"lan": lan, "subtitle_url": "https://example.com/caption"}]}}},
        {"body": [{"content": "original subtitle", "from": 1, "to": 3}]},
    ]
    monkeypatch.setattr(
        bilibili.httpx, "get", lambda *a, **k: SimpleNamespace(raise_for_status=lambda: None, json=lambda: data.pop(0))
    )
    result = bilibili._probe_bilibili_subtitle(
        "fixture", output_dir=str(tmp_path), task_id="", language=language, info={"cid": 1}
    )
    assert result["hit"] and result["language"] == lan and result["text"] == "original subtitle"


@pytest.mark.parametrize("detection_failed", [False, True])
def test_async_audio_failure_preserves_language_reason(monkeypatch, tmp_path, detection_failed):
    import time
    from runtime import optional_media, optional_media_worker, component_environment, leases, resource_concurrency
    from runtime.task_service import TaskService
    from runtime.tasks import TaskStore
    from runtime.model_roles import ASR_COMPONENT

    for key, path in {
        "KR_DATA_ROOT": tmp_path / "data",
        "KR_STATE_DIR": tmp_path / "state",
        "KR_LOG_DIR": tmp_path / "logs",
        "KR_OPTIONAL_COMPONENT_RECEIPTS_DIR": tmp_path / "receipts",
    }.items():
        monkeypatch.setenv(key, str(path))
    monkeypatch.delenv("KR_ASR_LANGUAGE", raising=False)
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    (receipts / (ASR_COMPONENT + ".json")).write_text("{}")
    monkeypatch.setattr(optional_media, "validate_receipt", lambda c: {"python": "fixture", "model_root": "fixture"})
    monkeypatch.setattr(component_environment, "component_environment", lambda *a, **kw: {})
    coordinator = leases.RuntimeLeaseCoordinator(tmp_path / "leases.sqlite3")
    monkeypatch.setattr(resource_concurrency, "get_runtime_lease_coordinator", lambda: coordinator)

    def reject(payload):
        if detection_failed:
            raise optional_media_worker.ASRLanguageDetectionFailed()
        raise optional_media_worker.UnsupportedASRLanguage("French")

    monkeypatch.setattr(optional_media_worker, "execute", reject)
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"not decoded in this control-flow test")
    service = TaskService(TaskStore(tmp_path / "tasks.sqlite3"))
    task = service.submit(
        "optional_media",
        {
            "input_path": str(audio),
            "approved_input_root": str(tmp_path),
            "input_sha256": optional_media.file_sha(audio),
        },
        timeout_s=10,
    )
    until = time.monotonic() + 5
    while time.monotonic() < until:
        row = service.store.get_task(task["task_id"])
        if row["status"] == "failed":
            break
        time.sleep(0.02)
    assert row["status"] == "failed"
    assert row["error_code"] == ("asr_language_detection_failed" if detection_failed else "unsupported_language")
    assert ("无法确认音频语言" if detection_failed else "不支持的语言") in row["error"]
    assert not row.get("result_path")
