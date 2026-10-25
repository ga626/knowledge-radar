"""Loopback-only configuration and maintenance console for installed products."""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from onboarding.configuration import apply_updates, public_snapshot
from runtime.account_alerts import account_alert_snapshot, recover_account
from onboarding.console_page import render_console_page
from onboarding.console_access import ConsoleAccess
from runtime.evaluation_view import evaluation_summary
from onboarding.product_status import (
    capability_packs,
    console_configuration_snapshot,
    dashboard_snapshot,
    data_root_move_console_plan,
    diagnostic_snapshot,
    expired_media_cleanup,
    installation_summary,
    local_component_catalog,
    optional_capabilities,
    optional_capability_apply,
    optional_capability_plan,
    storage_summary,
)


class WizardServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], *, access: ConsoleAccess | None = None):
        self.access = access
        self.setup_token = access.csrf_token if access else secrets.token_urlsafe(32)
        self.gate_nonce = secrets.token_urlsafe(32)
        self.setup_token_created_at = time.monotonic()
        super().__init__(server_address, _handler_factory(self))


CONSOLE_HEALTH_SCHEMA = "knowledgeradar-local-console/v2"
GUIDE_ASSETS = Path(__file__).with_name("assets").resolve()
GUIDE_SCREENSHOTS = frozenset(path.name for path in GUIDE_ASSETS.glob("*.jpg"))


def _page(token: str, snapshot: dict[str, Any]) -> str:
    """Render the static application shell; snapshots arrive over local APIs."""
    del snapshot
    return render_console_page(token)


