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
        assert meta["resource_kind"] == "asr_gpu"
        assert meta["limit"] == 1
        assert coordinator.summary()["counts"]["held"] == 1

    assert coordinator.summary()["counts"]["released"] == 1


def test_acquire_resource_blocks_when_cross_process_lease_is_held(monkeypatch, tmp_path: Path) -> None:
    coordinator = RuntimeLeaseCoordinator(tmp_path / "leases.sqlite3")
    monkeypatch.setattr(resource_concurrency, "get_runtime_lease_coordinator", lambda: coordinator)
    held = coordinator.acquire_bounded("runtime_resource", "asr_gpu", limit=1, owner={"client_id": "other"}, ttl_s=60)
    assert held.acquired is True

    with pytest.raises(RuntimeError, match="runtime resource busy"):
        with acquire_resource("asr_gpu", ResourceConcurrencyPolicy(asr_gpu=1)):
            pass
