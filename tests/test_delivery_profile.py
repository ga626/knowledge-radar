from __future__ import annotations

import json
import importlib.util
from pathlib import Path

_MODULE_PATH = Path(__file__).parents[1] / "scripts" / "kr_delivery_profile.py"
_SPEC = importlib.util.spec_from_file_location("kr_delivery_profile", _MODULE_PATH)
delivery = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(delivery)


def test_persisted_github_mode_is_sticky(tmp_path, monkeypatch):
    path = tmp_path / "delivery-profile.json"
    path.write_text(json.dumps({"mode": "github_delivery", "sticky": True}), encoding="utf-8")
    monkeypatch.setattr(delivery, "_remote_evidence", lambda: [])
    result = delivery.resolve(path=path)
    assert result["mode"] == "github_delivery"
    assert result["decision"] == "persisted_sticky"


def test_missing_remote_does_not_erase_history(tmp_path, monkeypatch):
    path = tmp_path / "delivery-profile.json"
    path.write_text(json.dumps({"mode": "github_delivery", "sticky": True}), encoding="utf-8")
    monkeypatch.setattr(delivery, "_remote_evidence", lambda: [])
    assert delivery.resolve(path=path)["mode"] == "github_delivery"


def test_ambiguous_evidence_blocks_initialization(tmp_path, monkeypatch):
    path = tmp_path / "delivery-profile.json"
    monkeypatch.setattr(delivery, "PROFILE_PATH", path)
    monkeypatch.setattr(delivery, "_remote_evidence", lambda: [{"kind": "git_remote", "mode": "remote_delivery"}])
    result = delivery.ensure(path=path)
    assert result["status"] == "BLOCKED"
    assert result["mode"] == "undetermined"


def test_local_receipt_is_not_misclassified_as_github_evidence(tmp_path, monkeypatch):
    receipt = tmp_path / "project_status" / "local-delivery-receipt.json"
    receipt.parent.mkdir()
    receipt.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(delivery, "ROOT", tmp_path)
    assert delivery._historical_evidence() == [
        {"kind": "delivery_receipt", "path": "project_status/local-delivery-receipt.json", "mode": "local_delivery"}
    ]
