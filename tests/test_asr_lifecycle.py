from runtime.asr_lifecycle import AsrLifecycleManager, AsrLifecyclePolicy


def test_asr_lifecycle_manager_uses_server_scope_not_research_session() -> None:
    policy = AsrLifecyclePolicy(gpu_hot_ttl_seconds=10, worker_keepalive_seconds=20)
    manager = AsrLifecycleManager(policy=policy, server_run_id="server-run-test")
    key = manager.make_key(
        worker_scope_id="task-burst-1",
        engine="faster-whisper",
        model="base",
        device="cuda",
        compute_type="float16",
    )

    assert key.server_run_id == "server-run-test"
    assert "research_session" not in key.stable_id()
    assert key.stable_id().startswith("server-run-test|task-burst-1|faster-whisper|base|cuda|float16")


def test_asr_lifecycle_manager_transitions_and_ttl_release() -> None:
    policy = AsrLifecyclePolicy(gpu_hot_ttl_seconds=10, worker_keepalive_seconds=20)
    manager = AsrLifecycleManager(policy=policy, server_run_id="server-run-test")
    key = manager.make_key(worker_scope_id="task-burst-2", device="cuda", compute_type="float16")

    manager.transition(key, "context_warm", now=100.0, metadata={"stage": "cuda_context"})
    manager.transition(key, "gpu_hot", now=101.0, metadata={"model_loaded": True})
    assert manager.should_release_gpu(key, now=105.0) is False
    assert manager.should_release_gpu(key, now=112.0) is True
    assert manager.should_terminate_worker(key, now=112.0) is False
    assert manager.should_terminate_worker(key, now=122.0) is True

    manager.transition(key, "cpu_resident", now=123.0, metadata={"unload_model": True})
    assert manager.should_release_gpu(key, now=140.0) is False
    compact = manager.compact(now=140.0)
    assert compact["records"][0]["state"] == "cpu_resident"
    assert compact["records"][0]["metadata"]["unload_model"] is True


def test_asr_lifecycle_rejects_unknown_state() -> None:
    manager = AsrLifecycleManager(server_run_id="server-run-test")
    key = manager.make_key()
    try:
        manager.transition(key, "loaded")
    except ValueError as exc:
        assert "unsupported ASR lifecycle state" in str(exc)
    else:
        raise AssertionError("expected ValueError for unsupported lifecycle state")
