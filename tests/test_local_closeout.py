import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "kr_local_closeout.py"
SPEC = importlib.util.spec_from_file_location("kr_local_closeout", MODULE_PATH)
closeout = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(closeout)


def test_closeout_lock_rejects_second_process_handle(tmp_path, monkeypatch):
    monkeypatch.setattr(closeout, "LOCK_PATH", tmp_path / "closeout.lock")
    with closeout._closeout_lock():
        with pytest.raises(RuntimeError, match="already held"):
            with closeout._closeout_lock():
                pass


def test_receipt_refreshes_existing_quality_snapshot(tmp_path, monkeypatch):
    quality_path = tmp_path / "project_status" / "Quality-Gate-State.json"
    quality_path.parent.mkdir()
    quality_path.write_text("{}", encoding="utf-8")
    calls = []
    monkeypatch.setattr(closeout, "ROOT", tmp_path)
    monkeypatch.setattr(closeout, "quality_state_path", lambda root: quality_path)
    monkeypatch.setattr(closeout, "refresh_quality_state_snapshot", lambda root, *, event: calls.append((root, event)))

    assert closeout._refresh_quality_state_after_receipt() == quality_path
    assert calls == [(tmp_path, "local_delivery_receipt")]
