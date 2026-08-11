from runtime.asr_policy import AsrPolicy
from runtime.asr_strategy import (
    AsrStrategyInput,
    SubtitleProbeResult,
    build_asr_strategy_plan,
)
from runtime.resource_concurrency import (
    RESOURCE_ASR_CPU,
    RESOURCE_ASR_GPU,
    RESOURCE_PROVIDER_BAILIAN,
    ResourceConcurrencyPolicy,
    infer_task_resource,
    resource_concurrency_summary,
)


def test_asr_strategy_selects_cache_first(monkeypatch, tmp_path):
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path / "runtime"))

    plan = build_asr_strategy_plan(AsrStrategyInput(platform="bilibili", has_transcript_cache=True))

    assert plan.schema == "knowledgeradar-asr-strategy-plan/v1"
    assert plan.selected_step_id == "transcript_cache"
    assert plan.steps[0].kind == "derived_text_cache"


def test_asr_strategy_selects_platform_subtitle_hit(monkeypatch, tmp_path):
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path / "runtime"))
    probe = SubtitleProbeResult(platform="youtube", status="hit", source="youtube-transcript-api", text_chars=120, line_count=8)

    plan = build_asr_strategy_plan(AsrStrategyInput(platform="youtube", platform_subtitle=probe))

    assert plan.selected_step_id == "platform_subtitle"
    assert plan.steps[0].resource_kind == "subtitle_probe"
    assert plan.steps[0].metadata["source"] == "youtube-transcript-api"


def test_asr_strategy_selects_downloader_subtitle_hit(monkeypatch, tmp_path):
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path / "runtime"))
    miss = SubtitleProbeResult(platform="youtube", status="miss", reason="no public transcript")
    hit = SubtitleProbeResult(platform="youtube", status="hit", source="yt-dlp", text_chars=200, line_count=12)

    plan = build_asr_strategy_plan(AsrStrategyInput(platform="youtube", platform_subtitle=miss, downloader_subtitle=hit))

    assert plan.selected_step_id == "downloader_subtitle"
    assert [step.id for step in plan.steps] == ["platform_subtitle", "downloader_subtitle"]


def test_asr_strategy_selects_local_gpu_asr_when_requested(monkeypatch, tmp_path):
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path / "runtime"))
    policy = AsrPolicy(models=("local:faster-whisper/base",), device="cuda", compute_type="float16")
    miss = SubtitleProbeResult(platform="bilibili", status="miss", reason="no subtitles")

    plan = build_asr_strategy_plan(AsrStrategyInput(platform="bilibili", platform_subtitle=miss, gpu_available=True), asr_policy=policy)

    assert plan.selected_step_id == "local_asr"
    selected = plan.steps[-1]
    assert selected.resource_kind == RESOURCE_ASR_GPU
    assert selected.engine == "local:faster-whisper/base"
    assert selected.metadata["benchmark_profile"]["status"] == "opt_in_hot_path"
    assert selected.metadata["download_resource_kind"] == "media_download"


def test_asr_strategy_prefers_tiny_when_cpu_load_is_high(monkeypatch, tmp_path):
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path / "runtime"))
    policy = AsrPolicy(models=("local:faster-whisper/base", "local:faster-whisper/tiny"), device="cpu", compute_type="int8")
    miss = SubtitleProbeResult(platform="bilibili", status="miss", reason="no subtitles")

    plan = build_asr_strategy_plan(
        AsrStrategyInput(platform="bilibili", platform_subtitle=miss, cpu_load_percent=90),
        asr_policy=policy,
    )

    selected = plan.steps[-1]
    assert selected.resource_kind == RESOURCE_ASR_CPU
    assert selected.engine == "local:faster-whisper/tiny"
    assert selected.metadata["benchmark_profile"]["status"] == "speed_fallback"


def test_asr_strategy_manifest_marks_unselected_engines() -> None:
    from runtime.asr_strategy import asr_strategy_manifest

    manifest = asr_strategy_manifest()

    assert manifest["default_engine"] == "local:faster-whisper/base"
    assert "local:funasr/sensevoice-small" in manifest["disabled_after_real_run"]
    assert "local:sherpa-onnx/sensevoice-int8" in manifest["disabled_after_real_run"]
    assert manifest["benchmark_profile"]["validated_paths"]["faster_whisper_cpu_base_int8"]["status"] == "default_main_path"


def test_resource_concurrency_policy_env(monkeypatch):
    monkeypatch.setenv("KR_ASR_CPU_CONCURRENCY", "2")
    monkeypatch.setenv("KR_PROVIDER_CONCURRENCY_BAILIAN", "4")

    policy = ResourceConcurrencyPolicy.from_env()
    summary = resource_concurrency_summary()

    assert policy.asr_cpu == 2
    assert policy.provider_bailian == 4
    assert summary["resources"]["asr_cpu"]["limit"] == 2
    assert infer_task_resource("bilibili_transcribe", {"device": "cpu"}) == RESOURCE_ASR_CPU
    assert infer_task_resource("bilibili_qwen_video_analysis", {}) == RESOURCE_PROVIDER_BAILIAN
