from __future__ import annotations

from pathlib import Path

from runtime.leases import RuntimeLeaseCoordinator


def test_exclusive_lease_blocks_second_coordinator(tmp_path: Path) -> None:
    db = tmp_path / "leases.sqlite3"
    first = RuntimeLeaseCoordinator(db)
    second = RuntimeLeaseCoordinator(db)

    acquired = first.acquire_exclusive("browser_profile", "xhs:account-a", owner={"client_id": "codex:a"}, ttl_s=60)
    blocked = second.acquire_exclusive("browser_profile", "xhs:account-a", owner={"client_id": "codex:b"}, ttl_s=60)

    assert acquired.acquired is True
    assert blocked.acquired is False
    assert blocked.reason == "lease_unavailable"
    assert blocked.holder["owner_client_id"] == "codex:a"

    assert first.release(acquired.lease_id) is True
    retry = second.acquire_exclusive("browser_profile", "xhs:account-a", owner={"client_id": "codex:b"}, ttl_s=60)
    assert retry.acquired is True


def test_bounded_lease_allows_limit_then_blocks(tmp_path: Path) -> None:
    coordinator = RuntimeLeaseCoordinator(tmp_path / "leases.sqlite3")

    a = coordinator.acquire_bounded("local_compute", "asr_gpu", limit=2, owner={"client_id": "a"})
    b = coordinator.acquire_bounded("local_compute", "asr_gpu", limit=2, owner={"client_id": "b"})
    c = coordinator.acquire_bounded("local_compute", "asr_gpu", limit=2, owner={"client_id": "c"})

    assert a.acquired is True
    assert b.acquired is True
    assert c.acquired is False
    assert c.metadata["limit"] == 2
    assert c.metadata["active"] == 2


def test_expired_lease_is_reclaimed(tmp_path: Path) -> None:
    coordinator = RuntimeLeaseCoordinator(tmp_path / "leases.sqlite3")

    old = coordinator.acquire_exclusive("provider_quota", "tavily", owner={"client_id": "old"}, ttl_s=1, now=100)
    assert old.acquired is True

    new = coordinator.acquire_exclusive("provider_quota", "tavily", owner={"client_id": "new"}, ttl_s=1, now=102)
    assert new.acquired is True
    summary = coordinator.summary(now=102)
    assert summary["counts"]["expired"] == 1
    assert summary["counts"]["held"] == 1


def test_heartbeat_extends_lease(tmp_path: Path) -> None:
    coordinator = RuntimeLeaseCoordinator(tmp_path / "leases.sqlite3")
    lease = coordinator.acquire_exclusive("manual_interaction", "vip_oa:account-a", owner={"client_id": "codex"}, ttl_s=1, now=100)

    assert coordinator.heartbeat(lease.lease_id, ttl_s=10, now=100.5) is True
    blocked = coordinator.acquire_exclusive("manual_interaction", "vip_oa:account-a", owner={"client_id": "other"}, ttl_s=1, now=101.5)

    assert blocked.acquired is False
