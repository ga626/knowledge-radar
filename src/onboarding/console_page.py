"""Loopback-only product console for the KnowledgeRadar installation."""

from __future__ import annotations

import json
from pathlib import Path
from functools import lru_cache

@lru_cache(maxsize=32)
def _asset(name: str) -> str:
    return (Path(__file__).with_name("static") / name).read_text(encoding="utf-8")

ACCOUNT_ALERT_HTML = _asset("account_alert_html.html")
ACCOUNT_ALERT_SCRIPT = _asset("account_alert_script.js")

COMPACT_ACCOUNT_STYLE = _asset("compact_account_style.css")


RADAR_STYLE = _asset("radar_style.css")


RADAR_POLISH_STYLE = _asset("radar_polish_style.css")


RADAR_OBSERVATION_STYLE = _asset("radar_observation_style.css")


RADAR_LAYOUT_STYLE = _asset("radar_layout_style.css")


RADAR_SCRIPT = _asset("radar_script.js")


CAPABILITY_CENTER_STYLE = _asset("capability_center_style.css")


# One visual contract for every console route.  Page-specific styles above may
# arrange content, but must not introduce a second typography, colour, spacing
# or motion language.  Keep these tokens semantic: component CSS consumes the
# role, never a new one-off colour value.
DESIGN_SYSTEM_STYLE = _asset("design_system_style.css")


CAPABILITY_CENTER_SCRIPT = _asset("capability_center_script.js")


