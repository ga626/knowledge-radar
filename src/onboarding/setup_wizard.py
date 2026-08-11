"""A loopback-only setup wizard. It never transmits or logs configuration values."""

from __future__ import annotations

import json
import secrets
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from onboarding.configuration import apply_updates, public_snapshot


class WizardServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int]):
        self.setup_token = secrets.token_urlsafe(32)
        super().__init__(server_address, _handler_factory(self))


def _page(token: str, snapshot: dict[str, Any]) -> str:
    field_rows = "".join(
        f'<label><span>{item["label"]}</span><input data-key="{item["key"]}" type="{"url" if item["kind"] == "url" else "password"}" autocomplete="off" placeholder="{"已配置；留空则保持不变" if item["configured"] else "按需填写"}"></label>'
        for item in snapshot["fields"]
    )
    return f"""<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>KnowledgeRadar 本地配置</title><style>
body{{margin:0;background:#f7f9fc;color:#172033;font:16px/1.55 system-ui,-apple-system,"Microsoft YaHei",sans-serif}}main{{max-width:760px;margin:48px auto;padding:0 24px}}section{{background:#fff;border:1px solid #dfe6f1;border-radius:18px;padding:28px;box-shadow:0 12px 34px #243b6214}}h1{{font-size:30px;margin:0 0 8px}}p{{color:#536176}}.notice{{border-left:4px solid #2667d9;padding:10px 14px;background:#f1f6ff}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:14px;margin-top:22px}}label{{display:grid;gap:6px;font-weight:600}}input{{box-sizing:border-box;width:100%;padding:10px 12px;border:1px solid #b9c5d6;border-radius:9px;font:inherit}}button{{margin-top:24px;background:#1559c4;color:#fff;border:0;border-radius:9px;padding:11px 18px;font:inherit;font-weight:700;cursor:pointer}}#status{{min-height:24px;margin-top:14px;font-weight:600}}.muted{{font-size:14px;color:#64748b}}</style><main><section><h1>配置你的本地 KnowledgeRadar</h1><p>只填写你准备启用的服务。此页面只运行在 <code>127.0.0.1</code>，配置只写入当前项目的 <code>.env</code>；不会显示、上传或记录密钥。</p><p class=\"notice\">已配置项不会回显。留空不会清除既有配置。</p><form id=\"form\"><div class=\"grid\">{field_rows}</div><button>保存本次填写的配置</button></form><p id=\"status\" role=\"status\"></p><p class=\"muted\">保存后可关闭此页，并运行 <code>python scripts\\verify_api_keys.py</code> 查看不含密钥的状态。</p></section></main><script>const token={json.dumps(token)};document.querySelector('#form').addEventListener('submit',async event=>{{event.preventDefault();const values={{}};document.querySelectorAll('[data-key]').forEach(input=>{{if(input.value.trim()) values[input.dataset.key]=input.value.trim()}});const r=await fetch('/api/config',{{method:'POST',headers:{{'Content-Type':'application/json','X-KR-Setup-Token':token}},body:JSON.stringify({{values}})}});const data=await r.json();document.querySelector('#status').textContent=data.ok?`已保存 ${{data.changed_count}} 项。密钥未在页面回显。`:data.error;}});</script>"""


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
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")

        def do_GET(self) -> None:
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
            if self.headers.get("Origin") != expected_origin or not secrets.compare_digest(self.headers.get("X-KR-Setup-Token", ""), server.setup_token):
                self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "本地会话校验失败。请刷新页面后重试。"})
                return
            if self.path != "/api/config":
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "不存在的操作。"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 65536:
                    raise ValueError("请求大小无效。")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                changed = apply_updates(payload.get("values"))
                self._send_json(HTTPStatus.OK, {"ok": True, "changed_count": len(changed)})
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
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
