from __future__ import annotations

from pathlib import Path

import pytest

from runtime import resource_concurrency
from runtime.leases import RuntimeLeaseCoordinator
from runtime.resource_concurrency import ResourceConcurrencyPolicy, acquire_resource


def test_acquire_resource_uses_runtime_lease(monkeypatch, tmp_path: Path) -> None:
    coordinator = RuntimeLeaseCoordinator(tmp_path / "leases.sqlite3")
    monkeypatch.setattr(resource_concurrency, "get_runtime_lease_coordinator", lambda: coordinator)

    with acquire_resource("asr_gpu", ResourceConcurrencyPolicy(asr_gpu=1)) as meta:
        assert meta["lease_backed"] is True
        assert meta["resource_kind"] == "local_heavy"
        assert meta["limit"] == 1
        assert coordinator.summary()["counts"]["held"] == 1

    assert coordinator.summary()["counts"]["released"] == 1


def test_acquire_resource_blocks_when_cross_process_lease_is_held(monkeypatch, tmp_path: Path) -> None:
    coordinator = RuntimeLeaseCoordinator(tmp_path / "leases.sqlite3")
    monkeypatch.setattr(resource_concurrency, "get_runtime_lease_coordinator", lambda: coordinator)
    monkeypatch.setenv("KR_LOCAL_HEAVY_QUEUE_TIMEOUT_S", "0")
    held = coordinator.acquire_bounded("runtime_resource", "local_heavy", limit=1, owner={"client_id": "other"}, ttl_s=60)
    assert held.acquired is True

    with pytest.raises(RuntimeError, match="runtime resource busy"):
        with acquire_resource("asr_gpu", ResourceConcurrencyPolicy(asr_gpu=1)):
            pass


def test_unrelated_leases_cannot_hide_a_busy_resource(monkeypatch, tmp_path):
    coordinator = RuntimeLeaseCoordinator(tmp_path / 'leases.sqlite3')
    monkeypatch.setattr(resource_concurrency, 'get_runtime_lease_coordinator', lambda: coordinator)
    held = coordinator.acquire_bounded('runtime_resource', 'local_heavy', limit=1,
        owner={'client_id': 'holder'}, ttl_s=60)
    assert held.acquired
    for i in range(105):
        assert coordinator.acquire_exclusive('unrelated', str(i), owner={'client_id': 'other'}, ttl_s=60).acquired
    assert not resource_concurrency.resource_available('asr_cpu')
    coordinator.release(held.lease_id)
    assert resource_concurrency.resource_available('asr_cpu')


def test_explicit_download_wait_queues_locally_without_changing_global_limit(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    coordinator = RuntimeLeaseCoordinator(tmp_path / 'leases.sqlite3')
    monkeypatch.setattr(resource_concurrency, 'get_runtime_lease_coordinator', lambda: coordinator)
    held = coordinator.acquire_bounded('runtime_resource', 'media_download', limit=1,
        owner={'client_id':'other'}, ttl_s=60)
    ready = Event()
    def waiting():
        ready.set()
        with acquire_resource('media_download', ResourceConcurrencyPolicy(media_download=1), wait_timeout_s=2) as meta:
            assert meta['limit'] == 1
            return True
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(waiting)
        assert ready.wait(timeout=1)
        coordinator.release(held.lease_id)
        assert pending.result(timeout=3)
    assert coordinator.summary()['counts'].get('held',0) == 0
