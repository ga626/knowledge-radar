import subprocess
import sys
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "asr_benchmark.py"
SPEC = importlib.util.spec_from_file_location("asr_benchmark_tool", SCRIPT)
asr_benchmark_tool = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(asr_benchmark_tool)
TIMING_FIELDS = asr_benchmark_tool.TIMING_FIELDS
dry_run_result = asr_benchmark_tool.dry_run_result
real_run_result = asr_benchmark_tool.real_run_result


def test_asr_benchmark_dry_run_schema(monkeypatch, tmp_path):
    monkeypatch.setenv("KR_STATE_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("KR_ASR_MODELS", "local:faster-whisper/base,local:faster-whisper/tiny")

    result = dry_run_result(["BV1", "BV2"])

    assert result["schema"] == "knowledgeradar-asr-benchmark/v2"
    assert result["mode"] == "dry_run"
    assert result["summary"]["planned_runs"] == 4
    assert result["baseline_fields"] == list(TIMING_FIELDS)
    assert set(result["rows"][0]["timing"]) == set(TIMING_FIELDS)
    assert result["rows"][0]["strategy"]["schema"] == "knowledgeradar-asr-strategy-plan/v1"
    assert result["rows"][0]["resource_kind"] == "asr_cpu"
    assert result["concurrency"]["status"] == "implemented_p2_2"
    assert result["lifecycle"]["schema"] == "knowledgeradar-asr-lifecycle/v1"


def test_asr_benchmark_cli_dry_run(tmp_path):
    output = tmp_path / "asr.json"
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--dry-run", "--samples", "BVx", "--output", str(output)],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "knowledgeradar-asr-benchmark/v2" in completed.stdout
    assert output.is_file()


def test_asr_benchmark_real_run_missing_audio_records_failure(tmp_path):
    manifest = tmp_path / "manifest.json"
    missing = tmp_path / "missing.wav"
    manifest.write_text(
        '{"samples":[{"id":"s1","audio_path":"' + str(missing).replace("\\", "\\\\") + '"}],'
        '"matrix":[{"engine":"faster-whisper","model_ref":"local:faster-whisper/base","device":"cpu","compute_type":"int8"}]}',
        encoding="utf-8",
    )

    result = real_run_result(str(manifest), include_engines=["faster-whisper"])

    assert result["mode"] == "real_run"
    assert result["rows"][0]["status"] == "failed"
    assert "audio_path not found" in result["rows"][0]["error"]
    assert result["excluded"]