def _handler_factory(server: WizardServer):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, status: HTTPStatus, payload: dict[str, Any], *, cookie: str = '') -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._security_headers()
            if cookie:
                self.send_header('Set-Cookie', cookie)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _security_headers(self) -> None:
            nonce = server.setup_token if (self.path != '/api/health' and self._local_request_allowed() and self._session_authenticated()) else server.gate_nonce
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                f"default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'nonce-{nonce}'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
            )
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")

        def do_GET(self) -> None:
            if not self._local_request_allowed():
                self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "local_origin_required"})
                return
            if server.access and self.path != '/api/health' and self._peer_identity() is False:
                self._send_json(HTTPStatus.FORBIDDEN, {'ok': False, 'error': '无法确认当前 Windows 账号的本机连接。请在安装此版本的账号中刷新页面。'})
                return
            if server.access and self.path != '/api/health' and not self._session_authenticated():
                if self.path == '/':
                    body = server.access.gate_page(server.gate_nonce).encode('utf-8')
                    self.send_response(HTTPStatus.OK)
                    # 门页不能通过 CSP 泄露有效 CSRF 令牌。
                    self.send_header('Content-Security-Policy', f"default-src 'none'; script-src 'nonce-{server.gate_nonce}'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Referrer-Policy', 'no-referrer')
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self._send_json(HTTPStatus.FORBIDDEN, {'ok': False, 'error': '请从本机 KnowledgeRadar 启动入口打开。'})
                return
            if self.path.startswith("/api/") and self.path != "/api/health" and not self._valid_page_token():
                self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "本地会话校验失败。请刷新页面后重试。"})
                return
            if self.path == "/api/account-alerts":
                self._send_json(HTTPStatus.OK, {**account_alert_snapshot(), "read_only": os.environ.get("KR_CONSOLE_READ_ONLY") == "1"})
                return
            if self.path == "/api/evaluation":
                from onboarding.console_page import _developer_view_enabled
                if not _developer_view_enabled():
                    self._send_json(HTTPStatus.FORBIDDEN, {'error':'maintainer_view_required'})
                    return
                self._send_json(HTTPStatus.OK, evaluation_summary())
                return
            if self.path == "/favicon.ico":
                # Keep the browser console quiet without adding a static-file
                # surface to the loopback-only configuration server.
                self.send_response(HTTPStatus.NO_CONTENT)
                self._security_headers()
                self.end_headers()
                return
            if self.path.startswith("/assets/"):
                name = self.path.removeprefix("/assets/")
                if name not in GUIDE_SCREENSHOTS:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                body = (GUIDE_ASSETS / name).read_bytes()
                self.send_response(HTTPStatus.OK)
                self._security_headers()
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/api/status":
                snapshot = public_snapshot()
                self._send_json(
                    HTTPStatus.OK,
                    {"packs": capability_packs(snapshot), "optional": optional_capabilities(), "components": local_component_catalog(), "installation": installation_summary()},
                )
                return
            if self.path == "/api/health":
                # This deliberately exposes no version, path, account, task or
                # configuration value.  The supervisor must nevertheless prove
                # that the loopback process is its own role and generation, not
                # a stale worker or an unrelated service on the fixed port.
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "schema": CONSOLE_HEALTH_SCHEMA,
                        "status": "ready",
                        "role": os.environ.get("KR_CONSOLE_ROLE", "unmanaged"),
                        "fingerprint": os.environ.get("KR_CONSOLE_FINGERPRINT", ""),
                        "generation": int(os.environ.get("KR_CONSOLE_GENERATION", "0") or 0),
                        **({'challenge': server.access.challenge} if server.access else {}),
                    },
                )
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
            if not server.access and time.monotonic() - server.setup_token_created_at >= 1800:
                server.setup_token = secrets.token_urlsafe(32)
                server.setup_token_created_at = time.monotonic()
            body = _page(server.setup_token, {}).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self._security_headers()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if not self._local_request_allowed():
                self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "local_origin_required"})
                return
            expected_origin = "http://" + self.headers.get("Host", "").lower()
            if server.access and self._peer_identity() is False:
                self._send_json(HTTPStatus.FORBIDDEN, {'ok': False, 'error': 'local_user_required'})
                return
            if server.access and self.path == '/api/session':
                if self.headers.get('Origin') != expected_origin:
                    self._send_json(HTTPStatus.FORBIDDEN, {'ok': False, 'error': 'local_origin_required'})
                    return
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 4096:
                        raise ValueError()
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict) or not isinstance(payload.get('ticket'), str) or not server.access.redeem(payload['ticket']):
                        raise ValueError()
                except (ValueError, UnicodeError):
                    self._send_json(HTTPStatus.FORBIDDEN, {'ok': False, 'error': '启动凭证无效或已过期。'})
                    return
                self._send_json(HTTPStatus.OK, {'ok': True}, cookie=server.access.session_cookie())
                return
            control = bool(server.access and self.path == '/api/console/stop' and server.access.valid_control(self.headers.get('X-KR-Control', '')))
            session = self._session_authenticated()
            if self.headers.get("Origin") != expected_origin or not (control or (session and self._valid_page_token())):
                self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "本地会话校验失败。请刷新页面后重试。"})
                return
            allowed = {
                "/api/research",
                "/api/account-recovery",
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
            if os.environ.get("KR_CONSOLE_READ_ONLY") == "1" and self.path not in {"/api/console/stop", "/api/research"}:
                self._send_json(HTTPStatus.CONFLICT, {"ok": False, "error": "开发预览只读复用稳定数据；请在 stable 控制台执行配置或维护操作。"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 65536:
                    raise ValueError("请求大小无效。")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("请求必须是JSON对象。")
                if self.path == "/api/research":
                    from runtime.research_workspace import workspace_for_console
                    from onboarding.research_api import workspace_api
                    from onboarding.console_page import _developer_view_enabled
                    response = workspace_api(workspace_for_console(), payload, developer=_developer_view_enabled())
                elif self.path == "/api/console/stop":
                    # Only the local page session can stop the host.  The
                    # version-neutral launcher uses this during an explicit
                    # product update/restart, then starts the active version.
                    response = {"ok": True, "status": "STOPPING"}
                    threading.Thread(target=server.shutdown, daemon=True).start()
                elif self.path == "/api/media-cleanup":
                    response = expired_media_cleanup(apply=payload.get("apply") is True)
                elif self.path == "/api/account-recovery":
                    response = recover_account(str(payload.get("profile_id") or ""), str(payload.get("action") or ""))
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

        def _valid_page_token(self) -> bool:
            return (bool(server.access) or time.monotonic() - server.setup_token_created_at < 1800) and secrets.compare_digest(
                self.headers.get("X-KR-Setup-Token", ""), server.setup_token
            )

        def _peer_identity(self) -> bool | None:
            if not hasattr(self, '_verified_peer'):
                self._verified_peer = server.access.connection_identity(self.connection) if server.access else None
            return self._verified_peer

        def _session_authenticated(self) -> bool:
            if not server.access:
                return True
            peer = self._peer_identity()
            return peer is True or (peer is None and server.access.valid_cookie(self.headers.get('Cookie', '')))

        def _local_request_allowed(self) -> bool:
            allowed = {f"127.0.0.1:{server.server_port}", f"localhost:{server.server_port}"}
            if self.headers.get("Host", "").lower() not in allowed:
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://{host}" for host in allowed}:
                return False
            return self.headers.get("Sec-Fetch-Site", "none") in {"none", "same-origin"}

    return Handler


def run_wizard(*, port: int = 0, open_browser: bool = True) -> str:
    access_root = Path(os.environ.get('KR_CONSOLE_ACCESS_ROOT') or Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'KnowledgeRadar' / 'console-access')
    access = ConsoleAccess(access_root, os.environ.get('KR_CONSOLE_ROLE', 'stable'))
    server = WizardServer(("127.0.0.1", port), access=access)
    url = f"http://localhost:{server.server_port}/"
    if open_browser:
        webbrowser.open(url if os.name == 'nt' else url + '#launch=' + access.issue_ticket(access.challenge))
    print(f"KnowledgeRadar 本地配置向导：{url}")
    print("只监听 127.0.0.1；按 Ctrl+C 关闭。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return url
