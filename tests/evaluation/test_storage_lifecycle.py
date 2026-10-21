import os
import time
from pathlib import Path

from runtime.storage_lifecycle import StorageLeases,quarantine_files,restore_quarantine


def test_active_lease_protects_and_restore_checks_digest(tmp_path,monkeypatch):
    monkeypatch.setenv("KR_STATE_DIR",str(tmp_path/"state"))
    cache=tmp_path/"state"/"media_cache";cache.mkdir(parents=True)
    active=cache/"active.wav";old=cache/"old.wav"
    active.write_bytes(b"active");old.write_bytes(b"restore exactly")
    StorageLeases().acquire(active)
    result=quarantine_files([active,old],cache_root=cache,data_root=tmp_path)
    assert active.exists() and not old.exists()
    assert result["quarantined_count"]==1
    restored=restore_quarantine(result["receipt"],data_root=tmp_path,cache_root=cache)
    assert restored["files"][0]["status"]=="restored"
    assert old.read_bytes()==b"restore exactly"


def test_restore_refuses_overwrite_and_tamper(tmp_path,monkeypatch):
    monkeypatch.setenv("KR_STATE_DIR",str(tmp_path/"state"))
    cache=tmp_path/"cache";cache.mkdir();file=cache/"one.bin";file.write_bytes(b"one")
    batch=quarantine_files([file],cache_root=cache,data_root=tmp_path)
    file.write_bytes(b"new data")
    result=restore_quarantine(batch["receipt"],data_root=tmp_path,cache_root=cache)
    assert result["files"][0]["status"]=="target_exists"
    assert file.read_bytes()==b"new data"
