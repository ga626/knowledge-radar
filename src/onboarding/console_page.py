"""同一显式页面模板，按本地安装身份投影；不再串联 HTML 补丁。"""
from __future__ import annotations
import html
import json
import os
import re
from pathlib import Path
from functools import lru_cache

@lru_cache(maxsize=32)
def _asset(name: str) -> str:
    return (Path(__file__).with_name("static") / name).read_text(encoding="utf-8")

def _developer_view_enabled() -> bool:
    """Return whether this local installation may expose maintainer details.

    Development previews are maintainer surfaces by definition.  A stable
    installation needs an explicit local marker or launcher flag; ordinary
    users never receive a visible toggle that could silently widen the UI.
    """
    if os.environ.get("KR_CONSOLE_ROLE") == "dev":
        return True
    if os.environ.get("KR_DEVELOPER_VIEW", "").strip() == "1":
        return True
    data_root = os.environ.get("KR_DATA_ROOT", "").strip()
    if not data_root:
        return False
    marker = Path(data_root).expanduser() / "config" / "developer-console.json"
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    return payload.get("schema") == "knowledgeradar-developer-console/v1" and payload.get("enabled") is True and payload.get("audience") == "maintainer"


def render_console_page(token: str) -> str:
    developer = _developer_view_enabled()
    role = os.environ.get('KR_CONSOLE_ROLE', 'unmanaged')
    scripts = [_asset('console.js'), _asset('research_workspace.js')]
    styles = [_asset('console.css'), _asset('research_workspace.css')]
    if developer:
        scripts.extend([_asset('evaluation_summary.js'), _asset('developer_console.js')])
        styles.append(_asset('developer_console.css'))
    values = {
        'TITLE': 'KnowledgeRadar 开发版' if role == 'dev' else 'KnowledgeRadar',
        'ROLE': html.escape(role, quote=True), 'DEVELOPER': str(developer).lower(),
        'TOKEN': html.escape(token, quote=True),
        'STYLE': '\n'.join(styles),
        'SCRIPT': '\n'.join(scripts).replace('__TOKEN_JSON__', json.dumps(token)).replace('__TOKEN__', token),
        'RESEARCH_HTML': _asset('research_workspace.html'),
        'DEVELOPER_HTML': _asset('developer_console.html') if developer else '',
        'DEVELOPER_NAV': '<button type="button" data-view="developer" aria-selected="false"><span class="nav-icon">07</span><span>开发者中心</span></button>' if developer else '',
        'DEV_NOTICE': '<p class="notice">KnowledgeRadar 开发版 · 研究记录只读，配置与缓存独立</p>' if role == 'dev' else '',
    }
    return re.sub(r'@@([A-Z_]+)@@', lambda match: values[match.group(1)], _asset('page.html'))
