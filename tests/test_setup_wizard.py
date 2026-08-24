import json
import sys
import threading
from http.client import HTTPConnection
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from onboarding import configuration  # noqa: E402
from onboarding.setup_wizard import WizardServer  # noqa: E402


def test_public_snapshot_never_returns_secret_values(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("TAVILY_API_KEY=private-value\nSEARXNG_BASE_URL=http://127.0.0.1:8080\n", encoding="utf-8")

    snapshot = configuration.public_snapshot(env_path)
    rendered = json.dumps(snapshot, ensure_ascii=False)

    assert "private-value" not in rendered
    assert "127.0.0.1:8080" not in rendered
    assert next(field for field in snapshot["fields"] if field["key"] == "TAVILY_API_KEY")["configured"] is True


def test_apply_updates_preserves_unrelated_lines_and_ignores_blank_values(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("# keep\nTAVILY_API_KEY=old\nCUSTOM_VALUE=preserve\n", encoding="utf-8")

    changed = configuration.apply_updates({"TAVILY_API_KEY": "new", "EXA_API_KEY": "", "BRAVE_SEARCH_API_KEY": "brave"}, env_path)

    saved = env_path.read_text(encoding="utf-8")
    assert changed == ["BRAVE_SEARCH_API_KEY", "TAVILY_API_KEY"]
    assert "# keep" in saved
    assert "CUSTOM_VALUE=preserve" in saved
    assert "TAVILY_API_KEY=new" in saved
    assert "BRAVE_SEARCH_API_KEY=brave" in saved
    assert "EXA_API_KEY=" not in saved


def test_apply_updates_rejects_unknown_or_multiline_values(tmp_path):
    with pytest.raises(ValueError):
        configuration.apply_updates({"UNKNOWN_KEY": "value"}, tmp_path / ".env")
    with pytest.raises(ValueError):
        configuration.apply_updates({"TAVILY_API_KEY": "line1\nline2"}, tmp_path / ".env")


def test_wizard_rejects_unauthenticated_posts_and_never_echoes_values(monkeypatch):
    saved_payloads = []
    monkeypatch.setattr("onboarding.setup_wizard.apply_updates", lambda values: saved_payloads.append(values) or list(values))
    server = WizardServer(("127.0.0.1", 0))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        port = server.server_port
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        body = json.dumps({"values": {"TAVILY_API_KEY": "private-value"}})
        connection.request("POST", "/api/config", body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 403
        assert "private-value" not in response.read().decode("utf-8")

        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request(
            "POST",
            "/api/config",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{port}",
                "X-KR-Setup-Token": server.setup_token,
            },
        )
        response = connection.getresponse()
        payload = response.read().decode("utf-8")
        assert response.status == 200
        assert "private-value" not in payload
        assert saved_payloads == [{"TAVILY_API_KEY": "private-value"}]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_wizard_status_is_sanitized_and_cleanup_requires_local_session(monkeypatch):
    monkeypatch.setattr(
        "onboarding.setup_wizard.capability_packs",
        lambda snapshot: [{"id": "core_web", "status": "ready", "configured_field_count": 1}],
    )
    storage_calls = []
    monkeypatch.setattr(
        "onboarding.setup_wizard.storage_summary",
        lambda: storage_calls.append(True) or {"available": True, "categories": [{"label": "媒体缓存", "bytes": 12}], "total_bytes": 12},
    )
    monkeypatch.setattr(
        "onboarding.setup_wizard.installation_summary",
        lambda: {"available": True, "version": "test", "channel": "stable", "data_root_present": True, "rollback_available": False, "message": "ok"},
    )
    calls = []
    monkeypatch.setattr(
        "onboarding.setup_wizard.expired_media_cleanup",
        lambda *, apply: calls.append(apply) or {"status": "APPLIED", "expired_file_count": 1},
    )
    server = WizardServer(("127.0.0.1", 0))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        port = server.server_port
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", "/api/status")
        response = connection.getresponse()
        payload = response.read().decode("utf-8")
        assert response.status == 200
        assert "private-value" not in payload
        assert "media_cache" not in payload
        assert storage_calls == []

        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", "/api/storage")
        response = connection.getresponse()
        assert response.status == 200
        storage = json.loads(response.read().decode("utf-8"))
        assert storage["total_bytes"] == 12
        assert storage["categories"][0]["bytes"] == 12
        assert storage_calls == [True]

        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("POST", "/api/media-cleanup", body=json.dumps({"apply": True}), headers={"Content-Type": "application/json"})
        assert connection.getresponse().status == 403
        assert calls == []

        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request(
            "POST",
            "/api/media-cleanup",
            body=json.dumps({"apply": True}),
            headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}", "X-KR-Setup-Token": server.setup_token},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read().decode("utf-8"))["expired_file_count"] == 1
        assert calls == [True]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_wizard_page_uses_its_nonce_for_interactive_script() -> None:
    server = WizardServer(("127.0.0.1", 0))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/")
        response = connection.getresponse()
        page = response.read().decode("utf-8")
        policy = response.getheader("Content-Security-Policy")
        assert response.status == 200
        assert f'nonce="{server.setup_token}"' in page
        assert f"'nonce-{server.setup_token}'" in policy
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
