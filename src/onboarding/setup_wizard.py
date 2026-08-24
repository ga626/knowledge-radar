"""A loopback-only setup wizard. It never transmits or logs configuration values."""

from __future__ import annotations

import json
import secrets
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from onboarding.configuration import apply_updates, public_snapshot
from onboarding.product_status import capability_packs, expired_media_cleanup, storage_summary


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
    page = rf"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>KnowledgeRadar 本地配置与健康台</title><style>
body{{margin:0;background:#f7f9fc;color:#172033;font:16px/1.55 system-ui,-apple-system,"Microsoft YaHei",sans-serif}}main{{max-width:880px;margin:48px auto;padding:0 24px}}section{{background:#fff;border:1px solid #dfe6f1;border-radius:18px;padding:28px;margin:18px 0;box-shadow:0 12px 34px #243b6214}}h1{{font-size:30px;margin:0 0 8px}}h2{{font-size:20px;margin:0 0 8px}}p{{color:#536176}}.notice{{border-left:4px solid #2667d9;padding:10px 14px;background:#f1f6ff}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:14px;margin-top:22px}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}}.card{{border:1px solid #dfe6f1;border-radius:12px;padding:14px}}.ready{{color:#137333;font-weight:700}}.needs{{color:#9a6700;font-weight:700}}label{{display:grid;gap:6px;font-weight:600}}input{{box-sizing:border-box;width:100%;padding:10px 12px;border:1px solid #b9c5d6;border-radius:9px;font:inherit}}button{{margin-top:14px;background:#1559c4;color:#fff;border:0;border-radius:9px;padding:11px 18px;font:inherit;font-weight:700;cursor:pointer}}button.secondary{{background:#536176}}#status,#health{{min-height:24px;margin-top:14px;font-weight:600}}.muted{{font-size:14px;color:#64748b}}dl{{display:grid;grid-template-columns:1fr auto;gap:6px 20px}}dt,dd{{margin:0}}</style><main><section><h1>配置你的本地 KnowledgeRadar</h1><p>只填写你准备启用的服务。此页面只运行在 <code>127.0.0.1</code>，配置只写入当前产品数据根；不会显示、上传或记录密钥。</p><p class="notice">已配置项不会回显。留空不会清除既有配置。高级平台会在实际调用时提示登录、额度或人工操作边界。</p><form id="form"><div class="grid">{field_rows}</div><button>保存本次填写的配置</button></form><p id="status" role="status"></p></section><section><h2>能力包</h2><p>能力按需启用。没有填写的项目不会让核心网页研究失效。</p><div class="cards" id="packs"></div></section><section><h2>本机空间与健康</h2><p>这里只显示分类大小，不显示文件名、路径或任何私密内容。清理只处理已过期的媒体缓存，绝不清理密钥、登录资料、浏览器 Profile、日志或模型。</p><div id="health">正在读取本机摘要…</div><p id="cleanup-status" role="status"></p><button class="secondary" id="refresh">刷新摘要</button><button class="secondary" id="cleanup">清理已过期媒体缓存</button></section><section><p class="muted">保存后可关闭此页；也可运行 <code>python scripts\verify_api_keys.py</code> 查看不含密钥的状态。安装、升级与回滚请阅读 <code>docs\PRODUCT_INSTALL.md</code>。</p></section></main><script nonce="{token}">const token={json.dumps(token)};const headers={{'Content-Type':'application/json','X-KR-Setup-Token':token}};const bytes=n=>n<1024?`${{n}} B`:n<1048576?`${{(n/1024).toFixed(1)}} KiB`:`${{(n/1048576).toFixed(1)}} MiB`;async function api(path,body){{const r=await fetch(path,{{method:body?'POST':'GET',headers:body?headers:{{}},body:body?JSON.stringify(body):undefined}});return r.json()}}function render(data){{document.querySelector('#packs').innerHTML=data.packs.map(p=>`<article class="card"><strong>${{p.label}}</strong><p>${{p.description}}</p><p class=${{p.status==='ready'?'ready':'needs'}}>${{p.status==='ready'?'已就绪':'待按需配置'}}</p><p class="muted">${{p.needs}}<br>${{p.boundary}}</p></article>`).join('');const s=data.storage;document.querySelector('#health').innerHTML=s.available?`<dl>${{s.categories.map(x=>`<dt>${{x.label}}</dt><dd>${{bytes(x.bytes)}}</dd>`).join('')}}<dt><strong>合计</strong></dt><dd><strong>${{bytes(s.total_bytes)}}</strong></dd></dl><p class="muted">${{s.cleanup_scope}}</p>`:`<p>${{s.cleanup_scope}}</p>`}}async function refresh(){{try{{render(await api('/api/status'))}}catch{{document.querySelector('#health').textContent='无法读取本机摘要，请稍后重试。'}}}}document.querySelector('#form').addEventListener('submit',async event=>{{event.preventDefault();const values={{}};document.querySelectorAll('[data-key]').forEach(input=>{{if(input.value.trim()) values[input.dataset.key]=input.value.trim()}});const data=await api('/api/config',{{values}});document.querySelector('#status').textContent=data.ok?`已保存 ${{data.changed_count}} 项。密钥未在页面回显。`:data.error;if(data.ok) refresh();}});document.querySelector('#refresh').onclick=refresh;document.querySelector('#cleanup').onclick=async()=>{{const data=await api('/api/media-cleanup',{{apply:true}});document.querySelector('#cleanup-status').textContent=`已清理 ${{data.expired_file_count}} 个过期媒体缓存文件。`;refresh()}};refresh();</script>"""
    return (
        page.replace("清理只处理已过期的媒体缓存", "此操作只隔离已过期的媒体缓存")
        .replace("绝不清理密钥", "绝不处理密钥")
        .replace("清理已过期媒体缓存", "隔离已过期媒体缓存")
        .replace("已清理 ${{data.expired_file_count}} 个过期媒体缓存文件。", "已隔离 ${{data.expired_file_count}} 个过期媒体缓存文件；可在数据根隔离区手动恢复或最终删除。")
        .replace("安装、升级与回滚", "安装、升级、数据迁移与回滚")
    )


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
            self.send_header("Content-Security-Policy", f"default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'nonce-{server.setup_token}'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")

        def do_GET(self) -> None:
            if self.path == "/api/status":
                snapshot = public_snapshot()
                self._send_json(HTTPStatus.OK, {"packs": capability_packs(snapshot), "storage": storage_summary()})
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
            if self.headers.get("Origin") != expected_origin or not secrets.compare_digest(self.headers.get("X-KR-Setup-Token", ""), server.setup_token):
                self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "本地会话校验失败。请刷新页面后重试。"})
                return
            if self.path not in {"/api/config", "/api/media-cleanup"}:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "不存在的操作。"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 65536:
                    raise ValueError("请求大小无效。")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if self.path == "/api/media-cleanup":
                    self._send_json(HTTPStatus.OK, expired_media_cleanup(apply=payload.get("apply") is True))
                    return
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
