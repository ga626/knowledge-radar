"""Loopback-only configuration and maintenance console for installed products."""

from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from onboarding.configuration import apply_updates, public_snapshot
from onboarding.console_page import render_console_page
from onboarding.product_status import (
    capability_packs,
    console_configuration_snapshot,
    dashboard_snapshot,
    data_root_move_console_plan,
    diagnostic_snapshot,
    expired_media_cleanup,
    installation_summary,
    optional_capabilities,
    optional_capability_apply,
    optional_capability_plan,
    storage_summary,
)


class WizardServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int]):
        self.setup_token = secrets.token_urlsafe(32)
        super().__init__(server_address, _handler_factory(self))


CONSOLE_HEALTH_SCHEMA = "knowledgeradar-local-console/v1"


def _page(token: str, snapshot: dict[str, Any]) -> str:
    """Render the static application shell; snapshots arrive over local APIs."""
    del snapshot
    return render_console_page(token)


def _handler_factory(server: WizardServer):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _security_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                f"default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'nonce-{server.setup_token}'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
            )
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")

        def do_GET(self) -> None:
            if self.path == "/favicon.ico":
                # Keep the browser console quiet without adding a static-file
                # surface to the loopback-only configuration server.
                self.send_response(HTTPStatus.NO_CONTENT)
                self._security_headers()
                self.end_headers()
                return
            if self.path == "/api/status":
                snapshot = public_snapshot()
                self._send_json(
                    HTTPStatus.OK,
                    {"packs": capability_packs(snapshot), "optional": optional_capabilities(), "installation": installation_summary()},
                )
                return
            if self.path == "/api/health":
                # This deliberately contains no version, path, account, task, or
                # configuration information.  The version-neutral launcher only
                # needs a safe way to distinguish this loopback server from an
                # unrelated process that happens to occupy the fixed port.
                self._send_json(HTTPStatus.OK, {"schema": CONSOLE_HEALTH_SCHEMA, "status": "ready"})
                return
            if self.path == "/api/dashboard":
                self._send_json(HTTPStatus.OK, dashboard_snapshot())
                return
            if self.path == "/api/configuration":
                self._send_json(HTTPStatus.OK, console_configuration_snapshot())
                return
            if self.path == "/api/storage":
                self._send_json(HTTPStatus.OK, storage_summary())
                return
            if self.path == "/api/diagnostics":
                self._send_json(HTTPStatus.OK, diagnostic_snapshot())
                return
            if self.path != "/":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = _page(server.setup_token, public_snapshot()).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self._security_headers()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            expected_origin = f"http://127.0.0.1:{server.server_port}"
            if self.headers.get("Origin") != expected_origin or not secrets.compare_digest(
                self.headers.get("X-KR-Setup-Token", ""), server.setup_token
            ):
                self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "本地会话校验失败。请刷新页面后重试。"})
                return
            allowed = {
                "/api/config",
                "/api/media-cleanup",
                "/api/data-move-plan",
                "/api/capability-plan",
                "/api/capability-apply",
                "/api/console/stop",
            }
            if self.path not in allowed:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "不存在的操作。"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 65536:
                    raise ValueError("请求大小无效。")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if self.path == "/api/console/stop":
                    # Only the local page session can stop the host.  The
                    # version-neutral launcher uses this during an explicit
                    # product update/restart, then starts the active version.
                    response = {"ok": True, "status": "STOPPING"}
                    threading.Thread(target=server.shutdown, daemon=True).start()
                elif self.path == "/api/media-cleanup":
                    response = expired_media_cleanup(apply=payload.get("apply") is True)
                elif self.path == "/api/data-move-plan":
                    response = data_root_move_console_plan(str(payload.get("target_root") or ""))
                elif self.path == "/api/capability-plan":
                    response = optional_capability_plan(str(payload.get("capability") or ""))
                elif self.path == "/api/capability-apply":
                    response = optional_capability_apply(str(payload.get("capability") or ""), str(payload.get("confirmation") or ""))
                else:
                    response = {"ok": True, "changed_count": len(apply_updates(payload.get("values")))}
                self._send_json(HTTPStatus.OK, response)
            except (OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(error)})

    return Handler


def run_wizard(*, port: int = 0, open_browser: bool = True) -> str:
    server = WizardServer(("127.0.0.1", port))
    url = f"http://127.0.0.1:{server.server_port}/"
    if open_browser:
        webbrowser.open(url)
    print(f"KnowledgeRadar 本地配置向导：{url}")
    print("只监听 127.0.0.1；按 Ctrl+C 关闭。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return url