def render_console_page(token: str) -> str:
    """Render a redacted, same-origin local console."""
    token_json = json.dumps(token)
    page = _asset("page.html")
    enhanced = (
        page.replace(
            '<div class="radar-metrics"><div class="radar-metric"><span>任务库</span><b id="task-source">读取中</b></div><div class="radar-metric"><span>核心研究</span><b id="ready-source">读取中</b></div></div>',
            '<div class="radar-status" aria-label="雷达实时状态"><div class="radar-status-head"><span>实时状态</span><b>本机快照</b></div><article class="radar-status-card good"><i>✓</i><b>研究就绪</b><small id="ready-note">读取中</small><strong id="ready-state">—</strong></article><article class="radar-status-card"><i>↗</i><b>能力覆盖</b><small id="coverage-note">读取中</small><strong id="coverage-state">—</strong></article><article class="radar-status-card"><i>≋</i><b>后台任务</b><small id="active-note">读取中</small><strong id="active-state">—</strong></article><article class="radar-status-card warn"><i>!</i><b>需要处理</b><small id="attention-note">读取中</small><strong id="attention-state">—</strong></article><div class="radar-status-foot"><span>任务库 <b id="task-source">读取中</b></span><span>研究 <b id="ready-source">读取中</b></span><span>当前队列 <b id="radar-queue">读取中</b></span></div></div>',
            1,
        )
        .replace('<article class="radar-status-card good"><i>✓</i><b>研究就绪</b><small id="ready-note">读取中</small><strong id="ready-state">—</strong></article>', '<button type="button" class="radar-status-card good" data-go-to="services"><i>✓</i><b>研究就绪</b><small id="ready-note">读取中</small><strong id="ready-state">—</strong></button>', 1)
        .replace('<article class="radar-status-card"><i>↗</i><b>能力覆盖</b><small id="coverage-note">读取中</small><strong id="coverage-state">—</strong></article>', '<button type="button" class="radar-status-card" data-go-to="services"><i>↗</i><b>能力覆盖</b><small id="coverage-note">读取中</small><strong id="coverage-state">—</strong></button>', 1)
        .replace('<article class="radar-status-card"><i>≋</i><b>后台任务</b><small id="active-note">读取中</small><strong id="active-state">—</strong></article>', '<button type="button" class="radar-status-card" data-scroll-to=".task-activity"><i>≋</i><b>后台任务</b><small id="active-note">读取中</small><strong id="active-state">—</strong></button>', 1)
        .replace('<article class="radar-status-card warn"><i>!</i><b>需要处理</b><small id="attention-note">读取中</small><strong id="attention-state">—</strong></article>', '<button type="button" class="radar-status-card warn" data-scroll-to=".lower-single .attention"><i>!</i><b>需要处理</b><small id="attention-note">读取中</small><strong id="attention-state">—</strong></button>', 1)
        .replace(
            '<circle class="node" cx="250" cy="117" r="4"/><circle class="node alert" id="alert-node" cx="294" cy="251" r="5"/><circle class="node" id="active-node" cx="159" cy="258" r="5"/>',
            '<g id="echo-layer" aria-hidden="true"></g><g id="alert-node" class="radar-inert-node"></g><g id="active-node" class="radar-inert-node"></g>',
            1,
        )
        .replace('<div class="caption" id="radar-caption">状态来自本机记录，不会读取任务内容。</div>', '', 1)
        .replace('LIVE LOCAL SIGNAL', '本机实时信号', 1)
        .replace("t.available?'研究雷达处于待命状态':'任务库尚未连通'", "t.available?'雷达待命':'任务库未连接'", 1)
        .replace("active?'扫描速度与任务状态会随真实队列变化。任务内容、查询和网址始终不在控制台显示。':t.available?'任务库已连通；当 Codex 发起需后台处理的研究时，任务会自动出现在下方列表。'", "active?'扫描速度随真实任务状态同步调整。':t.available?'后台任务出现时会自动记录到下方列表。'", 1)
        .replace("§('#radar-caption').textContent=t.available?(active?'实时反映本机后台任务':'任务库已连通 · 等待研究任务'):'未发现可读取的本机任务库';", "", 1)
        .replace(
            "§('#task-source').textContent=t.available?'已连通':'未连通';",
            "§('#task-source').textContent=t.available?'已连通':'未连通';§('#radar-queue').textContent=t.available?(active?active+' 项运行':'队列空闲'):'未连接';",
            1,
        )
        .replace("</style>", RADAR_STYLE + RADAR_POLISH_STYLE + RADAR_OBSERVATION_STYLE + RADAR_LAYOUT_STYLE + CAPABILITY_CENTER_STYLE + DESIGN_SYSTEM_STYLE + "</style>", 1)
        .replace(
            '<aside class="panel signals"><div class="panel-head"><h3>当前系统状态</h3><span>真实快照</span></div><p>这里区分“系统已连通”和“仍需你主动完成的按需事项”。</p><div class="stack"><div class="signal"><i class="icon good">✓</i><div><b>研究就绪</b><small id="ready-note">读取中</small></div><strong id="ready-state">—</strong></div><div class="signal"><i class="icon">↗</i><div><b>能力覆盖</b><small id="coverage-note">读取中</small></div><strong id="coverage-state">—</strong></div><div class="signal"><i class="icon">≋</i><div><b>后台任务</b><small id="active-note">读取中</small></div><strong id="active-state">—</strong></div><div class="signal"><i class="icon warn">!</i><div><b>需要处理</b><small id="attention-note">读取中</small></div><strong id="attention-state">—</strong></div></div></aside>',
            '<aside class="panel task-activity"><div class="panel-head"><h3>任务活动</h3><span id="task-summary">读取中</span></div><div class="task-cols"><article class="sub"><div class="sub-head"><b>正在进行</b><span id="active-count">—</span></div><div class="list" id="active-tasks"></div></article><article class="sub"><div class="sub-head"><b>最近任务</b><span>已脱敏</span></div><div class="list" id="recent-tasks"></div></article></div></aside>',
            1,
        )
        .replace(
            '<div class="lower"><section class="panel tasks"><div class="panel-head"><h3>任务活动</h3><span id="task-summary">读取中</span></div><div class="task-cols"><article class="sub"><div class="sub-head"><b>正在进行</b><span id="active-count">—</span></div><div class="list" id="active-tasks"></div></article><article class="sub"><div class="sub-head"><b>最近任务</b><span>已脱敏</span></div><div class="list" id="recent-tasks"></div></article></div></section><aside class="panel attention">',
            '<div class="lower lower-single"><aside class="panel attention">',
            1,
        )
        .replace(
            '<div class="radar-stage" aria-hidden="true"><svg',
            '<div class="radar-stage" aria-hidden="true"><canvas class="radar-light"></canvas><canvas class="radar-reflection"></canvas><svg',
            1,
        )
        .replace(
            '</defs><circle class="ring"',
            '<linearGradient id="sweep-tail" x1="200" y1="200" x2="20" y2="124" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#e4ffff" stop-opacity=".92"/><stop offset=".08" stop-color="#78efff" stop-opacity=".74"/><stop offset=".30" stop-color="#187cff" stop-opacity=".43"/><stop offset=".64" stop-color="#0d49c2" stop-opacity=".16"/><stop offset="1" stop-color="#06296e" stop-opacity="0"/></linearGradient><linearGradient id="sweep-arm" x1="200" y1="200" x2="200" y2="38" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#66e8ff" stop-opacity=".28"/><stop offset=".44" stop-color="#66e8ff" stop-opacity=".66"/><stop offset="1" stop-color="#66e8ff" stop-opacity=".98"/></linearGradient></defs><circle class="ring"',
            1,
        )
        .replace(
            '<g class="sweep"><path class="wedge"',
            '<g class="sweep"><path class="sweep-bloom" d="M200 200 L200 38 A162 162 0 0 0 42 222 Z"/><path class="sweep-tail" d="M200 200 L200 38 A162 162 0 0 0 42 222 Z"/><path class="wedge"',
            1,
        )
        .replace(
            '<path class="arm" d="M200 200L200 42"/></g>',
            '<path class="arm-glow" d="M200 200L200 38"/><path class="arm" d="M200 200L200 38"/></g>',
            1,
        )
        .replace("</script>", RADAR_SCRIPT + "\n" + CAPABILITY_CENTER_SCRIPT + "\n</script>", 1)
    )
    enhanced = enhanced.replace('<main class="workspace">', '<main class="workspace"><button type="button" class="button warn" id="account-alert-banner" hidden aria-live="polite"></button>', 1)
    enhanced = enhanced.replace('<section class="view" data-view-panel="components"', ACCOUNT_ALERT_HTML + '<section class="view" data-view-panel="components"', 1)
    enhanced = enhanced.replace('<button type="button" data-view="components"', '<button type="button" data-view="accounts" aria-selected="false"><span class="nav-icon">03</span><span>平台与账号</span><span id="account-nav-count" class="account-nav-count" hidden></span></button><button type="button" data-view="components"', 1)
    enhanced = enhanced.replace('data-view="components" aria-selected="false"><span class="nav-icon">03</span>', 'data-view="components" aria-selected="false"><span class="nav-icon">04</span>', 1)
    enhanced = enhanced.replace('data-view="data" aria-selected="false"><span class="nav-icon">04</span>', 'data-view="data" aria-selected="false"><span class="nav-icon">05</span>', 1)
    enhanced = enhanced.replace('data-view="settings" aria-selected="false"><span class="nav-icon">05</span>', 'data-view="settings" aria-selected="false"><span class="nav-icon">06</span>', 1)
    enhanced = enhanced.replace('这里只放需要填写 Key、服务地址或完成人工登录的能力。下载、电脑软件、诊断与数据恢复分别在其他页面管理。', '配置网页发现、资料来源与模型服务。平台登录请到左侧“平台与账号”管理。', 1)
    enhanced = enhanced.replace('先接通网页发现，再按任务配置资料来源、模型服务与平台账号', '先接通网页发现，再按任务配置资料来源与模型服务', 1)
    enhanced = enhanced.replace("function targetView(x){return x.id==='browser'||x.id==='xhs_bridge'?'components':'services';}", "function targetView(x){return x.id==='accounts_browser'?'accounts':x.id==='browser'||x.id==='xhs_bridge'?'components':'services';}", 1)
    enhanced = enhanced.replace('async function api(path,body){const r=await fetch(path,{', 'async function api(path,body,signal){const r=await fetch(path,{signal,', 1)
    enhanced = enhanced.replace('</style>', COMPACT_ACCOUNT_STYLE + '</style>', 1)
    enhanced = enhanced.replace("</script>", ACCOUNT_ALERT_SCRIPT + "</script>", 1)
    return enhanced.replace("__TOKEN__", token).replace("__TOKEN_JSON__", token_json).replace("§", "$")
