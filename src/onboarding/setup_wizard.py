"""Loopback-only configuration and maintenance console for installed products."""

from __future__ import annotations

import json
import secrets
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from onboarding.configuration import apply_updates, public_snapshot
from onboarding.product_status import (
    capability_packs,
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


def _page(token: str, snapshot: dict[str, Any]) -> str:
    fields = "".join(
        f'<label><span>{item["label"]}</span><input data-key="{item["key"]}" type="{"url" if item["kind"] == "url" else "password"}" autocomplete="off" placeholder="{"已配置；留空则保持不变" if item["configured"] else "按需填写"}"></label>'
        for item in snapshot["fields"]
    )
    return rf"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>KnowledgeRadar 本地控制台</title><style>
body{{margin:0;background:#f7f9fc;color:#172033;font:16px/1.55 system-ui,-apple-system,"Microsoft YaHei",sans-serif}}main{{max-width:920px;margin:42px auto;padding:0 24px}}section{{background:#fff;border:1px solid #dfe6f1;border-radius:18px;padding:26px;margin:18px 0;box-shadow:0 12px 34px #243b6214}}h1{{font-size:30px;margin:0 0 8px}}h2{{font-size:20px;margin:0 0 8px}}p{{color:#536176}}.notice{{border-left:4px solid #2667d9;padding:10px 14px;background:#f1f6ff}}.grid,.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px;margin-top:18px}}.card{{border:1px solid #dfe6f1;border-radius:12px;padding:14px}}.ready{{color:#137333;font-weight:700}}.needs{{color:#9a6700;font-weight:700}}label{{display:grid;gap:6px;font-weight:600}}input{{box-sizing:border-box;width:100%;padding:10px 12px;border:1px solid #b9c5d6;border-radius:9px;font:inherit}}button{{margin:12px 8px 0 0;background:#1559c4;color:#fff;border:0;border-radius:9px;padding:10px 16px;font:inherit;font-weight:700;cursor:pointer}}button.secondary{{background:#536176}}button.warn{{background:#9a6700}}.muted{{font-size:14px;color:#64748b}}#status,#health,#installation,#maintenance{{min-height:24px;margin-top:12px;font-weight:600}}dl{{display:grid;grid-template-columns:1fr auto;gap:6px 20px}}dt,dd{{margin:0}}pre{{max-height:300px;overflow:auto;white-space:pre-wrap;background:#f6f8fb;border-radius:10px;padding:12px;font-size:13px}}
</style><main>
<section><h1>配置你的本地 KnowledgeRadar</h1><p>页面只运行在 <code>127.0.0.1</code>。配置只写入当前产品数据根；不会显示、上传或记录密钥。</p><p class="notice">已配置项不会回显。留空不会清除既有配置；可选下载、登录和付费调用都不会自动发生。</p><form id="form"><div class="grid">{fields}</div><button>保存本次填写的配置</button></form><p id="status" role="status"></p></section>
<section><h2>当前状态</h2><p>首屏不扫描整个数据盘，只读取脱敏的产品身份与能力状态。</p><div id="installation">正在读取当前产品身份…</div></section>
<section><h2>能力包</h2><p>没有填写或下载的项目不会让核心网页研究失效。</p><div class="cards" id="packs"></div><h3>按需下载的本地能力</h3><p class="muted">先生成计划，再点击第二次确认安装。下载内容只写入你的数据根；不会登录或调用付费 API。</p><div class="cards" id="optional-packs"></div><p id="capability-status" role="status"></p></section>
<section><h2>数据维护</h2><p>空间扫描只显示分类大小。隔离只处理已过期且已登记的媒体缓存，不处理密钥、登录资料、浏览器 Profile、日志或模型。</p><div id="health">尚未扫描；需要时点击“扫描空间”。</div><p id="cleanup-status" role="status"></p><button class="secondary" id="refresh">扫描空间</button><button class="secondary" id="cleanup">隔离过期媒体缓存</button></section>
<section><h2>迁移与诊断</h2><p>迁移始终是 <code>plan → 复制确认令牌 → 在命令行 apply → 重启验收</code>。本页不会自动迁移或删除任何数据。</p><label><span>新的空数据根目录</span><input id="move-target" autocomplete="off" placeholder="例如 D:\Software\KnowledgeRadarData"></label><button class="secondary" id="move-plan">生成迁移计划</button><pre id="maintenance" hidden></pre><button class="secondary" id="diagnostics">查看/导出脱敏诊断</button></section>
<section><p class="muted">保存后可关闭此页。安装、更新、能力包、迁移与回滚请阅读 <code>docs\PRODUCT_INSTALL.md</code> 与 <code>docs\CAPABILITY_PACKS.md</code>。</p></section></main>
<script nonce="{token}">const token={json.dumps(token)};const headers={{'Content-Type':'application/json','X-KR-Setup-Token':token}};const plans={{}};const bytes=n=>n<1024?`${{n}} B`:n<1048576?`${{(n/1024).toFixed(1)}} KiB`:`${{(n/1048576).toFixed(1)}} MiB`;async function api(path,body){{const r=await fetch(path,{{method:body?'POST':'GET',headers:body?headers:{{}},body:body?JSON.stringify(body):undefined}});const data=await r.json();if(!r.ok)throw new Error(data.error||'操作未完成');return data}}function cards(rows,target){{document.querySelector(target).innerHTML=rows.map(p=>`<article class="card"><strong>${{p.label}}</strong><p>${{p.description}}</p><p class=${{p.status==='ready'?'ready':'needs'}}>${{p.status==='ready'?'已就绪':'待按需配置'}}</p><p class="muted">${{p.needs||''}}${{p.needs?'<br>':''}}${{p.boundary}}</p>${{target==='#optional-packs'?`<button class="warn" data-capability="${{p.id}}">生成安装计划</button>`:''}}</article>`).join('')}}function renderStatus(data){{cards(data.packs,'#packs');cards(data.optional,'#optional-packs');const i=data.installation;document.querySelector('#installation').innerHTML=i.available?`<dl><dt>版本</dt><dd>${{i.version}}</dd><dt>渠道</dt><dd>${{i.channel}}</dd><dt>数据根</dt><dd>${{i.data_root_present?'可用':'需要检查'}}</dd><dt>回滚版本</dt><dd>${{i.rollback_available?'可用':'尚无'}}</dd></dl><p class="muted">${{i.message}}</p>`:`<p>${{i.message}}</p>`}}function renderStorage(s){{document.querySelector('#health').innerHTML=s.available?`<dl>${{s.categories.map(x=>`<dt>${{x.label}}</dt><dd>${{bytes(x.bytes)}}</dd>`).join('')}}<dt><strong>合计</strong></dt><dd><strong>${{bytes(s.total_bytes)}}</strong></dd></dl><p class="muted">${{s.cleanup_scope}}</p>`:`<p>${{s.cleanup_scope}}</p>`}}async function refreshStatus(){{try{{renderStatus(await api('/api/status'))}}catch(e){{document.querySelector('#installation').textContent=e.message}}}}async function refreshStorage(){{document.querySelector('#health').textContent='正在扫描分类大小…';try{{renderStorage(await api('/api/storage'))}}catch(e){{document.querySelector('#health').textContent=e.message}}}}function output(data){{const box=document.querySelector('#maintenance');box.hidden=false;box.textContent=JSON.stringify(data,null,2)}}document.querySelector('#form').addEventListener('submit',async e=>{{e.preventDefault();const values={{}};document.querySelectorAll('[data-key]').forEach(i=>{{if(i.value.trim())values[i.dataset.key]=i.value.trim()}});try{{const d=await api('/api/config',{{values}});document.querySelector('#status').textContent=`已保存 ${{d.changed_count}} 项。密钥未在页面回显。`;refreshStatus()}}catch(e){{document.querySelector('#status').textContent=e.message}}}});document.querySelector('#refresh').onclick=refreshStorage;document.querySelector('#cleanup').onclick=async()=>{{try{{const d=await api('/api/media-cleanup',{{apply:true}});document.querySelector('#cleanup-status').textContent=`已隔离 ${{d.expired_file_count}} 个过期媒体缓存文件；可在隔离区手动恢复或最终删除。`;refreshStorage()}}catch(e){{document.querySelector('#cleanup-status').textContent=e.message}}}};document.querySelector('#move-plan').onclick=async()=>{{try{{output(await api('/api/data-move-plan',{{target_root:document.querySelector('#move-target').value}}))}}catch(e){{output({{error:e.message}})}}}};document.querySelector('#diagnostics').onclick=async()=>{{try{{const d=await api('/api/diagnostics');output(d);const blob=new Blob([JSON.stringify(d,null,2)],{{type:'application/json'}});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='knowledgeradar-diagnostic.json';a.click();URL.revokeObjectURL(a.href)}}catch(e){{output({{error:e.message}})}}}};document.querySelector('#optional-packs').addEventListener('click',async e=>{{const button=e.target.closest('button[data-capability]');if(!button)return;const id=button.dataset.capability;try{{if(!plans[id]){{plans[id]=await api('/api/capability-plan',{{capability:id}});output(plans[id]);document.querySelector('#capability-status').textContent=`已生成 ${{plans[id].label}} 计划。再次点击“确认安装”才会下载。`;button.textContent='确认安装'}}else{{const d=await api('/api/capability-apply',{{capability:id,confirmation:plans[id].confirmation_token}});document.querySelector('#capability-status').textContent=d.restart_required?'已安装；请按 Codex 支持方式重启或刷新后再使用。':'已安装。';delete plans[id];button.textContent='生成安装计划';refreshStatus()}}}}catch(err){{document.querySelector('#capability-status').textContent=err.message}}}});refreshStatus();</script></html>"""


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
                self._send_json(HTTPStatus.OK, {"packs": capability_packs(snapshot), "optional": optional_capabilities(), "installation": installation_summary()})
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
            if self.headers.get("Origin") != expected_origin or not secrets.compare_digest(self.headers.get("X-KR-Setup-Token", ""), server.setup_token):
                self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "本地会话校验失败。请刷新页面后重试。"})
                return
            allowed = {"/api/config", "/api/media-cleanup", "/api/data-move-plan", "/api/capability-plan", "/api/capability-apply"}
            if self.path not in allowed:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "不存在的操作。"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 65536:
                    raise ValueError("请求大小无效。")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if self.path == "/api/media-cleanup":
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
