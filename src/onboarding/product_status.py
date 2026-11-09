"""Sanitized product-facing status for the local setup and health page."""

from __future__ import annotations

import os
import json
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sqlite3
from runtime.process import silent_subprocess_run
import sys
import time
from typing import Any

from onboarding.configuration import public_provider_guides, public_snapshot
from search_providers.catalog import SEARCH_CATALOG
from runtime.media_cache import cleanup_expired_media_cache, media_cache_root
from runtime.executables import resolve_managed_chrome
from runtime.paths import project_root


CAPABILITY_PACKS = (
    {
        "id": "core_web",
        "label": "网页发现",
        "description": "先接通一个网页发现来源，即可开始开放网页、公众号发现与公开网页研究。",
        "fields": ("TAVILY_API_KEY", "ANYSEARCH_API_KEY", "BRAVE_SEARCH_API_KEY", "EXA_API_KEY"),
        "needs": "至少一条已配置并启用的网页发现路线；无需全部配置或部署 Docker",
        "boundary": "第三方额度、网络与来源可访问性仍会影响结果。",
        "status_kind": "setup",
        "tool_count": 4,
        "tools": ("kr_research", "kr_web_search", "search_wechat_articles", "extract_web_page"),
        "provider_ids": tuple(SEARCH_CATALOG),
    },
    {
        "id": "research_sources",
        "label": "资料来源",
        "description": "按研究问题补充学术、GitHub 与 YouTube；不是开始普通网页研究的前置条件。",
        "fields": (
            "YOUTUBE_API_KEY",
            "OPENALEX_API_KEY",
            "SEMANTIC_SCHOLAR_API_KEY",
            "WFDATA_APP_KEY",
            "SERPAPI_API_KEY",
            "KR_CORE_API_KEY",
        ),
        "needs": "YouTube 需要 Key；学术默认可走公开元数据；GitHub 推荐本机 gh 登录",
        "boundary": "学术授权、GitHub 限流和公开视频地域限制分别显示，不混成“已配置”。",
        "status_kind": "optional",
        "tool_count": 4,
        "tools": ("search_academic", "search_github_repositories", "search_youtube", "get_content_detail"),
        "provider_ids": ("youtube", "openalex", "semantic_scholar", "wanfang", "serpapi_scholar", "core"),
    },
    {
        "id": "accounts_browser",
        "label": "平台账号",
        "description": "管理小红书、知乎、BOSS、猎聘等需要人工验证的平台会话。",
        "fields": (),
        "needs": "受管本地浏览器 Profile；仅在页面明确要求时，由你扫码、登录或完成验证",
        "boundary": "不保存密码、不绕过验证码或风控；空结果不是要求重新登录的理由。",
        "status_kind": "manual",
        "tool_count": 5,
        "tools": (
            "search_xiaohongshu",
            "manage_xiaohongshu_accounts",
            "search_zhihu",
            "search_recruitment",
            "get_content_detail",
        ),
        "provider_ids": ("tikhub",),
    },
    {
        "id": "content_intelligence",
        "label": "模型服务",
        "description": "百炼处理图文与音视频理解；硅基流动处理图片和扫描件 OCR。本地转写组件单独管理。",
        "fields": ("DASHSCOPE_API_KEY", "SILICONFLOW_API_KEY"),
        "needs": "基础文本交给 Codex；图片和扫描件 OCR 需要硅基流动连接；语音转写留在本地",
        "boundary": "可能产生媒体缓存、后台任务或费用；每次任务会说明实际采用的路径。",
        "status_kind": "optional",
        "tool_count": 2,
        "tools": ("get_content_detail", "get_task_status"),
        "provider_ids": ("dashscope", "siliconflow"),
    },
)


