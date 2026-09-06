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


def test_public_provider_guides_expose_classified_tutorial_metadata_without_values(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("TAVILY_API_KEY=private-value\n", encoding="utf-8")

    guides = {guide["id"]: guide for guide in configuration.public_provider_guides(env_path)}

    tavily = guides["tavily"]
    assert tavily["group"] == "网页发现"
    assert tavily["configured"] is True
    assert tavily["prerequisite"] == "Tavily 账号"
    assert tavily["screenshot"] == "/assets/tavily.jpg"
    assert len(tavily["steps"]) == 4
    assert guides["compatible_llm"]["screenshot"] == ""
    assert "private-value" not in json.dumps(guides, ensure_ascii=False)


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
        assert '"id": "media_downloader"' in payload
        assert "components" in payload
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
        for view_id, label in (
            ("overview", "总览"),
            ("services", "能力中心"),
            ("components", "本地组件"),
            ("data", "数据与恢复"),
            ("settings", "设置与帮助"),
        ):
            assert f'data-view="{view_id}"' in page
            assert f'data-view-panel="{view_id}"' in page
            assert label in page
        assert "function activateView(id)" in page
        assert 'class="workspace"' in page
        assert "/api/dashboard" in page
        assert "/api/configuration" in page
        assert "配置中心" in page
        assert "诊断与隐私已移至“设置与帮助”" in page
        assert "按你想启用的功能选择组件" in page
        assert "插件本体与功能组件分开" in page
        assert 'id="component-groups"' in page
        assert "function componentCard(x)" in page
        assert "function showComponentGuide(item)" in page
        assert "prefers-reduced-motion" in page
        assert 'class="radar-light"' in page
        assert 'class="radar-reflection"' in page
        assert ".radar-light{display:block;z-index:2;opacity:1;background:none;filter:none" in page
        assert "function createWebGLLight()" in page
        assert "function createCanvasLight()" in page
        assert "function tailStrength(lag)" in page
        assert ".arm-glow{display:block;stroke:url(#sweep-arm);stroke-width:9" in page
        assert ".arm{display:block;stroke:url(#sweep-arm);stroke-width:2.6" in page
        assert ".radar-grid-ring.major{stroke:rgba(78,122,144,.072);stroke-width:.86}" in page
        assert ".radar-grid-ray{stroke:rgba(63,98,117,.016);stroke-width:.59}" in page
        assert "ctx.lineCap='butt';ctx.shadowBlur=0;" in page
        assert ".radar-grid-ray.major{stroke:rgba(76,120,142,.050);stroke-width:.79}" in page
        assert "rad(bearing+2.62)" in page
        assert "@keyframes scan-bloom" in page
        assert "state==='active'?3900:state==='attention'?4400:4800" in page
        assert "edge=46" in page
        assert "Math.hypot(x-cx,y-cy)<visibleRadius" in page
        assert "count=state==='active'?10:state==='attention'?8:7" in page
        assert "minEchoGap=30" in page
        assert "targets.every(existing=>" in page
        assert "sizes={small:[3.9,1.44],medium:[4.8,1.72],large:[5.8,2]}" in page
        assert "<circle class=\"echo-ring\" r=\"'+size[0]+'\"/><circle class=\"echo-core\"" in page
        assert "当前队列" in page
        assert 'class="radar-status"' in page
        assert 'class="panel task-activity"' in page
        assert 'class="panel signals"' not in page
        assert 'data-go-to="services"' in page
        assert 'data-scroll-to=".task-activity"' in page
        assert 'data-scroll-to=".lower-single .attention"' in page
        assert page.count('id="active-tasks"') == 1
        assert page.count('id="recent-tasks"') == 1
        assert "requestAnimationFrame(frame)" in page
        assert "function plan(state)" in page
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_wizard_favicon_is_a_safe_no_content_response() -> None:
    server = WizardServer(("127.0.0.1", 0))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/favicon.ico")
        response = connection.getresponse()

        assert response.status == 204
        assert response.read() == b""
        assert response.getheader("X-Content-Type-Options") == "nosniff"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_wizard_serves_only_curated_guide_screenshots() -> None:
    server = WizardServer(("127.0.0.1", 0))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/assets/tavily.jpg")
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type") == "image/jpeg"
        assert response.getheader("X-Content-Type-Options") == "nosniff"
        assert response.read().startswith(b"\xff\xd8\xff")

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/assets/../setup_wizard.py")
        assert connection.getresponse().status == 404
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_wizard_health_is_safe_and_local_session_can_stop_the_host() -> None:
    server = WizardServer(("127.0.0.1", 0))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        port = server.server_port
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        assert response.status == 200
        health = json.loads(response.read().decode("utf-8"))
        assert health == {
            "schema": "knowledgeradar-local-console/v2",
            "status": "ready",
            "role": "unmanaged",
            "fingerprint": "",
            "generation": 0,
        }

        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request(
            "POST",
            "/api/console/stop",
            body="{}",
            headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}", "X-KR-Setup-Token": server.setup_token},
        )
        assert connection.getresponse().status == 200
        worker.join(timeout=5)
        assert not worker.is_alive()
    finally:
        server.server_close()


def test_development_preview_rejects_mutating_console_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KR_CONSOLE_READ_ONLY", "1")
    server = WizardServer(("127.0.0.1", 0))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        port = server.server_port
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request(
            "POST",
            "/api/config",
            body='{"values": {}}',
            headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}", "X-KR-Setup-Token": server.setup_token},
        )
        response = connection.getresponse()
        assert response.status == 409
        assert "只读" in json.loads(response.read().decode("utf-8"))["error"]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
