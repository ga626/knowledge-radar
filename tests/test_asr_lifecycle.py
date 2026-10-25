from runtime import asr_lifecycle


def test_service_does_not_hold_a_model_or_claim_simulated_states(monkeypatch):
    monkeypatch.setattr(asr_lifecycle, "_gpu_memory_from_nvidia_smi", lambda: {"available": False})
    summary = asr_lifecycle.asr_lifecycle_summary()
    assert summary["component"] == "qwen3-asr-0.6b"
    assert summary["resident_model"] is False
    assert summary["concurrency_resource"] == "local_heavy"
    assert "records" not in summary
    assert "current_state" not in summary