def installation_summary() -> dict[str, Any]:
    """Return the active product identity without revealing a local path.

    The loopback console is also usable from a source checkout, so the
    installation root is opt-in.  A Release launcher supplies it and receives
    only version/channel/path hashes, never a configuration value or a path.
    """
    if os.environ.get("KR_CONSOLE_ROLE") == "dev":
        from runtime.task_service import source_identity
        from runtime.paths import runtime_state_dir
        return {
            "available": True, "role": "dev", "version": "源码 " + source_identity()[:12],
            "channel": "开发预览，尚未安装", "rollback_available": False,
            "data_root_present": runtime_state_dir().is_dir(),
            "message": "当前页面运行分支源码；正式安装和原生 MCP 身份需分别核对。",
        }
    install_root_raw = os.environ.get("KR_INSTALL_ROOT", "").strip()
    if not install_root_raw:
        return {
            "available": False,
            "message": "源码兼容模式：安装身份会在已安装产品中显示。",
        }
    try:
        install_root = Path(install_root_raw).expanduser().resolve()
        active = json.loads((install_root / "active.json").read_text(encoding="utf-8"))
        if active.get("schema") != "knowledgeradar-active-install/v1":
            raise ValueError("unsupported active record")
        data_root = Path(str(active.get("data_root") or "")).resolve()
        return {
            "available": True,
            "version": str(active.get("version") or "unknown"),
            "channel": str(active.get("channel") or "unknown"),
            "data_root_hash": str(active.get("data_root_hash") or ""),
            "data_root_present": data_root.is_dir(),
            "rollback_available": (install_root / "backup" / "active.previous.json").is_file(),
            "message": "当前产品身份已加载；更新不会覆盖数据根。",
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {
            "available": False,
            "message": "无法读取安装身份；请在安装器状态页检查后重试。",
        }


def _configured_keys(snapshot: dict[str, Any]) -> set[str]:
    return {str(field.get("key")) for field in snapshot.get("fields", []) if field.get("configured")}


def capability_packs(snapshot: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Describe readiness only; it never exposes values or changes enabled features."""
    snapshot = snapshot or public_snapshot()
    configured = _configured_keys(snapshot)
    rows: list[dict[str, Any]] = []
    for pack in CAPABILITY_PACKS:
        fields = tuple(pack["fields"])
        configured_count = len(configured.intersection(fields))
        status_kind = str(pack["status_kind"])
        ready = bool(configured_count) if fields else status_kind == "ready"
        if pack['id'] == 'core_web' and 'search_ready_sources' in snapshot:
            ready = bool(snapshot['search_ready_sources'])
        if status_kind == "manual":
            from runtime.browser_sessions import browser_sessions_summary

            sessions = browser_sessions_summary(limit=100).get("sessions") or []
            pending = any(
                row.get("state") in {"NEEDS_USER", "USER_INTERACTING", "USER_DONE_VERIFYING"} for row in sessions
            )
            status = "needs_interaction" if pending else "on_call_check"
        elif status_kind == "optional" and not ready:
            status = "optional"
        else:
            status = "ready" if ready else "needs_setup"
        rows.append(
            {
                "id": pack["id"],
                "label": pack["label"],
                "description": pack["description"],
                "needs": pack["needs"],
                "boundary": pack["boundary"],
                "status": status,
                "status_kind": status_kind,
                "configured_field_count": configured_count,
                "field_count": len(fields),
                "tool_count": int(pack["tool_count"]),
                "tools": list(pack["tools"]),
                "provider_ids": list(pack["provider_ids"]),
            }
        )
    return rows


def optional_capabilities() -> list[dict[str, Any]]:
    """Return user-selectable downloads, never a generic host-software bucket."""
    root_raw = os.environ.get("KR_DATA_ROOT", "").strip()
    data_root = (
        Path(root_raw).expanduser()
        if root_raw
        else Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "KnowledgeRadar" / "data"
    )
    from runtime.xhs_opencli import connection_status
    xhs_connection = connection_status()
    xhs_ready = xhs_connection["connected"]
    from runtime.browser_link import BrowserLink
    browser_link_health = BrowserLink().health()
    browser_link_config = Path(__file__).resolve().parents[2] / 'config/browser-link.json'
    connector = json.loads(browser_link_config.read_text(encoding='utf-8')) if browser_link_config.is_file() else {}

    browser_root = data_root / "playwright"
    browser_ready = browser_root.is_dir() and any(
        path.is_file()
        for pattern in (
            "chromium-*/chrome-*/chrome.exe",
            "chromium-*/chrome-*/chrome",
            "chromium-*/chrome-*/chrome-linux*/chrome",
        )
        for path in browser_root.glob(pattern)
    )
    media_downloader_ready = bool(importlib.util.find_spec("yt_dlp"))
    from runtime.optional_media import component_plan
    from runtime.model_roles import ASR_COMPONENT

    asr = component_plan(ASR_COMPONENT)
    transcription_model_ready = asr.get("ready", False)
    return [
        {
            "id": "browser",
            "label": "Playwright Chromium",
            "description": "为动态网页采集提供隔离 Chromium；普通静态网页研究不需要它。",
            "status": "ready" if browser_ready else "not_installed",
            "requirement": "按需",
            "install_mode": "managed",
            "boundary": "在本控制台确认后下载到产品数据目录；不自动登录，也不调用付费 API。",
            "restart_required": False,
        },
        {
            "id": "xhs_bridge",
            "label": "小红书日常浏览器连接",
            "description": "OpenCLI复用日常浏览器登录；需要本人启用扩展并绑定对应浏览器。",
            "status": "ready" if xhs_ready else "needs_connection",
            "status_label": "已连接" if xhs_ready else "等待连接Chrome",
            "requirement": "按需",
            "install_mode": "guided",
            "depends_on": ("node",),
            "boundary": "浏览器扩展由本人确认权限后启用；KR只回收自有标签，不关闭日常浏览器。",
            "restart_required": False,
            "bundle_component":"opencli",
            "guide": {"steps": ("点击准备随包扩展并复制路径；在Chrome扩展管理页开启开发者模式，加载已解压的扩展。",
                    "由KR核对固定OpenCLI版本及daemon连接，绑定这个日常Chrome的context；不选Testing浏览器。",
                    "在该Chrome登录小红书；Cookie留在浏览器，KR不复制。",
                    "返回刷新连接检测；扩展在线与账号登录分别检查。")},
        },
        {
            "id": "browser_link", "label": "KR 浏览器连接器",
            "description": "知乎、BOSS、猎聘动态页和脉脉共用一个原生通道；账号留在日常Chrome。",
            "status": "ready" if browser_link_health.get('ok') else "needs_connection" if connector.get('extension_ids') else "distribution_pending",
            "status_label": "已连接" if browser_link_health.get('ok') else "等待连接Chrome" if connector.get('extension_ids') else "等待扩展正式分发",
            "requirement": "浏览器站点必需，HTTP功能无需", "install_mode": "guided",
            "restart_required": False,
            "boundary": "扩展和Host随KR交付。浏览器首次加载需要本人确认；未知旧Host注册会保留并提示接管。",
            "bundle_component":"browser_link",
            "host_handoff":True,
            "guide": {"steps": ("点击准备随包扩展并复制路径。", "在日常Chrome扩展管理页开启开发者模式，点击“加载已解压的扩展”；升级后重新加载。", "扩展连接本机Host；连接状态与站点登录分别核验，文件准备好不表示已经连接。")},
        },
        {
            "id": "media_downloader",
            "label": "媒体下载器",
            "description": "为需要取得公开音视频的媒体理解任务提供下载能力；有现成字幕或公开文本时不需要它。",
            "status": "ready" if media_downloader_ready else "not_installed",
            "requirement": "按需",
            "install_mode": "managed",
            "boundary": "仅在本控制台确认后安装到产品运行环境；实际任务仍遵守平台与内容访问规则。",
            "restart_required": False,
        },
        {
            "id": "transcription_model",
            "label": "本地语音转写（sherpa INT8 · 0.6B）",
            "description": "无字幕时同进程处理中文、英文和中英混合；运行时与固定权重统一安装。",
            "status": "ready" if transcription_model_ready else "not_installed",
            "requirement": "按需",
            "install_mode": "managed",
            "restart_required": False,
            "boundary": "本机 CPU INT8 单实例复用，不上传音频；检测到其他语言停止并返回不支持。",
        },
        *[
            {
                "id": component,
                "label": component_plan(component)["label"],
                "description": "Office 文档解析，不下载模型。",
                "status": "ready" if component_plan(component).get("ready") else "not_installed",
                "requirement": "按需",
                "install_mode": "managed",
                "restart_required": False,
                "boundary": "隔离安装，显式下载；与转写共享一个本地重计算名额。",
            }
            for component in ("docling",)
        ],
    ]


def local_component_catalog() -> dict[str, Any]:
    """Organize local setup by user goal, with every download owned by the console."""
    chrome = resolve_managed_chrome()
    node=node_component_status()
    node_available=node['status']=='ready'
    from runtime.dependency_preflight import _ffmpeg_path

    ffmpeg_available = bool(_ffmpeg_path())
    selectable = {row["id"]: row for row in optional_capabilities()}
    return {
        "summary": "基础研究随KR安装。浏览器连接、资料归档和媒体处理按需启用；已有配置在升级后继续保留。",
        "groups": [
            {
                "id": "web_automation",
                "label": "网页采集与自动化",
                "description": "处理动态网页或需要隔离浏览器的公开网页流程。",
                "items": [selectable["browser"]],
            },
            {
                "id": "platform_sessions",
                "label": "平台账号与浏览器协作",
                "description": "用于需要人工登录、扫码或平台诊断的流程。每项前置都在对应功能内说明。",
                "items": [
                    {
                        "id": "chrome",
                        "label": "Google Chrome",
                        "description": "小红书、微信读书与微信动态正文通过OpenCLI复用日常Chrome；知乎、BOSS、猎聘动态页与脉脉使用KR浏览器连接器。",
                        "status": "ready" if chrome else "not_installed",
                        "requirement": "启用平台账号后必需",
                        "install_mode": "guided",
                        "boundary": "在此查看官方安装引导并在安装后重新检测；产品不会读取密码、Cookie 或绕过验证。",
                        "guide": {
                            "official_url": "https://www.google.com/chrome/",
                            "official_label": "打开 Chrome 官方下载页",
                            "steps": (
                                "打开官方安装页并下载适用于 Windows 的 Chrome 安装程序。",
                                "按安装程序完成安装，不要把浏览器资料导入产品目录。",
                                "回到本页刷新检测；后续只在页面明确要求时人工登录或扫码。",
                            ),
                        },
                    },
                    {
                        "id": "node",
                        "label": "Node.js 与 npm",
                        "description": "启用OpenCLI浏览器连接所需的运行环境。",
                        "status": node['status'],
                        "status_label": node['label'],
                        "requirement": "启用OpenCLI后必需",
                        "install_mode": "guided",
                        "boundary": "在此查看官方安装引导并在安装后重新检测；它本身不会自动启用任何平台访问。",
                        "guide": {
                            "official_url": "https://nodejs.org/en/download",
                            "official_label": "打开 Node.js 官方下载页",
                            "steps": (
                                "下载并安装 Node.js 长期支持版（LTS），要求至少 20.18.1。",
                                "安装时保留 npm；完成后重新打开本地控制台。",
                                "回到本页刷新检测，再按小红书连接引导启用扩展并绑定Chrome。",
                            ),
                        },
                    },
                    selectable["xhs_bridge"],
                    selectable["browser_link"],
                ],
            },
            {
                "id":"knowledge_archive", "label":"资料归档与Obsidian", "description":"KR本地保存完整资料；Obsidian是可选的阅读入口。授权与软件安装分别核验。",
                "items":[obsidian_component_status()],
            },
            {
                "id": "media_understanding",
                "label": "本地媒体理解与转写",
                "description": "只有在需要下载公开媒体、提取关键帧或使用本地转写时才安装。",
                "items": [
                    selectable["media_downloader"],
                    selectable["transcription_model"],
                    selectable["docling"],
                    {
                        "id": "ffmpeg",
                        "label": "FFmpeg",
                        "description": "本地音视频转码与关键帧提取所需的工具。",
                        "status": "ready" if ffmpeg_available else "not_installed",
                        "requirement": "启用本地媒体处理后必需",
                        "install_mode": "guided",
                        "boundary": "在此查看官方安装引导并在安装后重新检测；不会自动扫描或处理你的媒体文件。",
                        "guide": {
                            "official_url": "https://ffmpeg.org/download.html",
                            "official_label": "打开 FFmpeg 官方下载页",
                            "steps": (
                                "从官方页面进入适用于 Windows 的可信发行渠道。",
                                "按发行方说明安装，并确保 ffmpeg 可通过系统 PATH 或明确配置找到。",
                                "回到本页刷新检测，再启用本地媒体处理或转写。",
                            ),
                        },
                    },
                ],
            },
        ],
    }


def node_component_status():
    executable=shutil.which('node')
    if not executable:return {'status':'not_installed','label':'尚未安装（需要≥20.18.1）'}
    try:
        import re
        result=silent_subprocess_run([executable,'--version'],capture_output=True,text=True,timeout=4,check=False)
        match=re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)\s*',result.stdout)
        version=tuple(map(int,match.groups())) if match and result.returncode==0 else ()
        ready=bool(version and version>=(20,18,1))
        return {'status':'ready' if ready else 'outdated','label':('已检测到 ' if ready else '需要升级 ')+result.stdout.strip()[:30]}
    except (OSError,ValueError,subprocess.TimeoutExpired):return {'status':'unavailable','label':'运行环境检测失败'}


def obsidian_component_status():
    candidates=[Path(os.environ.get('LOCALAPPDATA',''))/'Obsidian/Obsidian.exe',Path(os.environ.get('PROGRAMFILES',''))/'Obsidian/Obsidian.exe']
    installed=any(path.is_file() for path in candidates) or bool(shutil.which('Obsidian'))
    from runtime.knowledge_delivery import DeliveryStore
    store=DeliveryStore();grants=0
    if store.db.is_file():
        import sqlite3
        with sqlite3.connect(store.db.resolve().as_uri()+'?mode=ro',uri=True) as connection:
            grants=connection.execute('SELECT COUNT(*) FROM grants WHERE active=1').fetchone()[0]
    return {'id':'obsidian','label':'Obsidian阅读与资料库授权','status':'available' if installed else 'not_installed',
        'status_label':('已检测到软件' if installed else '未检测到软件')+' · '+('已有授权目标（逐任务核验）' if grants else '尚未授权归档目标'),
        'description':'每项调研按项目、年份和日期时间归档；报告与全部有效来源正文一并保存。',
        'requirement':'选装','install_mode':'guided','boundary':'安装软件不等于授权资料库。已有目标保留；由Codex按项目绑定明确的本地库，不扫描私人目录。',
        'guide':{'official_url':'https://obsidian.md/download','official_label':'打开Obsidian官方下载页',
            'steps':('安装Obsidian，打开你选择的资料库。','告诉Codex项目名称及允许归档的库目录；授权后按任务绑定。','回到研究记录查看报告和来源交付状态；人改内容不会被升级覆盖。')}}


def bundled_component_prepare(component):
    if component not in {'browser_link','opencli'}:raise ValueError('unknown_browser_component')
    item=_run_product_installer(['component-prepare','--component',component],timeout=180)
    return {'status':item['status'],'path':item['path'],'extension_id':item['extension_id'],
        'next_step':'在Chrome扩展管理页开启开发者模式，选择“加载已解压的扩展”，选择上面的文件夹；已有版本请点重新加载。浏览器确认完成后刷新检测。'}


def browser_host_console_action(confirmation=''):
    if confirmation:
        payload=_run_product_installer(['browser-host-apply','--confirmation',confirmation],timeout=180)
        return {'status':payload['status'],'handoff_state':'applied',
            'next_step':'Host注册交接已完成。这一步只更新本机注册，不代表扩展或站点已连接；请在Chrome重新加载Browser Link后刷新检测，已有账号与配对记录保留。'}
    payload=_run_product_installer(['browser-host-plan'],timeout=30)
    return {'status':payload['status'],'confirmation_token':payload.get('confirmation_token'),
        'handoff_state':'awaiting_confirmation' if payload.get('confirmation_token') else 'no_change',
        'next_step':'当前只是交接预览，尚未修改注册。点击“确认接管Host”才会切换本机注册，保留旧Host文件；此操作不影响账号登录态。' if payload.get('confirmation_token') else 'Host注册无需接管。此检查不验证扩展或站点连接；已有扩展保留，按实际通道检测结果使用。'}


def _run_product_installer(arguments: list[str], *, timeout: int) -> dict[str, Any]:
    """Call the active product installer and keep paths/output inside this process."""
    install_root = os.environ.get("KR_INSTALL_ROOT", "").strip()
    program_root = os.environ.get("KR_PROJECT_ROOT", "").strip()
    if not install_root or not program_root:
        raise RuntimeError("源码兼容模式不提供产品维护操作。")
    installer = Path(program_root) / "scripts" / "product_install.py"
    if not installer.is_file():
        raise RuntimeError("当前产品安装器不可用。")
    result = silent_subprocess_run(
        [sys.executable, str(installer), *arguments, "--install-root", install_root],
        cwd=Path(program_root),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("产品维护命令未返回有效状态。") from error
    if result.returncode or payload.get("status") == "FAIL":
        raise RuntimeError(str(payload.get("error") or "产品维护操作未完成。"))
    return payload


def data_root_move_console_plan(target_root: str) -> dict[str, Any]:
    """Return a path-free migration plan; applying remains a separate CLI confirmation."""
    target_root = target_root.strip()
    if not target_root or any(char in target_root for char in "\r\n\x00"):
        raise ValueError("请输入一个有效的新数据根目录。")
    payload = _run_product_installer(["data-move-plan", "--data-root", target_root], timeout=180)
    source = payload.get("source", {})
    target = payload.get("target", {})
    locks = payload.get("browser_lock_relative_paths", [])
    return {
        "status": "PLAN",
        "source": {"files": int(source.get("files", 0)), "bytes": int(source.get("bytes", 0))},
        "target": {"exists": bool(target.get("exists")), "free_bytes": int(target.get("free_bytes", 0))},
        "required_free_bytes": int(payload.get("required_free_bytes", 0)),
        "browser_lock_count": len(locks) if isinstance(locks, list) else 0,
        "confirmation_token": str(payload.get("confirmation_token", "")),
        "next_step": "核对数据量和目标目录后，点击确认迁移。原目录保留；切换后需重新打开控制台并刷新Codex连接。",
    }


def data_root_move_console_apply(target_root,confirmation):
    if not target_root.strip() or not confirmation:raise ValueError('请先生成并确认迁移计划。')
    payload=_run_product_installer(['data-move-apply','--data-root',target_root,'--confirmation',confirmation],timeout=1800)
    return {'status':payload['status'],'restart_required':True,'source_retained':True,
        'next_step':'数据已迁移，原目录保留。请重新打开本地控制台并刷新Codex连接；在此之前旧连接会拒绝新任务。'}


def data_root_move_console_rollback(confirmation):
    if confirmation!='restore_retained_source':raise ValueError('请先确认恢复迁移前数据目录。')
    payload=_run_product_installer(['data-move-rollback'],timeout=180)
    return {'status':payload['status'],'restart_required':True,'target_preserved':True,
        'next_step':'已恢复迁移前的数据目录，目标目录保留。请重新打开控制台并刷新Codex连接。'}


def optional_capability_plan(capability: str) -> dict[str, Any]:
    payload = _run_product_installer(["capability-plan", "--capability", capability], timeout=60)
    details = payload.get("details", {})
    return {
        "status": "PLAN",
        "capability": str(payload.get("capability") or capability),
        "label": str(details.get("label") or "可选能力"),
        "network_download": bool(details.get("network_download")),
        "login_required": bool(details.get("login_required")),
        "may_use_paid_api": bool(details.get("may_use_paid_api")),
        "restart_required": bool(details.get("restart_required")),
        "confirmation_token": str(payload.get("confirmation_token") or ""),
        "boundary": str(details.get("boundary") or "仅在明确确认后才会下载安装。"),
    }


def optional_capability_apply(capability: str, confirmation: str) -> dict[str, Any]:
    if not confirmation or len(confirmation) > 128:
        raise ValueError("请先生成并确认当前能力安装计划。")
    payload = _run_product_installer(
        ["capability-apply", "--capability", capability, "--confirmation", confirmation], timeout=3600
    )
    return {
        "status": str(payload.get("status") or "APPLIED"),
        "capability": str(payload.get("capability") or capability),
        "restart_required": bool(payload.get("restart_required")),
    }


def diagnostic_snapshot() -> dict[str, Any]:
    """A copyable, path-free diagnostic record safe for local viewing or export."""
    snapshot = public_snapshot()
    return {
        "schema": "knowledgeradar-local-diagnostic/v1",
        "installation": installation_summary(),
        "configured_field_count": len(_configured_keys(snapshot)),
        "supported_field_count": len(snapshot.get("fields", [])),
        "capability_packs": [{"id": row["id"], "status": row["status"]} for row in capability_packs(snapshot)],
        "optional_capabilities": [{"id": row["id"], "status": row["status"]} for row in optional_capabilities()],
        "privacy": "不含配置值、绝对路径、文件名、账号、Cookie、Profile、日志或任务内容。",
    }


def _read_dashboard_rows(database: Path, query: str, parameters: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    """Read only an already-existing runtime database; never create product state."""
    if not database.is_file():
        return []
    try:
        connection = sqlite3.connect(database, timeout=2)
        connection.row_factory = sqlite3.Row
        try:
            return list(connection.execute(query, parameters).fetchall())
        finally:
            connection.close()
    except (OSError, sqlite3.DatabaseError):
        return []


_TASK_LABELS = {
    "durable_content_detail": "资料采集与识别",
    "durable_optional_media": "本地材料处理",
    "bilibili_transcribe": "视频转写",
    "bilibili_qwen_video_analysis": "视频理解",
    "bilibili_comment_filter": "评论去重",
}
_TASK_STATUS_LABELS = {
    "waiting_for_user": "等待你完成登录或验证",
    "waiting_cooldown": "平台冷却中，到期自动进行有限探测",
    "interrupted": "已中断，可继续",
    "queued": "排队中",
    "running": "处理中",
    "completed": "已完成",
    "failed": "未完成",
    "cancelled": "已取消",
    "skipped": "已跳过",
}


def _task_age_label(age_seconds: float) -> str:
    """Return a coarse elapsed label without leaking wall-clock activity history."""
    seconds = max(0, int(age_seconds or 0))
    if seconds < 60:
        return "刚刚更新"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前更新"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前更新"
    return f"{min(seconds // 86400, 99)} 天前更新"


def _public_task_row(row: sqlite3.Row, *, now: float) -> dict[str, Any]:
    """Expose task progress without targets, URLs, IDs, paths, or error text."""
    task_type = str(row["task_type"] or "")
    platform = str(row["platform"] or "")
    status = str(row["status"] or "")
    updated_at = float(row["updated_at"] or now)
    started_at = float(row["started_at"] or row["created_at"] or updated_at)
    finished_at = float(row["finished_at"] or updated_at)
    duration_s = (
        max(0, int(finished_at - started_at))
        if status in {"completed", "failed", "cancelled", "skipped"}
        else max(0, int(now - started_at))
    )
    metadata = {}
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    status_label = _TASK_STATUS_LABELS.get(status, "状态未知")
    if isinstance(metadata, dict) and status in {"queued", "running"} and metadata.get("collection_available"):
        status_label = "正文已保存，正在补充识别" if status == "running" else "正文已保存，等待补充识别"
    elif str(row["error_code"] or "").startswith("paid_"):
        status_label = "费用额度未授权或已用完"
    return {
        "label": _TASK_LABELS.get(task_type, "后台研究处理"),
        "platform": "B 站" if task_type.startswith("bilibili_") else (platform[:32] or "本机任务"),
        "status": status if status in _TASK_STATUS_LABELS else "unknown",
        "status_label": status_label,
        "updated_label": _task_age_label(now - updated_at),
        "duration_seconds": min(duration_s, 7 * 86400),
    }


def _dashboard_task_activity(since: float) -> dict[str, Any]:
    from runtime.tasks import default_task_db_path

    database = Path(default_task_db_path())
    completed = _read_dashboard_rows(
        database,
        "SELECT COUNT(*) AS count FROM runtime_tasks WHERE status='completed' AND COALESCE(finished_at, updated_at) >= ?",
        (since,),
    )
    pending = _read_dashboard_rows(
        database,
        "SELECT status, COUNT(*) AS count FROM runtime_tasks WHERE status IN ('queued', 'running') GROUP BY status",
    )
    rows = _read_dashboard_rows(
        database,
        """SELECT task_type, platform, status, created_at, started_at, updated_at, finished_at, metadata_json, error_code
           FROM runtime_tasks ORDER BY updated_at DESC LIMIT 8""",
    )
    now = time.time()
    tasks = [_public_task_row(row, now=now) for row in rows]
    return {
        "available": database.is_file(),
        "completed": int(completed[0]["count"] or 0) if completed else 0,
        "active": sum(int(row["count"] or 0) for row in pending),
        "active_tasks": [row for row in tasks if row["status"] in {"queued", "running"}],
        "recent_tasks": tasks,
    }


def _dashboard_trace_activity(since: float) -> dict[str, Any]:
    from runtime.tool_trace import default_tool_trace_path

    trace_path = Path(default_tool_trace_path())
    if not trace_path.is_file():
        return {"available": False, "successful": 0, "top_tools": []}
    from runtime.activity_index import trace_activity

    try:
        return trace_activity(trace_path, since)
    except (OSError, sqlite3.Error):
        return {"available": False, "successful": 0, "top_tools": []}


def _dashboard_usage_activity(since: float) -> dict[str, Any]:
    from runtime.usage_tracker import default_usage_db_path

    database = Path(default_usage_db_path())
    rows = _read_dashboard_rows(
        database,
        "SELECT capability, COUNT(*) AS count FROM usage_records WHERE created_at >= ? GROUP BY capability ORDER BY count DESC LIMIT 3",
        (since,),
    )
    return {
        "available": database.is_file(),
        "top_capabilities": [
            {"label": str(row["capability"] or "unknown")[:80], "count": int(row["count"] or 0)} for row in rows
        ],
    }


def _control_plane_capabilities(packs: list[dict[str, Any]], components: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Map existing product readiness to honest, user-facing control-plane states.

    "已接入" means that the necessary local configuration exists.  It does not
    claim that a remote provider has been called or that a logged-in platform
    can be used without an interaction; those require a real task or explicit
    user action respectively.
    """
    rows: list[dict[str, str]] = []
    for pack in packs:
        status = str(pack["status"])
        if status == "ready":
            state, detail = "connected", "已接入"
        elif status == "needs_interaction":
            state, detail = "manual", "需要登录"
        elif status == "on_call_check":
            state, detail = "on_call_check", "调用时核验"
        elif status == "optional":
            state, detail = "optional", "按需启用"
        else:
            state, detail = "needs_setup", "需要配置"
        rows.append({"id": str(pack["id"]), "label": str(pack["label"]), "state": state, "detail": detail})
    for component in components:
        ready = component["status"] == "ready"
        rows.append(
            {
                "id": str(component["id"]),
                "label": str(component["label"]),
                "state": "local_ready" if ready else str(component['status']),
                "detail": "本地组件就绪" if ready else component.get('status_label') or {
                    'needs_connection': '等待连接Chrome', 'distribution_pending': '等待扩展正式分发',
                    'not_installed': '尚未安装', 'on_demand': '按需下载', 'missing': '未检测到',
                    'unavailable': '运行时缺失', 'disabled': '默认关闭',
                }.get(str(component['status']), '需要检查组件状态'),
            }
        )
    return rows


def dashboard_snapshot(*, window_days: int = 7) -> dict[str, Any]:
    """Return a bounded, redacted dashboard view from existing local records.

    This endpoint intentionally never reads task targets, URLs, account data,
    configuration values, filenames, Profile information, or error text.
    """
    window_days = max(1, min(int(window_days or 7), 30))
    since = time.time() - window_days * 86400
    configuration = public_snapshot()
    packs = capability_packs(configuration)
    components = optional_capabilities()
    installation = installation_summary()
    task_activity = _dashboard_task_activity(since)
    trace_activity = _dashboard_trace_activity(since)
    usage_activity = _dashboard_usage_activity(since)
    core_pack = next((pack for pack in packs if pack["id"] == "core_web"), None)
    control_plane = _control_plane_capabilities(packs, components)
    next_action = (
        {"view": "services", "label": "配置网页搜索", "reason": "先配置至少一个网页搜索来源，才能启用核心网页研究。"}
        if core_pack and core_pack["status"] != "ready"
        else {
            "view": "services",
            "label": "查看能力中心",
            "reason": "核心网页研究已具备最小配置；可按需要补充其他能力。",
        }
    )
    return {
        "schema": "knowledgeradar-dashboard-snapshot/v1",
        "window": {"days": window_days, "label": f"近 {window_days} 天", "recorded_locally_only": True},
        "installation": installation,
        "research_readiness": "ready" if core_pack and core_pack["status"] == "ready" else "needs_setup",
        "packs": [{"id": pack["id"], "label": pack["label"], "status": pack["status"]} for pack in packs],
        "control_plane": {
            "capabilities": control_plane,
            "connected_count": sum(item["state"] in {"connected", "local_ready"} for item in control_plane),
            "attention_count": sum(
                item["state"] not in {"connected", "local_ready", "on_call_check"} for item in control_plane
            ),
        },
        "next_action": next_action,
        "activity": {"tasks": task_activity, "tools": trace_activity, "usage": usage_activity},
        "pending": {
            "active_tasks": task_activity["active"],
            "restart_required": any(
                bool(item.get("restart_required")) and item.get("status") != "ready" for item in components
            ),
        },
        "privacy": "仅汇总本机已有记录；不返回查询、网址、任务内容、账号、文件名、Profile、Cookie、配置值或密钥。",
    }


def console_configuration_snapshot() -> dict[str, Any]:
    """Provide one versioned public configuration/guide contract for the console."""
    snapshot = public_snapshot()
    return {
        "schema": "knowledgeradar-console-configuration/v2",
        "packs": capability_packs(snapshot),
        "providers": public_provider_guides(),
        "status_legend": {
            "ready": "本机已接入",
            "needs_setup": "需要配置",
            "optional": "按需启用",
            "needs_interaction": "需要人工登录",
            "on_call_check": "调用时核验",
        },
    }


def _tree_bytes(path: Path, *, excluded: tuple[Path, ...] = ()) -> int:
    if not path.exists() or path.is_symlink() or path.is_junction():
        return 0
    try:
        total = 0
        for root, directories, files in os.walk(path, followlinks=False):
            base = Path(root)
            directories[:] = [
                name
                for name in directories
                if not (base / name).is_symlink()
                and not (base / name).is_junction()
                and not any(item == base / name or item in (base / name).parents for item in excluded)
            ]
            for name in files:
                item = base / name
                if not item.is_symlink() and not any(item == skip or skip in item.parents for skip in excluded):
                    total += item.stat().st_size
        return total
    except OSError:
        return 0


def _storage_ownership_manifest() -> dict[str, Any]:
    manifest = project_root() / "config" / "storage-ownership.manifest.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("product storage ownership manifest is unavailable") from error
    if payload.get("schema") != "knowledgeradar-storage-ownership/v1" or not isinstance(
        payload.get("categories"), list
    ):
        raise RuntimeError("product storage ownership manifest is invalid")
    return payload


def storage_summary() -> dict[str, Any]:
    """Return category totals only, never local paths, names, or file contents."""
    configured_root = os.environ.get("KR_DATA_ROOT", "").strip()
    if not configured_root:
        return {
            "schema": "knowledgeradar-local-storage-summary/v1",
            "available": False,
            "categories": [],
            "per_profile": [],
            "total_bytes": 0,
            "cleanup_scope": "源码兼容模式不会扫描工作区；安装产品后可在此查看产品数据根的空间摘要。",
        }
    data_root = Path(configured_root).expanduser()
    manifest = _storage_ownership_manifest()
    rows: list[dict[str, Any]] = []
    for category in manifest["categories"]:
        relative = str(category["relative_path"])
        path = media_cache_root() if category["id"] == "media_cache" else data_root / relative
        excluded = tuple(data_root / str(item) for item in category.get("exclude_relative_paths", []))
        rows.append(
            {
                "id": category["id"],
                "label": category["label"],
                "policy": category["policy"],
                "bytes": _tree_bytes(path, excluded=excluded),
            }
        )
    from runtime.profile_registry import profile_registry_internal, _resolve_profile_dir

    browser_root = (data_root / "browser_data").resolve()
    registered = (profile_registry_internal().get("raw") or {}).get("profiles") or []
    candidates = []
    for profile in registered:
        raw_path = _resolve_profile_dir(str(profile.get("profile_dir") or ""))
        path = Path(raw_path) if raw_path else None
        safe = bool(
            path
            and path.resolve().is_relative_to(browser_root)
            and path.resolve() != browser_root
            and not any(parent.is_symlink() or parent.is_junction() for parent in [path, *path.parents])
        )
        candidates.append((profile, path, safe))
    counted_paths = []
    projected = {}
    # 外层目录先计；同路径优先归当前账号池，实验别名只展示共享关系。
    ordered = sorted(
        candidates,
        key=lambda item: (
            len(item[1].parts) if item[1] else 999,
            item[0].get("account_pool_member") is False,
            item[0].get("role") != "primary",
        ),
    )
    for profile, path, safe in ordered:
        shared_with = next(
            (owner for other, owner in counted_paths if safe and path.resolve().is_relative_to(other)), ""
        )
        present = bool(safe and path.is_dir())
        status = (
            "outside_managed_browser_root"
            if not safe
            else "missing"
            if not present
            else "shared_directory"
            if shared_with
            else "counted"
        )
        profile_id = profile.get("profile_id", "")
        projected[profile_id] = {
            "profile_id": profile_id,
            "platform": profile.get("platform", ""),
            "display_label": profile.get("display_label") or profile.get("label") or profile_id,
            "account_slot": profile.get("account_slot", ""),
            "bytes": _tree_bytes(path) if status == "counted" else 0,
            "status": status,
            "shared_with_profile_id": shared_with,
            "scope": "browser_data_subset",
            "policy": "protected",
        }
        if status == "counted":
            counted_paths.append((path.resolve(), profile_id))
    per_profile = [projected[profile.get("profile_id", "")] for profile, _, _ in candidates]
    return {
        "schema": "knowledgeradar-local-storage-summary/v1",
        "available": True,
        "categories": rows,
        "per_profile": per_profile,
        "per_profile_bytes": sum(row["bytes"] for row in per_profile),
        "total_bytes": sum(row["bytes"] for row in rows),
        "cleanup_scope": "只对已登记且过期的媒体缓存生成清理计划；执行时先移入可恢复隔离区。密钥、Profile、浏览器资料、模型、备份和未知文件始终受保护。",
    }


def expired_media_cleanup(*, apply: bool) -> dict[str, Any]:
    """Plan or quarantine expired media cache without deleting user data.

    ``runtime.media_cache`` remains a development helper with direct deletion
    semantics.  Product users instead receive a reversible lifecycle: only
    manifest-known expired files are eligible and application moves them below
    the active data root's quarantine directory.
    """
    if not os.environ.get("KR_DATA_ROOT", "").strip():
        return {
            "status": "SKIPPED",
            "expired_file_count": 0,
            "kept_file_count": 0,
            "error_count": 0,
            "scope": "源码兼容模式不清理工作区；请在已安装产品中使用此操作。",
        }
    data_root = Path(os.environ["KR_DATA_ROOT"]).resolve()
    cache_root = media_cache_root().resolve()
    if cache_root != data_root and data_root not in cache_root.parents:
        return {
            "status": "SKIPPED",
            "expired_file_count": 0,
            "kept_file_count": 0,
            "error_count": 0,
            "scope": "媒体缓存不在当前产品数据根，拒绝自动处理。",
        }
    result = cleanup_expired_media_cache(root=cache_root, dry_run=True)
    manifest = cache_root / "manifest.jsonl"
    known = set()
    if manifest.is_file():
        from runtime.media_cache import iter_manifest_records

        for row in iter_manifest_records(cache_root) or ():
            raw = str(row.get("path") or "")
            if raw:
                known.add(Path(raw).resolve())
    eligible = [Path(raw).resolve() for raw in result["deleted"] if Path(raw).resolve() in known]
    from runtime.storage_lifecycle import quarantine_files, StorageLeases

    leases = StorageLeases()
    eligible = [path for path in eligible if not leases.protected(path)]
    applied = quarantine_files(eligible, cache_root=cache_root, data_root=data_root) if apply else {}
    errors = [row for row in applied.get("files", []) if row["status"] == "error"]
    return {
        "status": "QUARANTINED" if apply else "PLAN",
        "expired_file_count": applied.get("quarantined_count", len(eligible)),
        "receipt": applied.get("receipt", ""),
        "kept_file_count": len(result["kept"]),
        "error_count": len(result["errors"]) + len(errors),
        "scope": "只处理 manifest 已登记的过期媒体缓存，并先移入可恢复隔离区；不包含密钥、Profile、浏览器资料、日志、模型、备份或未知文件。",
    }
