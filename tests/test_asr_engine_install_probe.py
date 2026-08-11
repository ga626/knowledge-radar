import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("asr_engine_install_probe", ROOT / "tools" / "asr_engine_install_probe.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
probe_engines = MODULE.probe_engines
isolated_install_probe = MODULE.isolated_install_probe


def test_asr_engine_install_probe_plan_only() -> None:
    result = probe_engines(["faster-whisper", "funasr", "sherpa-onnx"], run_import_smoke=False)

    assert result["schema"] == "knowledgeradar-asr-engine-install-probe/v1"
    assert result["mode"] == "plan_only"
    engines = {item["engine"]: item for item in result["engines"]}
    assert engines["faster-whisper"]["required"] is True
    assert "faster-whisper" in engines["faster-whisper"]["install_commands"][0]
    assert engines["funasr"]["required"] is False
    assert engines["sherpa-onnx"]["required"] is False
    assert all(item["model_load_executed"] is False for item in result["engines"])


def test_asr_engine_isolated_install_probe_can_be_called_with_empty_selection(tmp_path) -> None:
    result = isolated_install_probe([], venv_root=tmp_path, sample_smoke=False)

    assert result["schema"] == "knowledgeradar-asr-isolated-install-probe/v1"
    assert result["main_environment_modified"] is False
    assert result["engines"] == []
