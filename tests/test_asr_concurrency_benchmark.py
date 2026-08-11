import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "asr_concurrency_benchmark.py"
SPEC = importlib.util.spec_from_file_location("asr_concurrency_benchmark_tool", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_asr_concurrency_manifest_task_expansion(tmp_path):
    audio_58 = tmp_path / "sample_58s.wav"
    audio_5m = tmp_path / "sample_5m.wav"
    audio_58.write_bytes(b"RIFF")
    audio_5m.write_bytes(b"RIFF")
    manifest = {
        "samples": [
            {"id": "local_58s", "audio_path": str(audio_58), "platform": "fixture", "content_id": "s58"},
            {"id": "local_5m", "audio_path": str(audio_5m), "platform": "fixture", "content_id": "s5m"},
        ]
    }

    tasks = MODULE._tasks(manifest, "gpu_cpu_parallel", "cache-dir")

    assert len(tasks) == 2
    assert tasks[0]["device"] == "cuda"
    assert tasks[0]["compute_type"] == "float16"
    assert tasks[1]["device"] == "cpu"
    assert tasks[1]["compute_type"] == "int8"
    assert all(task["model_ref"] == "local:faster-whisper/base" for task in tasks)
