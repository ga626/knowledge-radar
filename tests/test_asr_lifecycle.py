from runtime import asr_lifecycle


def test_service_does_not_hold_a_model_or_claim_simulated_states(monkeypatch):
    from runtime import optional_media_worker
    monkeypatch.setattr(optional_media_worker, "_ASR_MODEL", None)
    summary = asr_lifecycle.asr_lifecycle_summary()
    assert summary["component"] == "qwen3-asr-0.6b"
    assert summary["resident_model"] is False
    assert summary["concurrency_resource"] == "local_heavy"
    assert "records" not in summary
    assert "current_state" not in summary


def test_lifecycle_reports_cached_instance_without_loading(monkeypatch):
    from runtime import optional_media_worker
    monkeypatch.setattr(optional_media_worker, '_ASR_MODEL', (('fixture',), object()))
    summary = asr_lifecycle.asr_lifecycle_summary()
    assert summary['resident_model'] is True
    assert summary['default_device'] == 'cpu' and summary['compute_type'] == 'int8'
